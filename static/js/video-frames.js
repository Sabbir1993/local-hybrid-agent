/* Browser-side video frames: the fallback when the server has no ffmpeg (core/media/video.py is the
 * normal path). Seeks a hidden <video> to evenly spaced times and snapshots each to a JPEG.
 * Nothing is uploaded; the picture never leaves the page until /agent/vision describes a frame. */

/* evenly spaced capture times, never the very first/last instant (often black) */
function videoFrameTimes(duration, maxFrames) {
  const n = Math.max(1, Math.min(maxFrames, Math.floor(duration / 3) + 1));
  const out = [];
  for (let i = 0; i < n; i++) out.push(Math.round(duration * (i + 0.5) / n * 10) / 10);
  return out;
}

/* -> {duration, frames:[{t, mime, b64}]}; throws Error with a plain message */
async function videoFramesInBrowser(file, opts) {
  const maxFrames = (opts && opts.maxFrames) || 16, maxMinutes = (opts && opts.maxMinutes) || 10;
  const url = URL.createObjectURL(file);
  const v = document.createElement('video');
  v.muted = true; v.preload = 'auto'; v.playsInline = true;
  try {
    await new Promise((res, rej) => {
      v.onloadedmetadata = res;
      v.onerror = () => rej(new Error('this browser can’t read that video format - try mp4 (H.264)'));
      v.src = url;
    });
    const dur = v.duration;
    if (!isFinite(dur) || dur <= 0) throw new Error('couldn’t tell how long that video is');
    if (dur > maxMinutes * 60) throw new Error(`it's ${Math.round(dur / 60)} minutes long (${maxMinutes} minutes max)`);
    const scale = Math.min(1, 1024 / Math.max(v.videoWidth || 1, v.videoHeight || 1));
    const c = document.createElement('canvas');
    c.width = Math.max(2, Math.round((v.videoWidth || 640) * scale));
    c.height = Math.max(2, Math.round((v.videoHeight || 360) * scale));
    const ctx = c.getContext('2d');
    const frames = [];
    for (const t of videoFrameTimes(dur, maxFrames)) {
      await new Promise((res, rej) => {
        v.onseeked = res;
        v.onerror = () => rej(new Error('the video stopped decoding part-way'));
        v.currentTime = Math.min(t, Math.max(0, dur - 0.1));
      });
      ctx.drawImage(v, 0, 0, c.width, c.height);
      frames.push({ t, mime: 'image/jpeg', b64: c.toDataURL('image/jpeg', 0.8).split(',')[1] });
    }
    return { duration: Math.round(dur * 10) / 10, frames };
  } finally {
    URL.revokeObjectURL(url);
    v.removeAttribute('src');
    v.load();
  }
}

/* a small JPEG (long side <= 480) of a frame: this is what the chat keeps and reloads as the video's
 * preview, so it stays ~20-40 KB per video. Falls back to the full frame if the browser can't resize. */
async function videoPoster(frame) {
  const full = `data:${frame.mime};base64,${frame.b64}`;
  if (typeof Image === 'undefined' || typeof document === 'undefined' || !document.createElement) return full;
  try {
    const img = await new Promise((res, rej) => {
      const i = new Image();
      i.onload = () => res(i);
      i.onerror = () => rej(new Error('poster'));
      i.src = full;
    });
    const k = Math.min(1, 480 / Math.max(img.width, img.height));
    const c = document.createElement('canvas');
    c.width = Math.max(2, Math.round(img.width * k));
    c.height = Math.max(2, Math.round(img.height * k));
    c.getContext('2d').drawImage(img, 0, 0, c.width, c.height);
    return c.toDataURL('image/jpeg', 0.7);
  } catch (_) {
    return full;
  }
}

/* 75.4 -> "1:15" */
function videoClock(t) {
  const s = Math.max(0, Math.round(t));
  return `${Math.floor(s / 60)}:${String(s % 60).padStart(2, '0')}`;
}

/* the text that goes into the prompt: [{t, text}] frame notes + optional audio transcript */
function videoPromptText(name, duration, notes, transcript, transcriptNote) {
  const head = `(video file ${name}, ${videoClock(duration)} long, ${notes.length} picture${notes.length === 1 ? '' : 's'} sampled. ` +
    'These are descriptions of still frames by an image reader, not true video understanding: motion and short events between frames are missed.)';
  const lines = notes.map(n => `[${videoClock(n.t)}] ${n.text}`);
  const audio = transcript ? '\n(audio transcript)\n' + transcript
    : transcriptNote ? '\n(audio not transcribed: ' + transcriptNote + ')' : '';
  return head + '\n' + lines.join('\n') + audio;
}

if (typeof module !== 'undefined') module.exports = { videoFrameTimes, videoClock, videoPromptText, videoPoster };

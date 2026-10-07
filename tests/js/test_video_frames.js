// tests/js/test_video_frames.js - video attach: frame timing, prompt text, and the attach flow in
// static/js/media.js with tiny DOM/fetch stubs (no browser, no server).
const fs = require('fs');
const vm = require('vm');
const ok = (c, m) => { if (!c) { console.error('FAIL', m); process.exitCode = 1; } else console.log('ok', m); };

const vf = require(__dirname + '/../../static/js/video-frames.js');

// --- pure helpers ---
const ts = vf.videoFrameTimes(60, 8);
ok(ts.length === 8, 'caps frames at maxFrames');
ok(ts[0] > 0 && ts[ts.length - 1] < 60, 'never the very first/last instant');
ok(ts.every((t, i) => i === 0 || t > ts[i - 1]), 'times are increasing');
ok(vf.videoFrameTimes(2, 16).length === 1, 'a 2 s clip gets one frame');
ok(vf.videoClock(75.4) === '1:15' && vf.videoClock(0) === '0:00', 'clock format');
const txt = vf.videoPromptText('a.mp4', 12, [{ t: 0, text: 'a door' }, { t: 6, text: 'a hand' }], 'hello', null);
ok(txt.includes('[0:00] a door') && txt.includes('[0:06] a hand'), 'frame lines carry timestamps');
ok(txt.includes('(audio transcript)\nhello'), 'transcript appended');
ok(txt.includes('not true video understanding'), 'honest caveat is in the prompt text');
ok(vf.videoPromptText('a.mp4', 3, [{ t: 1, text: 'x' }], null, 'no speech model').includes('audio not transcribed: no speech model'),
  'transcript note shown when audio could not be transcribed');

// --- attach flow (server ffmpeg path) ---
const calls = [];
const ctx = {
  console, setTimeout, clearTimeout, Promise, JSON, Math, Date, encodeURIComponent, FormData: class { append() {} },
  $: () => null,
  esc: s => String(s),
  toast: m => { ctx.lastToast = m; },
  localStorage: { getItem: () => null, setItem() {} },
  document: { addEventListener() {}, hidden: false, querySelector: () => null },
  window: {},
  attachments: [],
  refreshAttachUI() {},
  videoClock: vf.videoClock, videoPromptText: vf.videoPromptText, videoPoster: vf.videoPoster,
  fetch: async (url, opts) => {
    calls.push(url);
    if (url === '/media/video') {
      return { ok: true, json: async () => ({ duration: 9, transcript: 'hi there', transcript_note: null,
        frames: [{ t: 1, mime: 'image/jpeg', b64: 'AAA' }, { t: 5, mime: 'image/jpeg', b64: 'BBB' }] }) };
    }
    const q = JSON.parse(opts.body);
    return { ok: true, json: async () => ({ description: 'frame ' + q.image_b64 }) };
  },
};
ctx.window = ctx;
vm.createContext(ctx);
vm.runInContext(fs.readFileSync(__dirname + '/../../static/js/media.js', 'utf8'), ctx);
vm.runInContext('MEDIA.status = { video_read: { ready: true, ffmpeg: true, max_mb: 100, max_frames: 16, max_minutes: 10 } };', ctx);

(async () => {
  ctx.mediaAttachVideo({ name: 'clip.mp4', size: 1000 });
  const att = ctx.attachments[0];
  ok(att && att.isVideo && att.uploading, 'attachment added in the uploading state');
  await att.transcribePromise;
  ok(!att.uploading, 'done flag set');
  ok(calls.filter(c => c === '/media/video').length === 1 && calls.filter(c => c === '/agent/vision').length === 2, 'one upload, one vision call per frame');
  ok(att.content.includes('[0:01] frame AAA') && att.content.includes('[0:05] frame BBB'), 'descriptions land in content');
  ok(att.content.includes('hi there'), 'server transcript included');
  ok(att.thumb && att.thumb.startsWith('data:image/jpeg;base64,AAA'), 'first frame is the thumbnail');

  // too large is refused up front
  ctx.attachments.length = 0;
  ctx.mediaAttachVideo({ name: 'big.mp4', size: 101 * 1024 * 1024 });
  ok(ctx.attachments.length === 0 && /too large/.test(ctx.lastToast), 'oversized video refused');

  // removing the card mid-way stops the loop
  ctx.fetch = async (url, opts) => {
    if (url === '/media/video') return { ok: true, json: async () => ({ duration: 9, frames: [
      { t: 1, mime: 'image/jpeg', b64: 'A' }, { t: 3, mime: 'image/jpeg', b64: 'B' }, { t: 5, mime: 'image/jpeg', b64: 'C' }] }) };
    gone.cancelled = true;            // user removed the card after the first picture
    return { ok: true, json: async () => ({ description: 'x' }) };
  };
  ctx.mediaAttachVideo({ name: 'gone.mp4', size: 1000 });
  const gone = ctx.attachments[0];
  await gone.transcribePromise;
  ok(gone.content === undefined, 'removed attachment is not filled in');

  // pressing Send empties the attachments list while the video is still being read: it must finish anyway
  ctx.attachments.length = 0;
  ctx.fetch = async url => {
    if (url === '/media/video') return { ok: true, json: async () => ({ duration: 9, frames: [
      { t: 1, mime: 'image/jpeg', b64: 'A' }, { t: 3, mime: 'image/jpeg', b64: 'B' }] }) };
    ctx.attachments.length = 0;       // what clearAttachments() does on Send
    return { ok: true, json: async () => ({ description: 'seen' }) };
  };
  ctx.mediaAttachVideo({ name: 'sent.mp4', size: 1000 });
  const sent = ctx.attachments[0];
  await sent.transcribePromise;
  ok(sent.content && sent.content.includes('[0:03] seen'), 'a video sent mid-read is still described');

  // image reader down: stops after three failures with a plain message
  ctx.attachments.length = 0;
  ctx.fetch = async url => {
    if (url === '/media/video') return { ok: true, json: async () => ({ duration: 20, frames: [1, 2, 3, 4, 5].map(i => ({ t: i, mime: 'image/jpeg', b64: 'x' })) }) };
    return { ok: false, status: 400, json: async () => ({ error: 'vision model not configured' }) };
  };
  ctx.mediaAttachVideo({ name: 'bad.mp4', size: 1000 });
  const bad = ctx.attachments[0];
  await bad.transcribePromise;
  ok(/could not read the video/.test(bad.content) && /not configured/.test(bad.content), 'reader down -> clear error text');
})();

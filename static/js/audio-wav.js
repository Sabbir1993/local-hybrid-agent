/* ---------------- audio-wav.js: audio -> 16 kHz mono WAV (speech to text) ----------------
 * Whisper wants 16 kHz mono 16-bit PCM. The browser decodes any recording or
 * file it can play (webm/opus from the mic, mp3, m4a, ogg, wav) and these
 * helpers downmix, resample and wrap it as WAV, so the server never needs ffmpeg.
 * Pure functions (no DOM) - tested in tests/js/test_audio_wav.js. */

const STT_RATE = 16000;

/* channels: array of Float32Array (one per channel) -> one mono Float32Array */
function audioDownmix(channels) {
  if (!channels || !channels.length) return new Float32Array(0);
  if (channels.length === 1) return channels[0];
  const n = channels[0].length;
  const out = new Float32Array(n);
  for (const ch of channels) {
    for (let i = 0; i < n; i++) out[i] += ch[i] / channels.length;
  }
  return out;
}

/* Linear-interpolation resample with a simple box pre-filter when shrinking
 * (48 kHz -> 16 kHz). Good enough for speech recognition. */
function audioResample(samples, fromRate, toRate) {
  if (fromRate === toRate) return samples;
  const ratio = fromRate / toRate;
  const outLen = Math.max(0, Math.floor(samples.length / ratio));
  const out = new Float32Array(outLen);
  const win = ratio > 1 ? Math.floor(ratio) : 1;
  for (let i = 0; i < outLen; i++) {
    const pos = i * ratio;
    if (win > 1) {
      let s = 0, c = 0;
      const start = Math.floor(pos);
      for (let k = 0; k < win && start + k < samples.length; k++) { s += samples[start + k]; c++; }
      out[i] = c ? s / c : 0;
    } else {
      const i0 = Math.floor(pos), i1 = Math.min(i0 + 1, samples.length - 1);
      const f = pos - i0;
      out[i] = samples[i0] * (1 - f) + samples[i1] * f;
    }
  }
  return out;
}

/* mono Float32Array (-1..1) -> WAV bytes (Uint8Array), 16-bit PCM */
function audioWavBytes(samples, rate) {
  const n = samples.length;
  const buf = new ArrayBuffer(44 + n * 2);
  const v = new DataView(buf);
  const str = (off, s) => { for (let i = 0; i < s.length; i++) v.setUint8(off + i, s.charCodeAt(i)); };
  str(0, 'RIFF'); v.setUint32(4, 36 + n * 2, true); str(8, 'WAVE');
  str(12, 'fmt '); v.setUint32(16, 16, true); v.setUint16(20, 1, true); v.setUint16(22, 1, true);
  v.setUint32(24, rate, true); v.setUint32(28, rate * 2, true); v.setUint16(32, 2, true); v.setUint16(34, 16, true);
  str(36, 'data'); v.setUint32(40, n * 2, true);
  for (let i = 0; i < n; i++) {
    const s = Math.max(-1, Math.min(1, samples[i]));
    v.setInt16(44 + i * 2, s < 0 ? s * 0x8000 : s * 0x7fff, true);
  }
  return new Uint8Array(buf);
}

/* channels + their sample rate -> 16 kHz mono WAV bytes */
function audioToSttWav(channels, rate) {
  return audioWavBytes(audioResample(audioDownmix(channels), rate, STT_RATE), STT_RATE);
}

/* Browser only: decode an audio Blob/File and convert it. -> {wav: Blob, seconds} */
async function audioBlobToSttWav(blob) {
  const Ctx = window.AudioContext || window.webkitAudioContext;
  const ctx = new Ctx();
  try {
    const decoded = await ctx.decodeAudioData(await blob.arrayBuffer());
    const channels = [];
    for (let c = 0; c < decoded.numberOfChannels; c++) channels.push(decoded.getChannelData(c));
    const bytes = audioToSttWav(channels, decoded.sampleRate);
    return { wav: new Blob([bytes], { type: 'audio/wav' }), seconds: decoded.duration };
  } finally {
    try { ctx.close(); } catch (_) {}
  }
}

if (typeof window !== 'undefined') {
  window.audioBlobToSttWav = audioBlobToSttWav;
}

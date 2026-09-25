// node tests/js/test_audio_wav.js - static/js/audio-wav.js (16 kHz mono WAV for speech to text)
const assert = require('assert');
const fs = require('fs');
const path = require('path');
const vm = require('vm');

const src = fs.readFileSync(path.join(__dirname, '..', '..', 'static', 'js', 'audio-wav.js'), 'utf8');
const ctx = { Float32Array, Uint8Array, ArrayBuffer, DataView, Math };
vm.createContext(ctx);
vm.runInContext(src, ctx);

// header: RIFF / WAVE / PCM mono 16-bit at 16 kHz
{
  const bytes = ctx.audioWavBytes(new Float32Array([0, 0.5, -0.5, 1, -1, 2]), 16000);
  const v = new DataView(bytes.buffer);
  const str = (o, n) => String.fromCharCode(...bytes.slice(o, o + n));
  assert.strictEqual(str(0, 4), 'RIFF');
  assert.strictEqual(str(8, 4), 'WAVE');
  assert.strictEqual(str(36, 4), 'data');
  assert.strictEqual(v.getUint16(20, true), 1);        // PCM
  assert.strictEqual(v.getUint16(22, true), 1);        // mono
  assert.strictEqual(v.getUint32(24, true), 16000);
  assert.strictEqual(v.getUint16(34, true), 16);
  assert.strictEqual(v.getUint32(40, true), 12);       // 6 samples * 2 bytes
  assert.strictEqual(v.getUint32(4, true), 36 + 12);
  assert.strictEqual(v.getInt16(44 + 2, true), Math.floor(0.5 * 0x7fff));
  assert.strictEqual(v.getInt16(44 + 8, true), -0x8000);
  assert.strictEqual(v.getInt16(44 + 10, true), 0x7fff);  // clipped
}

// downmix averages channels
{
  const m = ctx.audioDownmix([new Float32Array([1, 0]), new Float32Array([0, 1])]);
  assert.deepStrictEqual(Array.from(m), [0.5, 0.5]);
  assert.strictEqual(ctx.audioDownmix([]).length, 0);
}

// 48 kHz -> 16 kHz keeps the duration
{
  const one = new Float32Array(48000).fill(0.25);
  const out = ctx.audioResample(one, 48000, 16000);
  assert.strictEqual(out.length, 16000);
  assert.ok(Math.abs(out[100] - 0.25) < 1e-6);
  // upsampling (8 kHz phone audio) interpolates
  const up = ctx.audioResample(new Float32Array([0, 1]), 8000, 16000);
  assert.strictEqual(up.length, 4);
  assert.ok(Math.abs(up[1] - 0.5) < 1e-6);
}

// end to end: stereo 44.1 kHz second -> ~16000 samples of WAV
{
  const n = 44100;
  const bytes = ctx.audioToSttWav([new Float32Array(n), new Float32Array(n)], n);
  const dataLen = new DataView(bytes.buffer).getUint32(40, true);
  assert.ok(Math.abs(dataLen / 2 - 16000) <= 1, 'about one second at 16 kHz');
}

console.log('audio-wav: all tests passed');

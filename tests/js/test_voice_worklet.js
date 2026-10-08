// node tests/js/test_voice_worklet.js - static/js/voice-worklet.js (mic -> 16 kHz frames)
// The processor is loaded with fake AudioWorklet globals and fed 128-sample blocks, as the browser does.
const assert = require('assert');
const fs = require('fs');
const path = require('path');
const vm = require('vm');

const src = fs.readFileSync(path.join(__dirname, '..', '..', 'static', 'js', 'voice-worklet.js'), 'utf8');

function run(sampleRateIn, seconds, freq, block) {
  const frames = [];
  let Proc = null;
  const ctx = {
    sampleRate: sampleRateIn, Float32Array, Math,
    AudioWorkletProcessor: class { constructor() { this.port = { postMessage: (f) => frames.push(f) }; } },
    registerProcessor: (_name, cls) => { Proc = cls; },
  };
  vm.createContext(ctx);
  vm.runInContext(src, ctx);
  const p = new Proc();
  const total = Math.round(sampleRateIn * seconds);
  for (let off = 0; off < total; off += block) {
    const n = Math.min(block, total - off);
    const chunk = Float32Array.from({ length: n }, (_, i) => 0.5 * Math.sin(2 * Math.PI * freq * (off + i) / sampleRateIn));
    p.process([[chunk]]);
  }
  const out = new Float32Array(frames.length * 512);
  frames.forEach((f, i) => out.set(f, i * 512));
  return out;
}

function checkSine(label, sr, freq, block) {
  const out = run(sr, 2, freq, block);
  // 2 s at 16 kHz = 32000 samples (minus what is still buffered): the duration must not drift
  assert.ok(out.length >= 31000 && out.length <= 32000, `${label}: ${out.length} samples`);
  // frequency from zero crossings
  let crossings = 0;
  for (let i = 1; i < out.length; i++) if (out[i - 1] < 0 && out[i] >= 0) crossings++;
  const hz = crossings / (out.length / 16000);
  assert.ok(Math.abs(hz - freq) < freq * 0.02, `${label}: measured ${hz} Hz, wanted ${freq}`);
  // a dropped or repeated sample shows up as a jump; a clean 16 kHz sine moves at most 2*pi*f/16000*0.5 per sample
  const maxStep = 2 * Math.PI * freq / 16000 * 0.5 * 1.15;
  let worst = 0;
  for (let i = 1; i < out.length; i++) worst = Math.max(worst, Math.abs(out[i] - out[i - 1]));
  assert.ok(worst <= maxStep, `${label}: sample jump ${worst.toFixed(4)} > ${maxStep.toFixed(4)} (blocks dropping samples?)`);
  // amplitude kept (the box filter costs a little at high frequencies only)
  let peak = 0;
  for (const v of out) peak = Math.max(peak, Math.abs(v));
  assert.ok(peak > 0.45 && peak < 0.55, `${label}: peak ${peak}`);
}

checkSine('48 kHz, 128 blocks', 48000, 440, 128);
checkSine('48 kHz, 128 blocks, 1 kHz', 48000, 1000, 128);
checkSine('44.1 kHz, 128 blocks', 44100, 440, 128);
checkSine('44.1 kHz, odd blocks', 44100, 1000, 441);
checkSine('16 kHz passthrough', 16000, 440, 128);

// above 8 kHz must be removed, not folded down: 12 kHz in at 48 kHz stays small after decimation
{
  const out = run(48000, 1, 12000, 128);
  let peak = 0;
  for (let i = 100; i < out.length; i++) peak = Math.max(peak, Math.abs(out[i]));
  assert.ok(peak < 0.2, `12 kHz tone leaked through at ${peak}`);
}

console.log('OK test_voice_worklet');

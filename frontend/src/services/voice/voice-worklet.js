/* voice-worklet.js - AudioWorklet: microphone -> 16 kHz mono frames of 512 samples (32 ms) for the voice loop.
 * Runs on the audio thread; voice.js receives each frame as a transferred Float32Array.
 *
 * The resampler is a box-filtered decimator: every output sample is the average of TAPS linear-interpolated points
 * spread over one output period, which acts as the anti-alias low-pass (speech above 8 kHz must not fold back
 * into the band whisper listens to). `pos` is the read position inside the carried buffer and may be
 * past its end after a block: the overshoot is kept, otherwise every block would drop a sample and add a buzz at
 * (sampleRate / blockSize) Hz (tests/js/test_voice_worklet.js).
 */
const VOICE_TAPS = 4;

class VoiceFrames extends AudioWorkletProcessor {
  constructor() {
    super();
    this.step = sampleRate / 16000;      // input samples per output sample
    this.left = new Float32Array(0);     // input not yet consumed
    this.pos = 0;                        // read position inside `left` + the next block (>= 0)
    this.buf = new Float32Array(512);
    this.n = 0;
  }
  process(inputs) {
    const x = inputs[0] && inputs[0][0];
    if (!x) return true;
    let src = x;
    if (this.left.length) {
      src = new Float32Array(this.left.length + x.length);
      src.set(this.left, 0);
      src.set(x, this.left.length);
    }
    const step = this.step;
    let p = this.pos;
    while (p + step + 1 < src.length) {
      let acc = 0;
      for (let k = 0; k < VOICE_TAPS; k++) {
        const q = p + (k + 0.5) * step / VOICE_TAPS;
        const i = Math.floor(q);
        const f = q - i;
        acc += src[i] * (1 - f) + src[i + 1] * f;
      }
      this.buf[this.n++] = acc / VOICE_TAPS;
      if (this.n === 512) {
        const out = this.buf;
        this.port.postMessage(out, [out.buffer]);
        this.buf = new Float32Array(512);
        this.n = 0;
      }
      p += step;
    }
    const drop = Math.min(Math.floor(p), src.length);
    this.left = src.slice(drop);
    this.pos = p - drop;
    return true;
  }
}
registerProcessor('voice-frames', VoiceFrames);

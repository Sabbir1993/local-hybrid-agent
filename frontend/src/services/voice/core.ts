/* core.ts - the pure parts of the voice conversation (no DOM, no audio). Port of static/js/voice-core.js:
 *   voiceStripThink / voiceTakeSpeakable   the next sentences of a streaming answer to say
 *   VoiceEnergyVad                         frame -> voiced? with a self-adjusting noise floor
 *   VoiceEndpointer                        voiced? per frame -> start / end of one utterance
 *   VoiceBargeDetector                     sustained speech while the agent talks -> interrupt
 *   voiceIsHallucination / voiceLooksUnfinished / voiceFillerLang / voiceHeardText
 * Keep the numbers in step with the legacy file: they were tuned on real microphones. */

export function voiceStripThink(text: string): string {
  return String(text || '').replace(/<think>[\s\S]*?(?:<\/think>|$)/gi, '');
}

const BOUNDARY = /([.!?।॥]+["')\]]*)(\s+)|(\n+)/g;
const HAS_WORD = /[\p{L}\p{N}]/u;

function keep(piece: string): boolean {
  const p = piece.trim();
  return HAS_WORD.test(p) && !/^\d{1,3}[.)]$/.test(p);
}

interface SpeakOpts { final: boolean; first: boolean; firstMinChars: number; softCap: number; codeNote: string }

// the sentences at the start of `seg` that are complete; `flush` also takes an unfinished tail
function sentences(seg: string, flush: boolean, o: SpeakOpts, first: boolean) {
  const pieces: string[] = [];
  const ends: number[] = [];           // where each piece ends inside `seg`: lets a caller know how much was said
  let consumed = 0;
  let m: RegExpExecArray | null;
  BOUNDARY.lastIndex = 0;
  while ((m = BOUNDARY.exec(seg)) !== null) {
    const end = m.index + m[0].length;
    const piece = seg.slice(consumed, end);
    if (keep(piece)) { pieces.push(piece.trim()); ends.push(end); }
    consumed = end;
  }
  let tail = seg.slice(consumed);
  if (flush) {
    if (keep(tail)) { pieces.push(tail.trim()); ends.push(seg.length); }
    return { pieces, ends, consumed: seg.length };
  }
  // first-clause rule: start talking at the first comma once there are ~8 words, don't wait for the full sentence
  if (first && !pieces.length) {
    const m2 = /[,;:—]\s/.exec(tail.slice(o.firstMinChars - 1));
    if (m2) {
      const cut = o.firstMinChars - 1 + m2.index + m2[0].length;
      pieces.push(tail.slice(0, cut).trim());
      ends.push(consumed + cut);
      consumed += cut;
      tail = seg.slice(consumed);
    }
  }
  // a run-on with no punctuation: cut at a space so speech does not wait for the end
  if (tail.length > o.softCap) {
    const sp = tail.lastIndexOf(' ', o.softCap);
    if (sp > o.softCap / 3) {
      if (keep(tail.slice(0, sp))) { pieces.push(tail.slice(0, sp).trim()); ends.push(consumed + sp + 1); }
      consumed += sp + 1;
    }
  }
  return { pieces, ends, consumed };
}

/* text: the whole answer so far (think blocks already stripped); spoken: index up to which it was already
 * handed to the speaker. -> the new pieces to speak and the new index. Fenced code is never read out. */
export function voiceTakeSpeakable(text: string, spoken: number, opts?: Partial<SpeakOpts>) {
  const o: SpeakOpts = { final: false, first: false, firstMinChars: 28, softCap: 220, codeNote: 'The code is on the screen.', ...(opts || {}) };
  let pos = spoken || 0;
  const pieces: string[] = [];
  const ends: number[] = [];           // index into `text` just after each piece
  while (pos < text.length) {
    const fs = text.indexOf('```', pos);
    const segEnd = fs < 0 ? text.length : fs;
    const seg = text.slice(pos, segEnd);
    const r = sentences(seg, o.final || fs >= 0, o, o.first && !pieces.length);
    pieces.push(...r.pieces);
    for (const e of r.ends) ends.push(pos + e);
    pos += r.consumed;
    if (r.consumed < seg.length || fs < 0) break;
    const fe = text.indexOf('```', fs + 3);
    if (fe < 0) { if (o.final) pos = text.length; break; }       // fence still open: wait for its end
    pieces.push(o.codeNote);
    pos = fe + 3;
    ends.push(pos);
  }
  return { pieces, ends, spoken: pos };
}

/* Noise reduction. "standard" is what the browser does by itself. "crowd" is for a room full of other
 * voices: automatic gain control is OFF, the detector needs a much louder, steadier voice than the room,
 * and the audio is band-limited to the speech range before it is analysed or recorded. */
export interface VadOpts { minThreshold?: number; ratio?: number; floorMax?: number; minStats?: boolean; histFrames?: number }
export interface NoiseLevel {
  constraints: Record<string, boolean>;
  vad: VadOpts;
  startMs: number;
  filter: { hp: number; lp: number } | null;
}
export type NoiseKey = 'off' | 'standard' | 'crowd';

export const VOICE_NOISE_LEVELS: Record<NoiseKey, NoiseLevel> = {
  off: { constraints: { echoCancellation: true, noiseSuppression: false, autoGainControl: false }, vad: {}, startMs: 128, filter: null },
  standard: { constraints: { echoCancellation: true, noiseSuppression: true, autoGainControl: true }, vad: {}, startMs: 128, filter: null },
  crowd: {
    constraints: { echoCancellation: true, noiseSuppression: true, autoGainControl: false, voiceIsolation: true },
    vad: { minThreshold: 0.02, ratio: 4, floorMax: 0.03, minStats: true }, startMs: 192, filter: { hp: 120, lp: 3800 },
  },
};

// speech-band filter between `src` and whatever consumes it; returns { out, set(level) } so it can change live
export function voiceFilterChain(ctx: AudioContext, src: AudioNode, level: NoiseKey) {
  const hp = ctx.createBiquadFilter(); hp.type = 'highpass';
  const lp = ctx.createBiquadFilter(); lp.type = 'lowpass';
  src.connect(hp); hp.connect(lp);
  const set = (lv: NoiseKey) => {
    const f = VOICE_NOISE_LEVELS[lv].filter;
    hp.frequency.value = f ? f.hp : 10;           // 10 Hz / 20 kHz: effectively transparent
    lp.frequency.value = f ? f.lp : 20000;
  };
  set(level);
  return { out: lp as AudioNode, set };
}

export class VoiceEnergyVad {
  o: Required<VadOpts>;
  floor = 0.003;
  level = 0;
  hist: number[] = [];
  constructor(o?: VadOpts) {
    this.o = { minThreshold: 0.012, ratio: 3.5, floorMax: 0.05, minStats: false, histFrames: 160, ...(o || {}) };
  }
  // gate multiplies the threshold (higher while the agent is talking);
  // adapt: true only when nobody is speaking, so the floor tracks the room, not the voice
  isVoiced(frame: Float32Array, gate: number, adapt: boolean): boolean {
    let s = 0;
    for (let i = 0; i < frame.length; i++) s += frame[i] * frame[i];
    const rms = Math.sqrt(s / (frame.length || 1));
    this.level = rms;
    if (this.o.minStats) {
      // Steady babble is "voiced" all the time, so the floor would never learn it. Instead the floor is the
      // quiet end (10th percentile) of the last ~5 s: babble keeps it up, a close voice is still well above it.
      this.hist.push(rms);
      if (this.hist.length > this.o.histFrames) this.hist.shift();
      if (this.hist.length >= 30) {
        const sorted = this.hist.slice().sort((a, b) => a - b);
        this.floor = Math.min(this.o.floorMax, sorted[Math.floor(sorted.length * 0.1)]);
      }
    }
    const thr = Math.max(this.o.minThreshold, this.floor * this.o.ratio) * (gate || 1);
    const voiced = rms > thr;
    if (!this.o.minStats && adapt && !voiced) this.floor = Math.min(this.o.floorMax, this.floor * 0.95 + rms * 0.05);
    return voiced;
  }
}

export interface EndpointOpts { frameMs: number; startMs: number; endSilenceMs: number; minSpeechMs: number; maxMs: number }
export type EndpointEvent = 'start' | 'end' | 'short' | 'max' | null;

export class VoiceEndpointer {
  o: EndpointOpts;
  inSpeech = false;
  run = 0;
  silence = 0;
  voicedFrames = 0;
  frames = 0;
  trailing = 0;
  constructor(o?: Partial<EndpointOpts>) {
    this.o = { frameMs: 32, startMs: 128, endSilenceMs: 700, minSpeechMs: 300, maxMs: 60000, ...(o || {}) };
  }
  reset() { this.inSpeech = false; this.run = 0; this.silence = 0; this.voicedFrames = 0; this.frames = 0; }
  // silent frames at the end of the utterance that just ended (kept across reset(), so the caller can trim them)
  trailingSilence() { return this.trailing || 0; }
  // 'short' = too brief to be speech, 'max' = cut at the limit
  feed(voiced: boolean): EndpointEvent {
    const o = this.o;
    if (!this.inSpeech) {
      this.run = voiced ? this.run + 1 : 0;
      if (this.run * o.frameMs >= o.startMs) {
        this.inSpeech = true; this.silence = 0; this.voicedFrames = this.run; this.frames = this.run;
        return 'start';
      }
      return null;
    }
    this.frames++;
    if (voiced) { this.voicedFrames++; this.silence = 0; } else { this.silence++; }
    let ev: EndpointEvent = null;
    if (this.silence * o.frameMs >= o.endSilenceMs) {
      ev = this.voicedFrames * o.frameMs >= o.minSpeechMs ? 'end' : 'short';
    } else if (this.frames * o.frameMs >= o.maxMs) {
      ev = 'max';
    }
    if (ev) { this.trailing = this.silence; this.reset(); }
    return ev;
  }
}

export class VoiceBargeDetector {
  run = 0;
  o = { frameMs: 32, confirmMs: 256 };
  reset() { this.run = 0; }
  feed(voiced: boolean): boolean {
    this.run = voiced ? this.run + 1 : 0;
    return this.run * this.o.frameMs >= this.o.confirmMs;
  }
}

// whisper invents these from near-silence; a real one-word reply like "yes" is not in the list
export function voiceIsHallucination(text: string): boolean {
  const t = String(text || '').trim().toLowerCase().replace(/[\s.!?,'"]+/g, ' ').trim();
  if (!t) return true;
  return /^(thank you|thanks|thanks for watching|thank you for watching|you|bye|bye bye|\.+)$/.test(t)
    || /^\[?(blank_audio|music|silence|noise|applause)\]?$/.test(t)
    || /^\(.*\)$/.test(String(text || '').trim());
}

// A sentence the person has probably not finished: it trails off ("...") or ends on a connector or a filler word.
const OPEN_END = new Set(['and', 'but', 'so', 'because', 'or', 'then', 'if', 'when', 'that', 'which', 'with', 'to',
  'of', 'the', 'a', 'an', 'um', 'uh', 'hmm', 'also', 'plus', 'like', 'about', 'for', 'in', 'on', 'is', 'are',
  'এবং', 'কিন্তু', 'তাই', 'যে', 'আর', 'বা', 'যদি', 'তখন', 'কারণ', 'যখন', 'সাথে', 'জন্য', 'মানে']);
export function voiceLooksUnfinished(text: string): boolean {
  const t = String(text || '').trim();
  if (!t) return false;
  if (/(\.{3}|…)$/.test(t)) return true;
  if (/[.!?।॥]["')\]]*$/.test(t)) return false;
  const words = t.toLowerCase().replace(/[,;:]+$/, '').split(/\s+/);
  return OPEN_END.has(words[words.length - 1].replace(/[^\p{L}\p{N}\p{M}']/gu, ''));
}

// short spoken acknowledgements, played once if the real answer is slow (cached as audio at start)
export const VOICE_FILLERS: Record<'en' | 'bn', string[]> = {
  en: ['Mm-hm.', 'Okay.', 'Let me think.'],
  bn: ['হুম।', 'ঠিক আছে।', 'একটু দেখি।'],
};
export function voiceFillerLang(text: string): 'en' | 'bn' {
  const bn = (String(text || '').match(/[ঀ-৿]/g) || []).length;
  const en = (String(text || '').match(/[A-Za-z]/g) || []).length;
  return bn && bn >= en ? 'bn' : 'en';
}

// the text of the interrupted answer that was actually heard: everything up to the last piece that started playing
export function voiceHeardText(raw: string, ends: number[], startedCount: number): string {
  if (!startedCount || !ends.length) return '';
  return String(raw || '').slice(0, ends[Math.min(startedCount, ends.length) - 1]).trim();
}

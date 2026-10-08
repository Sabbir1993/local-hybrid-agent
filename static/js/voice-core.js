/* voice-core.js - the pure parts of the voice conversation: no DOM, no audio, so node can test them
 * (tests/js/test_voice_core.js). voice.js builds the live loop on top.
 *
 *   voiceStripThink(text)              the visible answer: <think> blocks removed (an open one hides the rest)
 *   voiceTakeSpeakable(text, spoken, o) -> { pieces, spoken }   the next sentences of a streaming answer to say
 *   VoiceEnergyVad                     frame -> voiced? with a self-adjusting noise floor
 *   VoiceEndpointer                    voiced? per frame -> start / end of one utterance
 *   VoiceBargeDetector                 sustained speech while the agent talks -> interrupt
 *   voiceIsHallucination(text)         "Thank you." and the like that whisper invents out of silence
 */

function voiceStripThink(text) {
  return String(text || '').replace(/<think>[\s\S]*?(?:<\/think>|$)/gi, '');
}

const _VOICE_BOUNDARY = /([.!?।॥]+["')\]]*)(\s+)|(\n+)/g;
const _VOICE_HAS_WORD = /[\p{L}\p{N}]/u;

function _voiceKeep(piece) {
  const p = piece.trim();
  return _VOICE_HAS_WORD.test(p) && !/^\d{1,3}[.)]$/.test(p);
}

// the sentences at the start of `seg` that are complete; `flush` also takes an unfinished tail
function _voiceSentences(seg, flush, o, first) {
  const pieces = [];
  const ends = [];                     // where each piece ends inside `seg`: lets a caller know how much was said
  let consumed = 0;
  let m;
  _VOICE_BOUNDARY.lastIndex = 0;
  while ((m = _VOICE_BOUNDARY.exec(seg)) !== null) {
    const end = m.index + m[0].length;
    const piece = seg.slice(consumed, end);
    if (_voiceKeep(piece)) { pieces.push(piece.trim()); ends.push(end); }
    consumed = end;
  }
  let tail = seg.slice(consumed);
  if (flush) {
    if (_voiceKeep(tail)) { pieces.push(tail.trim()); ends.push(seg.length); }
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
      if (_voiceKeep(tail.slice(0, sp))) { pieces.push(tail.slice(0, sp).trim()); ends.push(consumed + sp + 1); }
      consumed += sp + 1;
    }
  }
  return { pieces, ends, consumed };
}

/* text: the whole answer so far (think blocks already stripped); spoken: index up to which it was already
 * handed to the speaker. -> the new pieces to speak and the new index. Fenced code is never read out. */
function voiceTakeSpeakable(text, spoken, opts) {
  const o = Object.assign({ final: false, first: false, firstMinChars: 28, softCap: 220, codeNote: 'The code is on the screen.' }, opts || {});
  let pos = spoken || 0;
  const pieces = [];
  const ends = [];                     // index into `text` just after each piece
  while (pos < text.length) {
    const fs = text.indexOf('```', pos);
    const segEnd = fs < 0 ? text.length : fs;
    const seg = text.slice(pos, segEnd);
    const r = _voiceSentences(seg, o.final || fs >= 0, o, o.first && !pieces.length);
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

class VoiceEnergyVad {
  constructor(o) {
    this.o = Object.assign({ minThreshold: 0.012, ratio: 3.5, floorMax: 0.05 }, o || {});
    this.floor = 0.003;
    this.level = 0;
  }
  // frame: Float32Array; gate multiplies the threshold (higher while the agent is talking);
  // adapt: true only when nobody is speaking, so the floor tracks the room, not the voice
  isVoiced(frame, gate, adapt) {
    let s = 0;
    for (let i = 0; i < frame.length; i++) s += frame[i] * frame[i];
    const rms = Math.sqrt(s / (frame.length || 1));
    this.level = rms;
    const thr = Math.max(this.o.minThreshold, this.floor * this.o.ratio) * (gate || 1);
    const voiced = rms > thr;
    if (adapt && !voiced) this.floor = Math.min(this.o.floorMax, this.floor * 0.95 + rms * 0.05);
    return voiced;
  }
}

class VoiceEndpointer {
  constructor(o) {
    this.o = Object.assign({ frameMs: 32, startMs: 128, endSilenceMs: 700, minSpeechMs: 300, maxMs: 60000 }, o || {});
    this.reset();
  }
  reset() { this.inSpeech = false; this.run = 0; this.silence = 0; this.voicedFrames = 0; this.frames = 0; }
  // silent frames at the end of the utterance that just ended (kept across reset(), so the caller can trim them)
  trailingSilence() { return this.trailing || 0; }
  // -> 'start' | 'end' | 'short' (too brief to be speech) | 'max' (cut at the limit) | null
  feed(voiced) {
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
    let ev = null;
    if (this.silence * o.frameMs >= o.endSilenceMs) {
      ev = this.voicedFrames * o.frameMs >= o.minSpeechMs ? 'end' : 'short';
    } else if (this.frames * o.frameMs >= o.maxMs) {
      ev = 'max';
    }
    if (ev) { this.trailing = this.silence; this.reset(); }
    return ev;
  }
}

class VoiceBargeDetector {
  constructor(o) {
    this.o = Object.assign({ frameMs: 32, confirmMs: 256 }, o || {});
    this.run = 0;
  }
  reset() { this.run = 0; }
  feed(voiced) {
    this.run = voiced ? this.run + 1 : 0;
    return this.run * this.o.frameMs >= this.o.confirmMs;
  }
}

// whisper invents these from near-silence; a real one-word reply like "yes" is not in the list
function voiceIsHallucination(text) {
  const t = String(text || '').trim().toLowerCase().replace(/[\s.!?,'"]+/g, ' ').trim();
  if (!t) return true;
  return /^(thank you|thanks|thanks for watching|thank you for watching|you|bye|bye bye|\.+)$/.test(t)
    || /^\[?(blank_audio|music|silence|noise|applause)\]?$/.test(t)
    || /^\(.*\)$/.test(String(text || '').trim());
}

// A sentence the person has probably not finished: it trails off ("...") or ends on a connector or a filler word.
// The voice loop waits a little longer before answering these, as a person would.
const _VOICE_OPEN_END = new Set(['and', 'but', 'so', 'because', 'or', 'then', 'if', 'when', 'that', 'which', 'with', 'to',
  'of', 'the', 'a', 'an', 'um', 'uh', 'hmm', 'also', 'plus', 'like', 'about', 'for', 'in', 'on', 'is', 'are',
  'এবং', 'কিন্তু', 'তাই', 'যে', 'আর', 'বা', 'যদি', 'তখন', 'কারণ', 'যখন', 'সাথে', 'জন্য', 'মানে']);
function voiceLooksUnfinished(text) {
  const t = String(text || '').trim();
  if (!t) return false;
  if (/(\.{3}|…)$/.test(t)) return true;
  if (/[.!?।॥]["')\]]*$/.test(t)) return false;
  const words = t.toLowerCase().replace(/[,;:]+$/, '').split(/\s+/);
  return _VOICE_OPEN_END.has(words[words.length - 1].replace(/[^\p{L}\p{N}\p{M}']/gu, ''));
}

// short spoken acknowledgements, played once if the real answer is slow (voice.js caches them as audio at start)
const VOICE_FILLERS = {
  en: ['Mm-hm.', 'Okay.', 'Let me think.'],
  bn: ['হুম।', 'ঠিক আছে।', 'একটু দেখি।'],
};
function voiceFillerLang(text) {
  const bn = (String(text || '').match(/[ঀ-৿]/g) || []).length;
  const en = (String(text || '').match(/[A-Za-z]/g) || []).length;
  return bn && bn >= en ? 'bn' : 'en';
}

// the text of the interrupted answer that was actually heard: everything up to the last piece that started playing
function voiceHeardText(raw, ends, startedCount) {
  if (!startedCount || !ends.length) return '';
  return String(raw || '').slice(0, ends[Math.min(startedCount, ends.length) - 1]).trim();
}

// classic script: expose the classes to voice.js (the lint globals are read from `window.X =` assignments)
if (typeof window !== 'undefined') {
  window.VoiceEnergyVad = VoiceEnergyVad;
  window.VoiceEndpointer = VoiceEndpointer;
  window.VoiceBargeDetector = VoiceBargeDetector;
}

if (typeof module !== 'undefined') {
  module.exports = { voiceStripThink, voiceTakeSpeakable, VoiceEnergyVad, VoiceEndpointer, VoiceBargeDetector, voiceIsHallucination,
    voiceLooksUnfinished, voiceFillerLang, voiceHeardText, VOICE_FILLERS };
}

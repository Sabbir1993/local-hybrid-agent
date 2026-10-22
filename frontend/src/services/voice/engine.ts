/* engine.ts - hands-free voice conversation (port of static/js/voice.js).
 *
 *   mic -> worklet frames (16 kHz) -> VAD + endpointing -> one WAV per utterance -> /media/transcribe
 *       -> a normal chat turn (host.send, with voice: true)
 *       -> the growing answer is read sentence by sentence -> /media/speak (local CPU TTS) -> speakers
 *
 * The engine knows nothing about React or the store: VoiceMode.tsx hands it a VoiceHost. Audio goes only to the
 * app's own endpoints; the microphone and the audio context are released in stop(). Spoken text is cleaned and
 * card numbers are masked on the server (core/media/tts.py), never trusted to this file. An approval card is
 * never answered by voice: the agent says it is waiting and the card stays on screen. */
import workletUrl from './voice-worklet.js?url';
import { apiFetch } from '../api';
import {
  VOICE_FILLERS, VOICE_NOISE_LEVELS, VoiceBargeDetector, VoiceEndpointer, VoiceEnergyVad, voiceFillerLang, voiceFilterChain,
  voiceHeardText, voiceIsHallucination, voiceLooksUnfinished, voiceStripThink, voiceTakeSpeakable, type NoiseKey,
} from './core';
import { audioWavBytes } from './wav';

const PREROLL_FRAMES = 10;            // 320 ms kept from before the speech start, so first syllables are not clipped
const IDLE_END_MS = 120000;           // leave voice mode after 2 minutes without anyone speaking
const SPEAK_GATE = 2.2;               // the mic must be this much louder than usual while the agent is talking
const DEFAULT_END_MS = 500;           // silence that ends a turn (server config voice.end_silence_ms)
const EXTRA_WAIT_MS = 700;            // extra wait when the sentence sounds unfinished
const MERGE_WINDOW_MS = 1500;         // resuming within this long after sending, before any speech: same turn
const FILLER_AFTER_MS = 1100;         // slow answer: say "Mm-hm." after this long
const DUCK_GAIN = 0.3;
// served by the backend from /static (same origin, so the CSP's script-src 'self' allows it; no blob: URL needed)
// bundled with the app (a same-origin file under /assets), so voice does not depend on the old UI's static folder
const WORKLET_URL = workletUrl;

export type VoiceState = 'idle' | 'listening' | 'hearing' | 'thinking' | 'speaking';
export type VoiceLang = 'auto' | 'en' | 'bn';

export interface VoiceHostMessage { id: string; role: string; content: string; errorAlert?: string }
export interface VoiceHost {
  send: (text: string) => void;                       // one chat turn, flagged voice: true
  abort: () => void;                                  // stop the running answer
  isBusy: () => boolean;                              // an answer is being generated
  isOwnSession?: () => boolean;                       // the active chat is still the one the conversation runs in
  messageCount: () => number;
  messageAt: (i: number) => VoiceHostMessage | undefined;
  trimMessage: (id: string, content: string) => void; // keep only what was heard after an interruption
  persistTrim?: (id: string, content: string) => void; // same, for the saved copy (needs a server update endpoint; none yet)
  permissionPending: () => boolean;                   // an approval card is waiting
  notify: (msg: string, bad?: boolean) => void;
}
export interface VoiceCallbacks {
  onState: (s: VoiceState) => void;
  onLevel: (level: number) => void;                   // 0..1 microphone level
  onHeard: (text: string) => void;
  onEnded: () => void;                                // the engine stopped itself (idle timeout)
}
export interface VoiceOptions { lang: VoiceLang; noise: NoiseKey }

/* ---------------- transport (HTTP) ---------------- */
async function errText(r: Response): Promise<string> {
  const j = await r.json().catch(() => ({}));
  return j.error || j.detail || `HTTP ${r.status}`;
}

async function transcribe(wav: Blob, lang: VoiceLang): Promise<string> {
  const fd = new FormData();
  fd.append('file', wav, 'speech.wav');
  fd.append('language', lang || 'auto');
  const r = await apiFetch('/media/transcribe', { method: 'POST', body: fd });
  if (!r.ok) throw new Error(await errText(r));
  const j = await r.json().catch(() => ({}));
  return String(j.text || '').trim();
}

async function speak(text: string, signal?: AbortSignal): Promise<ArrayBuffer> {
  const r = await apiFetch('/media/speak', {
    method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ text }), signal,
  });
  if (!r.ok) throw new Error(await errText(r));
  return r.arrayBuffer();
}

/* ---------------- playback: gapless, cancellable, duckable ---------------- */
class VoicePlayer {
  ctx: AudioContext;
  sources = new Set<AudioBufferSourceNode>();
  next = 0;
  gen = 0;
  onFirstSound: (() => void) | null = null;
  gain: GainNode;
  started: { at: number; end: number | null }[] = [];   // one per scheduled piece: `end` = how much of the answer it covers
  constructor(ctx: AudioContext) {
    this.ctx = ctx;
    this.gain = ctx.createGain();
    this.gain.connect(ctx.destination);
  }
  get busy() { return this.sources.size > 0; }
  duck(on: boolean) { this.gain.gain.setTargetAtTime(on ? DUCK_GAIN : 1, this.ctx.currentTime, 0.03); }
  // how many answer pieces have begun to play (the rest was never heard)
  startedCount() { const t = this.ctx.currentTime; return this.started.filter((p) => p.end != null && p.at <= t).length; }
  schedule(buf: AudioBuffer, end: number | null) {
    const src = this.ctx.createBufferSource();
    src.buffer = buf;
    src.connect(this.gain);
    const at = Math.max(this.ctx.currentTime + 0.02, this.next);
    src.start(at);
    this.next = at + buf.duration;
    this.sources.add(src);
    src.onended = () => this.sources.delete(src);
    this.started.push({ at, end });
    if (end != null && this.onFirstSound) { const f = this.onFirstSound; this.onFirstSound = null; f(); }
  }
  async play(arrayBuffer: ArrayBuffer, gen: number, end: number | null) {
    const buf = await this.ctx.decodeAudioData(arrayBuffer.slice(0));
    if (gen !== this.gen) return;
    this.schedule(buf, end);
  }
  stop() {
    this.gen++;
    for (const s of this.sources) { try { s.stop(); } catch { /* already ended */ } }
    this.sources.clear();
    this.next = 0;
    this.started = [];
    this.duck(false);
  }
}

/* sentences in, speech out, in order; the next two are fetched while one plays */
class SpeakQueue {
  player: VoicePlayer;
  onError: (e: Error) => void;
  items: { text: string; end: number | null; p: Promise<ArrayBuffer | null> | null }[] = [];
  draining = false;
  gen = 0;
  ctl = new AbortController();
  constructor(player: VoicePlayer, onError: (e: Error) => void) {
    this.player = player; this.onError = onError;
  }
  isSpeaking() { return this.items.length > 0 || this.draining || this.player.busy; }
  push(text: string, end: number | null) {
    this.items.push({ text, end, p: null });
    if (!this.draining) { this.draining = true; void this.drain(this.gen); }
  }
  cancel() {
    this.gen++;
    this.ctl.abort();
    this.ctl = new AbortController();
    this.items = [];
    this.draining = false;
    this.player.stop();
  }
  private async drain(g: number) {
    while (g === this.gen && this.items.length) {
      for (const it of this.items.slice(0, 3)) {
        if (!it.p) {
          const signal = this.ctl.signal;
          it.p = speak(it.text, signal).catch((e: Error) => { if (e.name !== 'AbortError') this.onError(e); return null; });
        }
      }
      const it = this.items[0];
      const ab = await it.p;
      if (g !== this.gen) return;
      this.items.shift();
      if (ab) { try { await this.player.play(ab, this.player.gen, it.end); } catch (e) { if (g === this.gen) this.onError(e as Error); } }
    }
    if (g === this.gen) this.draining = false;
  }
}

function concatFrames(frames: Float32Array[]): Float32Array {
  let n = 0;
  for (const f of frames) n += f.length;
  const out = new Float32Array(n);
  let o = 0;
  for (const f of frames) { out.set(f, o); o += f.length; }
  return out;
}

// when each step of one turn happened (performance.now()), for the console.debug latency line
interface TurnTimes { endOfSpeech: number; transcript: number; firstToken: number; firstAudio: number }

interface Reply {
  known: Set<string>; msgId: string; msg: VoiceHostMessage | null; text: string; t: TurnTimes | null; spoken: number; first: boolean; lastRaw: string; ends: number[];
  said: boolean; sawBusy: boolean; busyAt?: number; quiet: number; startedAt: number; sentAt: number;
  permNoted: boolean; errNoted: boolean; fillerDone: boolean; lang: 'en' | 'bn';
}

export class VoiceEngine {
  active = false;
  state: VoiceState = 'idle';
  private host: VoiceHost;
  private cb: VoiceCallbacks;
  private opt: VoiceOptions;
  private ctx: AudioContext | null = null;
  private stream: MediaStream | null = null;
  private node: AudioWorkletNode | null = null;
  private src: MediaStreamAudioSourceNode | null = null;
  private chain: { out: AudioNode; set: (l: NoiseKey) => void } | null = null;
  private player!: VoicePlayer;
  private queue!: SpeakQueue;
  private vad!: VoiceEnergyVad;
  private ep!: VoiceEndpointer;
  private barge = new VoiceBargeDetector();
  private ring: Float32Array[] = [];
  private utter: Float32Array[] = [];
  private reply: Reply | null = null;
  private turnId = 0;
  private sentAny = false;
  private carry = '';
  private lastActivity = 0;
  private pendingSend: { text: string; timer: number } | null = null;
  private pendingTrim: { msg: VoiceHostMessage; text: string } | null = null;
  private sentText = '';
  private ducked = false;
  private quietFrames = 0;
  private fillersOn = true;
  private fillers: Record<'en' | 'bn', AudioBuffer[]> = { en: [], bn: [] };
  private timer = 0;
  private startSeq = 0;                       // bumped by every start() and by stop(): an older start() sees it changed and gives up
  private turnT: TurnTimes | null = null;

  constructor(host: VoiceHost, cb: VoiceCallbacks, opt: VoiceOptions) {
    this.host = host; this.cb = cb; this.opt = opt;
  }

  private setState(st: VoiceState) {
    this.state = st;
    this.cb.onState(st);
  }

  // the settings can change while talking: detector, filter and microphone follow them
  setOptions(opt: VoiceOptions) {
    this.opt = opt;
    if (!this.active) return;
    const ns = VOICE_NOISE_LEVELS[opt.noise];
    this.vad = new VoiceEnergyVad(ns.vad);
    this.ep.o.startMs = ns.startMs;
    try { this.chain?.set(opt.noise); } catch { /* keep the old filter */ }
    try { this.stream?.getAudioTracks().forEach((t) => { t.applyConstraints(ns.constraints).catch(() => {}); }); } catch { /* keep the old constraints */ }
  }

  /* ---------------- start / stop ---------------- */
  async start(): Promise<boolean> {
    if (this.active) return true;
    // stop() (or a newer start()) while this one is still awaiting: leave quietly, no microphone, no state, no toast
    const seq = ++this.startSeq;
    const stale = () => seq !== this.startSeq;
    // speech to text must be set up, and so must the local voice (replies are spoken on this PC)
    try {
      const mr = await apiFetch('/media/status');
      if (stale()) return false;
      if (!mr.ok) { this.host.notify('Could not check speech to text (HTTP ' + mr.status + ').', true); return false; }
      const m = await mr.json();
      if (stale()) return false;
      if (!(m.transcribe && m.transcribe.ready)) { this.host.notify('Speech to text is not set up (Settings -> Models & Jobs).', true); return false; }
    } catch { if (!stale()) this.host.notify('Could not check speech to text.', true); return false; }
    let st: any;
    try {
      const tr = await apiFetch('/media/tts/status');
      if (stale()) return false;
      if (!tr.ok) { this.host.notify('Could not check spoken replies (HTTP ' + tr.status + ').', true); return false; }
      st = await tr.json();
      if (stale()) return false;
    } catch { if (!stale()) this.host.notify('Could not check spoken replies.', true); return false; }
    if (!st.ready) {
      const why = !st.installed ? 'install the speech package: pip install sherpa-onnx' : 'put a voice model in ' + st.models_dir;
      this.host.notify('Spoken replies are not set up yet - ' + why, true);
      return false;
    }
    const ns = VOICE_NOISE_LEVELS[this.opt.noise];
    let stream: MediaStream;
    try {
      stream = await navigator.mediaDevices.getUserMedia({ audio: { channelCount: 1, ...ns.constraints } });
    } catch (e) {
      if (!stale()) this.host.notify('Microphone is not available: ' + ((e as Error).message || e), true);
      return false;
    }
    if (stale()) { stream.getTracks().forEach((t) => t.stop()); return false; }   // stopped while the permission prompt was open
    const AC: typeof AudioContext = window.AudioContext || (window as any).webkitAudioContext;
    const ctx = new AC();
    try {
      await ctx.resume();
      await ctx.audioWorklet.addModule(WORKLET_URL);
    } catch (e) {
      stream.getTracks().forEach((t) => t.stop());
      try { void ctx.close(); } catch { /* already closed */ }
      if (!stale()) this.host.notify('Audio could not start: ' + ((e as Error).message || e), true);
      return false;
    }
    if (stale()) {
      stream.getTracks().forEach((t) => t.stop());
      try { void ctx.close(); } catch { /* already closed */ }
      return false;
    }
    let src: MediaStreamAudioSourceNode;
    let node: AudioWorkletNode;
    let chain: ReturnType<typeof voiceFilterChain>;
    try {
      src = ctx.createMediaStreamSource(stream);
      node = new AudioWorkletNode(ctx, 'voice-frames');
      const mute = ctx.createGain();
      mute.gain.value = 0;
      chain = voiceFilterChain(ctx, src, this.opt.noise);
      chain.out.connect(node); node.connect(mute); mute.connect(ctx.destination);   // connected, so the browser keeps pulling frames
    } catch (e) {
      stream.getTracks().forEach((t) => t.stop());
      try { void ctx.close(); } catch { /* already closed */ }
      this.host.notify('Audio could not start: ' + ((e as Error).message || e), true);
      return false;
    }

    const pol = st.voice || {};
    const endMs = parseInt(localStorage.getItem('voice_end_silence_ms') || '', 10) || pol.end_silence_ms || DEFAULT_END_MS;
    this.ctx = ctx; this.stream = stream; this.node = node; this.src = src; this.chain = chain;
    this.player = new VoicePlayer(ctx);
    this.queue = new SpeakQueue(this.player, (e) => { if (this.active) this.host.notify('Speech failed: ' + (e.message || e), true); });
    this.vad = new VoiceEnergyVad(ns.vad);
    this.ep = new VoiceEndpointer({ endSilenceMs: endMs, startMs: ns.startMs });
    this.barge = new VoiceBargeDetector();
    this.ring = []; this.utter = []; this.reply = null; this.carry = ''; this.lastActivity = performance.now();
    this.pendingSend = null; this.pendingTrim = null; this.sentText = ''; this.ducked = false; this.quietFrames = 0; this.turnT = null;
    this.fillersOn = pol.filler !== false; this.fillers = { en: [], bn: [] };
    node.port.onmessage = (ev) => this.onFrame(ev.data as Float32Array);
    this.timer = window.setInterval(() => this.tickReply(), 80);

    this.active = true;
    this.setState('listening');
    // pre-load both models so the first turn does not pay the load time (failures are fine: first use reports them)
    apiFetch('/media/transcribe/warmup', { method: 'POST' }).catch(() => {});
    apiFetch('/media/tts/warmup?voice=en', { method: 'POST' }).catch(() => {});
    if (this.fillersOn) void this.loadFillers();
    return true;
  }

  // the running answer belongs to the chat this conversation started in (the active chat may have changed since)
  private ownsRun(): boolean { return this.host.isOwnSession ? this.host.isOwnSession() : true; }

  // after stop() the tick interval is gone: apply an unapplied trim once the aborted run has settled
  private flushTrimLater() {
    let tries = 0;
    const go = () => {
      const T = this.pendingTrim;
      if (!T || this.active) return;                 // nothing to do, or a new conversation took over
      if (this.host.isBusy() && ++tries < 50) { window.setTimeout(go, 100); return; }
      this.applyPendingTrim(true);
    };
    go();
  }

  // releases the microphone and every audio object; safe to call twice
  stop() {
    this.startSeq++;                            // also cancels a start() that has not finished yet
    if (!this.active) return;
    this.active = false;
    clearInterval(this.timer);
    if (this.pendingSend) { clearTimeout(this.pendingSend.timer); this.pendingSend = null; }
    try { this.queue.cancel(); } catch { /* nothing queued */ }
    if (this.sentAny && this.host.isBusy() && this.ownsRun()) this.host.abort();
    this.flushTrimLater();
    try { if (this.node) { this.node.port.onmessage = null; this.node.disconnect(); } this.src?.disconnect(); } catch { /* already disconnected */ }
    try { this.stream?.getTracks().forEach((t) => t.stop()); } catch { /* already stopped */ }
    try { void this.ctx?.close(); } catch { /* already closed */ }
    this.stream = null; this.ctx = null; this.node = null; this.src = null;
    this.reply = null;
    this.cb.onLevel(0);
    this.setState('idle');
  }

  /* ---------------- the loop ---------------- */
  private onFrame(f: Float32Array) {
    if (!this.active) return;
    this.ring.push(f);
    if (this.ring.length > PREROLL_FRAMES) this.ring.shift();
    const playing = this.queue.isSpeaking();
    const listening = this.state === 'listening';
    const voiced = this.vad.isVoiced(f, playing ? SPEAK_GATE : 1, listening && !this.ep.inSpeech);
    this.cb.onLevel(Math.min(1, this.vad.level * 12));
    // thinking and speaking are not idle time: the clock restarts when it is the person's turn again
    if (voiced || (this.state !== 'listening' && this.state !== 'hearing')) this.lastActivity = performance.now();

    if (this.state === 'listening' || this.state === 'hearing') {
      if (this.state === 'hearing') this.utter.push(f);
      const ev = this.ep.feed(voiced);
      if (ev === 'start' && this.state === 'listening') {
        this.utter = this.ring.slice();
        this.setState('hearing');
      } else if (ev === 'end' || ev === 'max') {
        void this.finishUtterance();
      } else if (ev === 'short') {
        this.utter = [];
        this.setState('listening');
      } else if (this.state === 'listening' && performance.now() - this.lastActivity > IDLE_END_MS) {
        this.host.notify('Voice conversation ended: no speech for 2 minutes.');
        this.stop();
        this.cb.onEnded();
      }
    } else {                                    // thinking or speaking: did the person start talking?
      if (playing) {                            // duck at the first sign of a voice, restore if it was only a noise
        if (voiced && !this.ducked) { this.player.duck(true); this.ducked = true; this.quietFrames = 0; }
        else if (!voiced && this.ducked && ++this.quietFrames >= 8) { this.player.duck(false); this.ducked = false; }
      }
      if (this.barge.feed(voiced)) this.bargeIn();
    }
  }

  // text that was sent (or about to be) but the person was not done: it joins what they say next
  private takeBackPending(): boolean {
    if (this.pendingSend) {
      clearTimeout(this.pendingSend.timer);
      this.carry = this.pendingSend.text;
      this.pendingSend = null;
      return true;
    }
    const R = this.reply;
    if (R && !R.said && performance.now() - R.sentAt < MERGE_WINDOW_MS && this.sentText) {
      this.carry = this.sentText;
      return true;
    }
    return false;
  }

  private bargeIn() {
    const R = this.reply;
    const mergeBack = this.takeBackPending();
    // keep only what was heard in the chat history (applied once the aborted run has settled)
    if (!mergeBack && R && R.msg && R.said) {
      this.pendingTrim = { msg: R.msg, text: voiceHeardText(R.lastRaw, R.ends, this.player.startedCount()) };
    }
    this.queue.cancel();
    this.ducked = false;
    if (this.host.isBusy() && this.ownsRun()) this.host.abort();
    this.reply = null;
    this.barge.reset();
    this.utter = this.ring.slice();
    this.ep.reset();
    this.ep.inSpeech = true; this.ep.voicedFrames = PREROLL_FRAMES; this.ep.frames = PREROLL_FRAMES;
    this.setState('hearing');
  }

  private async finishUtterance() {
    const frames = this.utter;
    this.utter = [];
    this.barge.reset();
    this.setState('thinking');
    const turn = ++this.turnId;
    const endOfSpeech = performance.now();
    // whisper invents words from a long silent tail: keep only ~190 ms of it
    const keepN = frames.length - Math.max(0, this.ep.trailingSilence() - 6);
    const wav = new Blob([audioWavBytes(concatFrames(frames.slice(0, Math.max(1, keepN))), 16000)], { type: 'audio/wav' });
    let text = '';
    try {
      text = await transcribe(wav, this.opt.lang);
    } catch (e) {
      this.host.notify('Could not hear that: ' + ((e as Error).message || e), true);
      // only this turn may put the loop back to listening (not a newer one, not someone who is talking again)
      if (this.active && turn === this.turnId && this.state === 'thinking') {
        this.setState('listening');
        if (this.carry) { const c = this.carry; this.carry = ''; this.sendToAgent(c); }   // the earlier question still gets asked
      }
      return;
    }
    if (!this.active) return;
    if (turn !== this.turnId) {                                       // a newer utterance overtook this one: keep its words
      if (text && !voiceIsHallucination(text)) this.carry = (this.carry ? this.carry + ' ' : '') + text;
      return;
    }
    if (voiceIsHallucination(text)) {
      if (this.state === 'thinking') {
        this.setState('listening');
        if (this.carry) { const c = this.carry; this.carry = ''; this.sendToAgent(c); }   // nothing new was said: ask what was held back
      }
      return;
    }
    if (this.carry) { text = this.carry + ' ' + text; this.carry = ''; }
    if (this.state === 'hearing') { this.carry = text; return; }      // they kept talking: join it to the next utterance
    this.turnT = { endOfSpeech, transcript: performance.now(), firstToken: 0, firstAudio: 0 };
    this.cb.onHeard(text);
    if (voiceLooksUnfinished(text)) {                                 // "...and", "...but", "um": give them a moment to go on
      this.pendingSend = {
        text, timer: window.setTimeout(() => { const p = this.pendingSend; this.pendingSend = null; if (p && this.active) this.sendToAgent(p.text); }, EXTRA_WAIT_MS),
      };
      return;
    }
    this.sendToAgent(text);
  }

  private sendToAgent(text: string) {
    this.sentAny = true;
    if (this.host.isBusy() && this.ownsRun()) this.host.abort();
    this.sentText = text;
    const now = performance.now();
    const known = new Set<string>();             // messages that exist now: the reply is a message that is not among them
    for (let i = this.host.messageCount() - 1; i >= 0; i--) { const m = this.host.messageAt(i); if (m) known.add(m.id); }
    const t = this.turnT;
    this.turnT = null;
    this.reply = {
      known, msgId: '', msg: null, text, t, spoken: 0, first: true, lastRaw: '', ends: [], said: false, sawBusy: false,
      quiet: 0, startedAt: now, sentAt: now, permNoted: false, errNoted: false, fillerDone: false, lang: voiceFillerLang(text),
    };
    if (t) this.player.onFirstSound = () => { t.firstAudio = performance.now(); };
    this.host.send(text);
    this.setState('thinking');
  }

  // the assistant message that answers `R.text`: the first assistant message after the user message with that
  // text, found from the end and not one that existed before sending (the list can be replaced or re-fetched)
  private findReply(R: Reply): VoiceHostMessage | undefined {
    if (R.msgId) {
      for (let i = this.host.messageCount() - 1; i >= 0; i--) { const m = this.host.messageAt(i); if (m && m.id === R.msgId) return m; }
      return undefined;
    }
    const want = R.text.trim();
    let next: VoiceHostMessage | undefined;
    for (let i = this.host.messageCount() - 1; i >= 0; i--) {
      const m = this.host.messageAt(i);
      if (!m) { next = undefined; continue; }
      if (m.role === 'user' && m.content.trim() === want && next && next.role === 'assistant' && !R.known.has(next.id)) return next;
      next = m;
    }
    return undefined;
  }

  private speakNow(text: string) { this.queue.push(text, null); }

  // a short "Mm-hm." while a slow answer is still being worked out (pre-recorded at start, played at most once per turn)
  private maybeFiller(R: Reply) {
    if (R.fillerDone || R.said || !this.fillersOn || performance.now() - R.sentAt < FILLER_AFTER_MS) return;
    const list = this.fillers[R.lang] || [];
    if (!list.length) return;
    R.fillerDone = true;
    this.player.schedule(list[Math.floor(Math.random() * list.length)], null);
    if (this.state === 'thinking') this.setState('speaking');
  }

  private async loadFillers() {
    for (const lang of ['en', 'bn'] as const) {
      for (const phrase of VOICE_FILLERS[lang].slice(0, lang === 'en' ? 3 : 2)) {
        if (!this.active || !this.ctx) return;
        try {
          const ab = await speak(phrase);
          this.fillers[lang].push(await this.ctx.decodeAudioData(ab.slice(0)));
        } catch { /* a missing voice just means no filler in that language */ }
      }
    }
  }

  private applyPendingTrim(force = false) {
    const T = this.pendingTrim;
    if (!T || (!force && this.host.isBusy())) return;
    this.pendingTrim = null;
    this.host.trimMessage(T.msg.id, T.text || '(interrupted)');
    this.host.persistTrim?.(T.msg.id, T.text || '(interrupted)');
  }

  private tickReply() {
    this.applyPendingTrim();
    const R = this.reply;
    if (!R || !this.active) return;
    const busy = this.host.isBusy();
    if (busy) R.sawBusy = true;
    // the agent is waiting for an approval card: never answered by voice, only a heads-up
    if (!R.permNoted && this.host.permissionPending()) {
      R.permNoted = true;
      this.speakNow('I need your approval. Please check the screen.');
      if (this.state === 'thinking') this.setState('speaking');
    }
    // re-read each tick: the store replaces the message object as it streams (found by id once known)
    const found = this.findReply(R);
    if (found) { R.msg = found; R.msgId = found.id; }
    else if (!R.msg) {
      // give up 15 s after the run first started and ended without a message, or 60 s if nothing ever started
      const now = performance.now();
      if (busy && !R.busyAt) R.busyAt = now;
      const gaveUp = !busy && (R.busyAt ? now - R.busyAt > 15000 : now - R.startedAt > 60000);
      if (gaveUp) { this.reply = null; this.setState('listening'); }
      return;
    }
    const m = R.msg!;
    const raw = voiceStripThink(m.content || '');
    if (R.t && !R.t.firstToken && raw) R.t.firstToken = performance.now();
    // the draft was replaced (answer check / rewrite): drop what was queued and start over
    if (R.lastRaw && R.spoken && !raw.startsWith(R.lastRaw.slice(0, R.spoken))) {
      this.queue.cancel(); R.spoken = 0; R.first = true; R.ends = [];
    }
    R.lastRaw = raw;
    const finished = R.sawBusy && !busy;
    R.quiet = finished ? R.quiet + 1 : 0;
    const final = finished && R.quiet >= 3;                    // stable for ~250 ms after the run ended
    if (m.errorAlert && final && !R.errNoted) { R.errNoted = true; this.speakNow('Sorry, something went wrong.'); }
    const r = voiceTakeSpeakable(raw, R.spoken, { final, first: R.first });
    r.pieces.forEach((p, i) => {
      R.ends.push(r.ends[i]);
      this.queue.push(p, r.ends[i]);
      R.first = false;
      R.said = true;
    });
    R.spoken = r.spoken;
    if (!R.said) this.maybeFiller(R);
    if (r.pieces.length && this.state === 'thinking') this.setState('speaking');
    if (final && R.spoken >= raw.length && !this.queue.isSpeaking()) {
      if (R.t) {
        const t = R.t, ms = (a: number, b: number) => (a && b ? Math.round(a - b) : null);
        console.debug('[voice] turn', {
          transcribeMs: ms(t.transcript, t.endOfSpeech), firstTokenMs: ms(t.firstToken, t.endOfSpeech),
          firstAudioMs: ms(t.firstAudio, t.endOfSpeech), totalMs: Math.round(performance.now() - t.endOfSpeech),
        });
      }
      this.reply = null;
      this.barge.reset();
      this.setState('listening');
    } else if (this.state === 'speaking' && !this.queue.isSpeaking() && !final) {
      this.setState('thinking');                               // between sentences while the model is still writing
    }
  }
}

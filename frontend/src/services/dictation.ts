// Live dictation: interim text while the person is still talking (port of the streaming part of static/js/media.js).
// The microphone is tapped as 16 kHz mono PCM (the voice worklet) and shipped as 4 s windows with a re-sent tail to
// /media/transcribe_stream/*; every chunk answer carries the joined `interim` text so far and /finish the final `text`.
// The MediaRecorder in the composer keeps running untouched, so a failure here only means "no live text": the caller
// then transcribes the whole recording as before.
import { apiFetch } from './api';
import { audioWavBytes, STT_RATE } from './voice/wav';
import workletUrl from './voice/voice-worklet.js?url';

const WIN_S = 4;
const OVERLAP_S = 0.75;      // re-sent tail so a cut never eats a word
const KEEP_S = 8;            // PCM seconds kept in RAM; older audio is dropped, never queued
const MIN_TAIL_S = 0.5;      // shorter remainders are not worth a request
const FINISH_TIMEOUT_MS = 30000;
const SILENCE_PEAK = 0.02;   // windows quieter than this are room noise, not speech

export interface LiveDictation {
  /** Ships the last window and returns the joined final transcript. Throws when the live path broke. */
  finish(): Promise<string>;
  /** Stops capturing and drops the server session (also the cleanup on unmount). */
  cancel(): void;
  /** True once any request failed: the live text is no longer trustworthy. */
  readonly dead: boolean;
}

async function postJson(path: string, body: unknown): Promise<any> {
  const r = await apiFetch(path, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) });
  const j = await r.json().catch(() => ({}));
  if (!r.ok) throw new Error(j.error || 'HTTP ' + r.status);
  return j;
}

/**
 * Starts the live path on an already open microphone stream. Rejects when it cannot start (no worklet, no server
 * session), in which case nothing is left running. `onInterim` gets the whole interim transcript on every window.
 */
export async function startLiveDictation(stream: MediaStream, language: string, onInterim: (text: string) => void): Promise<LiveDictation> {
  const AC: typeof AudioContext | undefined = window.AudioContext || (window as any).webkitAudioContext;
  if (!AC || !window.AudioWorkletNode) throw new Error('live dictation is not supported here');
  const ctx = new AC();
  let node: AudioWorkletNode | null = null;
  let src: MediaStreamAudioSourceNode | null = null;
  let streamId = '';
  let ended = false;
  const close = () => {
    ended = true;
    clearInterval(timer);
    try { if (node) { node.port.onmessage = null; node.disconnect(); } src?.disconnect(); } catch { /* already disconnected */ }
    try { void ctx.close(); } catch { /* already closed */ }
  };
  let timer = 0;
  try {
    await ctx.resume();
    await ctx.audioWorklet.addModule(workletUrl);
    src = ctx.createMediaStreamSource(stream);
    node = new AudioWorkletNode(ctx, 'voice-frames');
    const mute = ctx.createGain();
    mute.gain.value = 0;
    src.connect(node); node.connect(mute); mute.connect(ctx.destination);   // connected, so the browser keeps pulling frames
    const sj = await postJson('/media/transcribe_stream/start', { language });
    if (!sj.stream_id) throw new Error('no stream');
    streamId = sj.stream_id;
  } catch (e) {
    close();
    throw e;
  }

  // newest PCM (16 kHz frames of 512), trimmed to KEEP_S; `total`/`consumed` count samples ever received / inspected
  let frames: Float32Array[] = [];
  let have = 0;
  let total = 0;
  let consumed = 0;
  node!.port.onmessage = (ev) => {
    const f = ev.data as Float32Array;
    frames.push(f); have += f.length; total += f.length;
    const keep = STT_RATE * KEEP_S;
    while (frames.length > 1 && have - frames[0].length >= keep) have -= frames.shift()!.length;
  };

  /** Next window as WAV bytes, or null when there is nothing new worth sending (older audio is dropped, not queued). */
  const takeWindow = (force: boolean): Uint8Array<ArrayBuffer> | null => {
    const fresh = total - consumed;
    if (fresh <= 0) return null;
    if (!force && fresh < STT_RATE * MIN_TAIL_S) return null;
    const n = Math.min(have, fresh + Math.floor(STT_RATE * OVERLAP_S), Math.floor(STT_RATE * (WIN_S + OVERLAP_S)));
    const all = new Float32Array(have);
    let o = 0;
    for (const f of frames) { all.set(f, o); o += f.length; }
    consumed = total;   // inspected: never re-sent, shipped or not
    if (n <= 0) return null;
    const pcm = all.subarray(all.length - n);
    let peak = 0;
    for (let i = 0; i < pcm.length; i += 7) { const a = Math.abs(pcm[i]); if (a > peak) peak = a; }
    if (peak < SILENCE_PEAK) return null;
    return audioWavBytes(pcm, STT_RATE);
  };

  let dead = false;
  let seq = 0;
  let chain: Promise<void> = Promise.resolve();
  const queueWindow = (force: boolean) => {
    if (dead || ended) return;
    const bytes = takeWindow(force);
    if (!bytes) return;
    const n = seq++;
    chain = chain.then(async () => {
      if (dead) return;
      const fd = new FormData();
      fd.append('file', new Blob([bytes], { type: 'audio/wav' }), 'win.wav');
      fd.append('stream_id', streamId);
      fd.append('seq', String(n));
      const r = await apiFetch('/media/transcribe_stream/chunk', { method: 'POST', body: fd });
      const j = await r.json().catch(() => ({}));
      if (!r.ok) throw new Error(j.error || 'HTTP ' + r.status);
      if (!dead && !ended) onInterim(String(j.interim || ''));
    }).catch(() => { dead = true; if (!ended) onInterim(''); });
  };

  // a timer, not requestAnimationFrame: it keeps shipping windows while the tab is in the background
  timer = window.setInterval(() => queueWindow(false), WIN_S * 1000);

  return {
    get dead() { return dead; },
    async finish() {
      if (dead) throw new Error('live dictation failed');
      queueWindow(true);   // flush the tail (< 1 window)
      const stopCapture = close;
      try {
        const work = (async () => {
          await chain;
          if (dead) throw new Error('live dictation failed');
          const fin = await postJson('/media/transcribe_stream/finish', { stream_id: streamId });
          return String(fin.text || '').trim();
        })();
        work.catch(() => { /* reported through the race */ });
        let tm = 0;
        const timeout = new Promise<never>((_, rej) => { tm = window.setTimeout(() => rej(new Error('live dictation timed out')), FINISH_TIMEOUT_MS); });
        try { return await Promise.race([work, timeout]); } finally { clearTimeout(tm); }
      } finally {
        stopCapture();
      }
    },
    cancel() {
      const was = ended;
      close();
      if (!was && streamId) void postJson('/media/transcribe_stream/cancel', { stream_id: streamId }).catch(() => { /* session expires on its own */ });
    },
  };
}

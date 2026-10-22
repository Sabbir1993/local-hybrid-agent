// Images, videos and audio files (port of static/js/media.js and static/js/video-frames.js).
// Server side: routes/media.py. Each feature is available once a model is set up for it in Settings -> Models & Jobs.
// /image and /video run as a job (progress card in the chat, result saved with the conversation); audio and video
// attachments become text (transcript / described frames) that goes into the prompt as a FILE block.
import { apiFetch, api } from './api';
import { useAppStore, getActiveSessionKey, type ChatMessage, externalBusy, sessionKey } from '../stores/useAppStore';

export type MediaKind = 'image' | 'video';

export const MEDIA_SETUP_URL = '/settings#sec-lanes';
const MEDIA_WORD: Record<MediaKind, string> = { image: 'image', video: 'video' };

export interface MediaStatus {
  image?: any;
  video?: any;
  transcribe?: any;
  video_read?: { ffmpeg?: boolean; max_mb?: number; max_frames?: number; max_minutes?: number };
  max_audio_mb?: number;
  [k: string]: any;
}

let statusCache: MediaStatus | null = null;
let statusAt = 0;

export async function loadMediaStatus(): Promise<MediaStatus | null> {
  try {
    const r = await apiFetch('/media/status');
    if (r.ok) { statusCache = await r.json(); statusAt = Date.now(); }
  } catch (_) {
    // keep the last known status
  }
  return statusCache;
}

/** The status, re-read at most every 30 s (the slash menu asks on every keystroke). */
export async function cachedMediaStatus(): Promise<MediaStatus | null> {
  return statusCache && Date.now() - statusAt < 30000 ? statusCache : loadMediaStatus();
}

export function mediaReady(key: string, st: MediaStatus | null = statusCache): boolean {
  return !!(st && st[key] && st[key].ready);
}

export function whereText(st: any): string {
  if (!st) return '';
  return st.cloud ? `${st.model} (${st.provider}, cloud)` : `${st.model} (on this PC)`;
}

/** Same guidance as the old UI's setup toast. */
export function setupMessage(what: string): string {
  return `${what} isn't set up yet. Add a model in Settings → Models & Jobs.`;
}

export const fmtClock = (s: number): string => {
  s = Math.max(0, Math.floor(s));
  return `${Math.floor(s / 60)}:${String(s % 60).padStart(2, '0')}`;
};

function sttLang(): string {
  try { return localStorage.getItem('stt_lang') || 'auto'; } catch (_) { return 'auto'; }
}

export interface MediaOpts { aspect: string; seconds: number; size?: string; seed?: number; mode?: string; strength?: number }

// image size (models on this PC). Mirrors core/media.py size_for: same pixels, shape changes
export const MEDIA_SIZES: Record<string, number> = { small: 512, medium: 768, large: 1024, xlarge: 1280 };
export const MEDIA_SIZE_WORD: Record<string, string> = { small: 'Small', medium: 'Medium', large: 'Large', xlarge: 'Extra large' };
const SHAPE_R: Record<string, number> = { square: 1, wide: 1.5, tall: 2 / 3 };
const m32 = (v: number) => Math.max(64, Math.floor(v / 32) * 32);
export function mediaSizeFor(size: string, aspect: string): [number, number] {
  const side = MEDIA_SIZES[size] || 1024, r = SHAPE_R[aspect] || 1;
  return [m32(side * Math.sqrt(r)), m32(side / Math.sqrt(r))];
}

/** The options row's choices, kept in the same localStorage keys as the old UI (media_opts_image / media_opts_video). */
export function mediaOpts(kind: MediaKind): MediaOpts {
  let o: any = {};
  try { o = JSON.parse(localStorage.getItem('media_opts_' + kind) || '{}') || {}; } catch (_) {}
  const out: MediaOpts = { aspect: o.aspect || (kind === 'video' ? 'wide' : 'square'), seconds: o.seconds || 4 };
  if (kind === 'image' && MEDIA_SIZES[o.size]) out.size = o.size;   // unset = the model's default
  return out;
}

export function saveMediaOpts(kind: MediaKind, patch: Partial<MediaOpts>): MediaOpts {
  const o: any = { ...mediaOpts(kind), ...patch };
  if (!o.size) delete o.size;
  try { localStorage.setItem('media_opts_' + kind, JSON.stringify(o)); } catch (_) {}
  return o;
}

export const mediaStatusNow = (): MediaStatus | null => statusCache;

// ---------------------------------------------------------------- pictures to start from, composer mode
// Local only: the server sends them to the image model on this PC, never to the cloud.

type Listener = () => void;
function tinyStore<T>(get: () => T) {
  const subs = new Set<Listener>();
  let snap = get();
  return {
    subscribe: (fn: Listener) => { subs.add(fn); return () => { subs.delete(fn); }; },
    snapshot: () => snap,
    emit: () => { snap = get(); subs.forEach((f) => f()); },
  };
}

export interface EditPic { path: string; name: string }
let editPicList: EditPic[] = [];   // from "Edit this picture" on a result
export const editPicsStore = tinyStore<EditPic[]>(() => editPicList);
export function setEditPics(list: EditPic[]): void { editPicList = list; editPicsStore.emit(); }

const readPref = (k: string, d: string): string => { try { return localStorage.getItem(k) || d; } catch (_) { return d; } };
const writePref = (k: string, v: string): void => { try { localStorage.setItem(k, v); } catch (_) {} };

/** What Send does: 'chat', or make an image from the typed text (old UI's "Chat / Image" select, key comp_mode). */
let compModeVal: 'chat' | 'image' = readPref('comp_mode', 'chat') === 'image' ? 'image' : 'chat';
export const compModeStore = tinyStore<'chat' | 'image'>(() => compModeVal);
export function setCompMode(v: 'chat' | 'image'): void {
  compModeVal = v; writePref('comp_mode', v);
  if (v === 'chat' && editPicList.length) setEditPics([]);   // leaving Image mode drops a picture kept for editing
  compModeStore.emit();
}
export const imageComposeMode = (): boolean => compModeVal === 'image' && mediaReady('image');

export interface PicCaps { img2img: boolean; refs: boolean; max_refs: number }
export function picCaps(st: MediaStatus | null = statusCache): PicCaps {
  return (st && st.image && st.image.edit) || { img2img: false, refs: false, max_refs: 0 };
}
export const PIC_OK = /^image\/(png|jpe?g|webp)$/i;
export const picStrength = (): number => {
  const v = parseFloat(readPref('media_pic_strength', '0.6'));
  return v >= 0.05 && v <= 1 ? v : 0.6;
};
export const setPicStrength = (v: number): void => writePref('media_pic_strength', String(v));
export const setPicMode = (v: string): void => writePref('media_pic_mode', v);
/** img2img = change one picture, edit = combine / edit with references. */
export function picMode(n: number, caps: PicCaps = picCaps()): 'img2img' | 'edit' {
  if (n > 1) return 'edit';
  const want = readPref('media_pic_mode', '');
  if (want === 'edit' && caps.refs) return 'edit';
  if (want === 'img2img' && caps.img2img) return 'img2img';
  return caps.refs ? 'edit' : 'img2img';
}

/** The composer attachments the image model can use as a starting picture. */
interface PicAtt { id: string; name: string; isImage?: boolean; b64?: string; mime?: string; dataUrl?: string }
export interface MediaPics { inputs: { path?: string; b64?: string }[]; mode: 'img2img' | 'edit'; strength?: number; count: number; used: string[] }
export const picAttachments = (list: PicAtt[]): PicAtt[] => list.filter((a) => a.isImage && a.b64);

/** make an upload small (the server re-checks and re-encodes everything anyway) */
function shrinkPic(dataUrl: string, mime: string): Promise<string> {
  return new Promise((res, rej) => {
    const raw = () => res(String(dataUrl).split(',')[1] || '');
    const img = new Image();
    img.onload = () => {
      const s = Math.min(1, 1536 / Math.max(img.naturalWidth, img.naturalHeight));
      if (s >= 1) return raw();
      const c = document.createElement('canvas');
      c.width = Math.max(1, Math.round(img.naturalWidth * s));
      c.height = Math.max(1, Math.round(img.naturalHeight * s));
      c.getContext('2d')!.drawImage(img, 0, 0, c.width, c.height);
      res(c.toDataURL(/png|webp/i.test(mime) ? 'image/png' : 'image/jpeg', 0.92).split(',')[1] || '');
    };
    img.onerror = () => rej(new Error('picture could not be decoded'));   // never send a raw, possibly huge, undecodable file
    img.src = dataUrl;
  });
}

/** The pictures for this request: null = none, a string = why they cannot be used. */
export async function takeMediaPics(atts: PicAtt[]): Promise<MediaPics | string | null> {
  const pics = picAttachments(atts);
  if (!pics.length && !editPicList.length) return null;
  if (pics.some((a) => !PIC_OK.test(a.mime || ''))) return 'Only PNG, JPEG or WEBP pictures can be used.';
  const mode = picMode(pics.length + editPicList.length);
  const inputs: MediaPics['inputs'] = editPicList.map((p) => ({ path: p.path }));
  for (const a of pics) {
    try { inputs.push({ b64: await shrinkPic(a.dataUrl || '', a.mime || '') }); }
    catch (_) { return `"${a.name}" could not be read as a picture. Remove it or use another one.`; }
  }
  return { inputs, mode, strength: mode === 'img2img' ? picStrength() : undefined, count: inputs.length, used: pics.map((a) => a.id) };
}

// ---------------------------------------------------------------- audio -> text

const STT_RATE = 16000;

/** Decode any audio the browser can play and wrap it as 16 kHz mono 16-bit WAV (Whisper's input). */
async function audioBlobToSttWav(blob: Blob): Promise<{ wav: Blob; seconds: number }> {
  const Ctx = (window as any).AudioContext || (window as any).webkitAudioContext;
  const ctx = new Ctx();
  try {
    const decoded: AudioBuffer = await ctx.decodeAudioData(await blob.arrayBuffer());
    const n = decoded.length;
    const mono = new Float32Array(n);
    for (let c = 0; c < decoded.numberOfChannels; c++) {
      const ch = decoded.getChannelData(c);
      for (let i = 0; i < n; i++) mono[i] += ch[i] / decoded.numberOfChannels;
    }
    // box pre-filter when shrinking (48 kHz -> 16 kHz), linear interpolation otherwise
    const ratio = decoded.sampleRate / STT_RATE;
    const outLen = ratio === 1 ? n : Math.max(0, Math.floor(n / ratio));
    const out = ratio === 1 ? mono : new Float32Array(outLen);
    if (ratio !== 1) {
      const win = ratio > 1 ? Math.floor(ratio) : 1;
      for (let i = 0; i < outLen; i++) {
        const pos = i * ratio;
        if (win > 1) {
          let s = 0, cnt = 0;
          const start = Math.floor(pos);
          for (let k = 0; k < win && start + k < n; k++) { s += mono[start + k]; cnt++; }
          out[i] = cnt ? s / cnt : 0;
        } else {
          const i0 = Math.floor(pos), i1 = Math.min(i0 + 1, n - 1);
          const f = pos - i0;
          out[i] = mono[i0] * (1 - f) + mono[i1] * f;
        }
      }
    }
    const buf = new ArrayBuffer(44 + outLen * 2);
    const v = new DataView(buf);
    const str = (off: number, s: string) => { for (let i = 0; i < s.length; i++) v.setUint8(off + i, s.charCodeAt(i)); };
    str(0, 'RIFF'); v.setUint32(4, 36 + outLen * 2, true); str(8, 'WAVE');
    str(12, 'fmt '); v.setUint32(16, 16, true); v.setUint16(20, 1, true); v.setUint16(22, 1, true);
    v.setUint32(24, STT_RATE, true); v.setUint32(28, STT_RATE * 2, true); v.setUint16(32, 2, true); v.setUint16(34, 16, true);
    str(36, 'data'); v.setUint32(40, outLen * 2, true);
    for (let i = 0; i < outLen; i++) {
      const s = Math.max(-1, Math.min(1, out[i]));
      v.setInt16(44 + i * 2, s < 0 ? s * 0x8000 : s * 0x7fff, true);
    }
    return { wav: new Blob([buf], { type: 'audio/wav' }), seconds: decoded.duration };
  } finally {
    try { ctx.close(); } catch (_) {}
  }
}

/** Any audio Blob/File -> transcript text (throws Error with a plain message). */
export async function transcribeBlob(blob: Blob, language?: string): Promise<string> {
  let conv;
  try {
    conv = await audioBlobToSttWav(blob);
  } catch (_) {
    throw new Error('this audio format can’t be read by the browser - try mp3, m4a, wav or ogg');
  }
  const cap = ((statusCache && statusCache.max_audio_mb) || 25) * 1024 * 1024;
  if (conv.wav.size > cap) {
    throw new Error(`it's too long (${Math.round(conv.seconds / 60)} min) - the limit is about ${Math.floor(cap / 32000 / 60)} minutes`);
  }
  const fd = new FormData();
  fd.append('file', conv.wav, 'audio.wav');
  fd.append('language', language || sttLang());
  const r = await apiFetch('/media/transcribe', { method: 'POST', body: fd });
  const j = await r.json().catch(() => ({}));
  if (!r.ok) throw new Error(j.error || 'HTTP ' + r.status);
  return String(j.text || '').trim();
}

/** An audio file attachment: its transcript, as the text of a FILE block. Throws when it cannot be used at all. */
export async function audioAttachmentContent(f: File): Promise<string> {
  if (!statusCache || !mediaReady('transcribe')) await loadMediaStatus();
  if (!mediaReady('transcribe')) throw new Error(setupMessage('Speech to text (for audio files)'));
  if (f.size > 200 * 1024 * 1024) throw new Error(`"${f.name}" is too large (200 MB max).`);
  const name = f.name || 'audio';
  try {
    const text = await transcribeBlob(f);
    return `(transcript of the audio file ${name})\n${text || '(no speech found)'}`;
  } catch (e: any) {
    return `(could not transcribe ${name}: ${e.message})`;
  }
}

// ---------------------------------------------------------------- video -> text

interface VideoFrame { t: number; mime: string; b64: string }
interface VideoRead { duration: number; frames: VideoFrame[]; transcript?: string | null; transcript_note?: string }

/** 75.4 -> "1:15" */
const videoClock = (t: number): string => {
  const s = Math.max(0, Math.round(t));
  return `${Math.floor(s / 60)}:${String(s % 60).padStart(2, '0')}`;
};

/** Evenly spaced capture times, never the very first/last instant (often black). */
function videoFrameTimes(duration: number, maxFrames: number): number[] {
  const n = Math.max(1, Math.min(maxFrames, Math.floor(duration / 3) + 1));
  const out: number[] = [];
  for (let i = 0; i < n; i++) out.push(Math.round(((duration * (i + 0.5)) / n) * 10) / 10);
  return out;
}

/** The fallback when the server has no ffmpeg: seek a hidden <video> and snapshot each time to a JPEG. Nothing is uploaded. */
async function videoFramesInBrowser(file: File, maxFrames: number, maxMinutes: number, signal?: AbortSignal): Promise<VideoRead> {
  const url = URL.createObjectURL(file);
  const v = document.createElement('video');
  v.muted = true; v.preload = 'auto'; v.playsInline = true;
  const cancelled = () => new DOMException('cancelled', 'AbortError');
  try {
    await new Promise<void>((res, rej) => {
      if (signal?.aborted) return rej(cancelled());
      signal?.addEventListener('abort', () => rej(cancelled()), { once: true });
      v.onloadedmetadata = () => res();
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
    const ctx = c.getContext('2d')!;
    const frames: VideoFrame[] = [];
    for (const t of videoFrameTimes(dur, maxFrames)) {
      await new Promise<void>((res, rej) => {
        if (signal?.aborted) return rej(cancelled());
        const target = Math.min(t, Math.max(0, dur - 0.1));
        // "seeked" never fires when the time is already the target; a stuck seek must not hang the chip forever
        const tm = window.setTimeout(() => { v.onseeked = null; rej(new Error('the video did not respond while reading it')); }, 5000);
        const onAbort = () => { clearTimeout(tm); rej(cancelled()); };
        signal?.addEventListener('abort', onAbort, { once: true });
        const done = () => { clearTimeout(tm); signal?.removeEventListener('abort', onAbort); };
        v.onseeked = () => { done(); res(); };
        v.onerror = () => { done(); rej(new Error('the video stopped decoding part-way')); };
        if (Math.abs(v.currentTime - target) < 0.001 && v.readyState >= 2) { done(); res(); return; }
        v.currentTime = target;
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

/** A small JPEG of the first frame (about 480 px), shown on the attachment chip. */
async function videoPoster(frame: VideoFrame): Promise<string> {
  const full = `data:${frame.mime};base64,${frame.b64}`;
  try {
    const img = await new Promise<HTMLImageElement>((res, rej) => {
      const i = new Image();
      i.onload = () => res(i);
      i.onerror = () => rej(new Error('poster'));
      i.src = full;
    });
    const k = Math.min(1, 480 / Math.max(img.width, img.height));
    const c = document.createElement('canvas');
    c.width = Math.max(2, Math.round(img.width * k));
    c.height = Math.max(2, Math.round(img.height * k));
    c.getContext('2d')!.drawImage(img, 0, 0, c.width, c.height);
    return c.toDataURL('image/jpeg', 0.7);
  } catch (_) {
    return full;
  }
}

/** The text that goes into the prompt: frame notes plus the optional audio transcript. */
function videoPromptText(name: string, duration: number, notes: { t: number; text: string }[], transcript?: string | null, transcriptNote?: string): string {
  const head = `(video file ${name}, ${videoClock(duration)} long, ${notes.length} picture${notes.length === 1 ? '' : 's'} sampled. ` +
    'These are descriptions of still frames by an image reader, not true video understanding: motion and short events between frames are missed.)';
  const lines = notes.map((n) => `[${videoClock(n.t)}] ${n.text}`);
  const audio = transcript ? '\n(audio transcript)\n' + transcript
    : transcriptNote ? '\n(audio not transcribed: ' + transcriptNote + ')' : '';
  return head + '\n' + lines.join('\n') + audio;
}

/** A video attachment: sampled frames described one by one (and the audio transcribed) as the text of a FILE block. */
export async function videoAttachmentContent(f: File, onStep?: (text: string) => void, onPoster?: (url: string) => void, signal?: AbortSignal): Promise<string> {
  if (!statusCache) await loadMediaStatus();
  const vr = (statusCache && statusCache.video_read) || {};
  const capMb = vr.max_mb || 100;
  const name = f.name || 'video';
  if (f.size > capMb * 1024 * 1024) throw new Error(`"${name}" is too large (${capMb} MB max for videos).`);
  try {
    onStep?.('getting pictures…');
    let r: VideoRead;
    if (vr.ffmpeg) {
      // the server samples the frames (ffmpeg) and transcribes the audio
      const fd = new FormData();
      fd.append('file', f, name);
      const res = await apiFetch('/media/video', { method: 'POST', body: fd });
      r = await res.json().catch(() => ({}));
      if (!res.ok) throw new Error((r as any).error || 'HTTP ' + res.status);
    } else {
      r = await videoFramesInBrowser(f, vr.max_frames || 16, vr.max_minutes || 10, signal);
      r.transcript = null;
      if (mediaReady('transcribe')) {
        try { r.transcript = (await transcribeBlob(f)) || null; } catch (e: any) { r.transcript_note = e.message; }
      }
    }
    if (r.frames[0]) onPoster?.(await videoPoster(r.frames[0]));   // the chip shows the first frame
    const notes: { t: number; text: string }[] = [];
    let failed = 0;
    for (let i = 0; i < r.frames.length; i++) {
      if (signal?.aborted) throw new DOMException('cancelled', 'AbortError');   // the chip was removed
      const fr = r.frames[i];
      onStep?.(`reading picture ${i + 1}/${r.frames.length}…`);
      let text: string;
      try {
        const res = await apiFetch('/agent/vision', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({
            image_b64: fr.b64,
            mime: fr.mime,
            question: `This is a frame at ${videoClock(fr.t)} of a video. Describe what is visible, including any readable text. Be concise.`,
          }),
          signal,
        });
        const vj = await res.json().catch(() => ({}));
        if (!res.ok) throw new Error(vj.error || 'HTTP ' + res.status);
        text = String(vj.description || '').trim() || '(nothing described)';
      } catch (e: any) {
        if (e?.name === 'AbortError') throw e;
        failed++;
        text = `(could not read this picture: ${e.message})`;
      }
      notes.push({ t: fr.t, text });
      // the first three pictures all failed: the image reader is down, stop instead of grinding on
      if (failed >= 3 && failed === notes.length) throw new Error(notes[0].text.replace(/^\(could not read this picture: |\)$/g, ''));
    }
    return videoPromptText(name, r.duration, notes, r.transcript, r.transcript_note);
  } catch (e: any) {
    if (e?.name === 'AbortError') return '';
    return `(could not read the video ${name}: ${e.message})`;
  }
}

// ---------------------------------------------------------------- /image and /video jobs
// The card's state lives in the assistant message's meta.media, so the chat renders it like any other message and a
// reload shows the saved result. Live-only bits (job id, session) stay in `live` below.

export interface MediaMeta {
  kind: MediaKind;
  prompt: string;
  opts?: any;
  state: 'starting' | 'running' | 'confirm' | 'not_loaded' | 'done' | 'error' | 'cancelled';
  text?: string;
  pct?: number | null;
  t0?: number;
  model?: string;
  where?: string;
  secs?: number;
  error?: string;
  notSetup?: boolean;
  files?: { path: string; name?: string; mime?: string }[];
  provider?: string;       // confirm: the cloud provider that may charge
  cloudModel?: string;
  lane?: string;           // not_loaded: the local model that has to be loaded
  laneLabel?: string;
  canLoad?: boolean;
  loading?: boolean;
}

interface LiveJob { kind: MediaKind; prompt: string; sid: string | number | null; jobId?: string; saved: boolean; ctrl?: AbortController; pics?: MediaPics }
const live = new Map<string, LiveJob>();

export const rawUrl = (path: string): string => '/agent/raw?path=' + encodeURIComponent(path);

/** Patch one message's media card, wherever the message is (open chat or a parked one). */
function patchMedia(msgId: string, patch: Partial<MediaMeta>, content?: string, extra?: Record<string, any>): void {
  const upd = (m: ChatMessage): ChatMessage =>
    m.id !== msgId ? m : {
      ...m,
      ...(content !== undefined ? { content } : {}),
      ...(extra || {}),
      meta: { ...(m.meta || {}), media: { ...(m.meta?.media || {}), ...patch } },
    };
  useAppStore.setState((st) => {
    const bg: Record<string, ChatMessage[]> = {};
    for (const k of Object.keys(st.bgMessages)) bg[k] = st.bgMessages[k].some((m) => m.id === msgId) ? st.bgMessages[k].map(upd) : st.bgMessages[k];
    return { messages: st.messages.map(upd), bgMessages: bg };
  });
}

function findMsg(msgId: string): ChatMessage | undefined {
  const st = useAppStore.getState();
  return st.messages.find((m) => m.id === msgId) || Object.values(st.bgMessages).flat().find((m) => m.id === msgId);
}

/** The chat the card belongs to; made on first use, like sending a first prompt does. */
async function ensureSessionId(title: string): Promise<string | number | null> {
  const st = useAppStore.getState();
  if (st.activeSessionId) return st.activeSessionId;
  const pid = st.appMode === 'code' && st.activeProjectId ? st.activeProjectId : 0;
  // an agent chat is listed under its agent: the same title and agent id as a first prompt would use
  const ca = st.appMode === 'agent' ? st.activeCustomAgent : null;
  const sessionTitle = ca ? `${ca.icon || '🤖'} ${ca.name}` : title;
  try {
    const res = await apiFetch(`/control/projects/${pid}/sessions`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ title: sessionTitle, agent_id: ca ? ca.id : undefined }),
    });
    const data = await res.json();
    if (data && data.session) {
      const id = data.session.id;
      try { localStorage.setItem(getActiveSessionKey(st.appMode), String(id)); } catch (_) {}
      useAppStore.setState({ activeSessionId: id });
      useAppStore.getState().fetchSessions();
      return id;
    }
  } catch (_) {
    // the card still works, it just is not saved
  }
  return null;
}

function persistMedia(msgId: string): void {
  const job = live.get(msgId);
  const m = findMsg(msgId);
  if (!job || !m || job.saved || !job.sid) return;
  job.saved = true;
  const md: MediaMeta | undefined = m.meta?.media;
  if (!md) return;
  const text = md.state === 'done' ? m.content
    : `(${md.state === 'cancelled' ? 'cancelled' : 'couldn’t make the ' + md.kind}: ${md.error || ''})`;
  const meta = {
    media: { kind: md.kind, prompt: md.prompt, opts: md.opts, state: md.state, model: md.model, where: md.where, secs: md.secs, error: md.error || undefined, files: md.files },
    modelDisplay: md.model,
    timeStr: m.timestamp,
  };
  const key = sessionKey(job.sid);
  api.sessions
    .persistMessage(job.sid, 'assistant', text, meta)
    .finally(() => {
      // the saved card is on the server now: the chat no longer needs its parked copy
      externalBusy.delete(key);
      useAppStore.setState((st) => {
        if (sessionKey(st.activeSessionId) === key || !(key in st.bgMessages)) return st;
        const bg = { ...st.bgMessages };
        delete bg[key];
        return { bgMessages: bg };
      });
    });
}

function fail(msgId: string, error: string, notSetup = false, cancelled = false): void {
  const md: MediaMeta | undefined = findMsg(msgId)?.meta?.media;
  if (md && md.state === 'cancelled') return;   // already stopped: keep "Stopped."
  patchMedia(msgId, { state: cancelled ? 'cancelled' : 'error', error, notSetup, loading: false }, '');
  persistMedia(msgId);
}

/** Read a text/event-stream response; fn gets (event, parsed data). */
async function readSSE(res: Response, fn: (ev: string, data: any) => void): Promise<void> {
  if (!res.body) return;
  const reader = res.body.getReader();
  const dec = new TextDecoder();
  let buf = '';
  for (;;) {
    const { done, value } = await reader.read();
    if (done) break;
    buf += dec.decode(value, { stream: true });
    let i: number;
    while ((i = buf.indexOf('\n\n')) >= 0) {
      const block = buf.slice(0, i);
      buf = buf.slice(i + 2);
      let ev = 'message';
      let data = '';
      for (const line of block.split('\n')) {
        if (line.startsWith('event:')) ev = line.slice(6).trim();
        else if (line.startsWith('data:')) data += line.slice(5).trim();
      }
      if (!data) continue;
      try { fn(ev, JSON.parse(data)); } catch (_) { /* heartbeat / partial */ }
    }
  }
}

function notLoaded(msgId: string, nl: any): void {
  patchMedia(msgId, { state: 'not_loaded', lane: nl.lane, laneLabel: nl.label, canLoad: !!nl.can_load, loading: !!nl.loading, error: '' });
  if (nl.loading) void waitLoaded(msgId);
}

async function startJob(msgId: string, confirmCost: boolean): Promise<void> {
  const job = live.get(msgId);
  const md: MediaMeta | undefined = findMsg(msgId)?.meta?.media;
  if (!job || !md) return;
  patchMedia(msgId, { state: 'starting', t0: Date.now(), error: '', text: 'Starting…' });
  const o = md.opts || {};
  const body: any = { kind: md.kind, prompt: md.prompt, aspect: o.aspect, confirm_cost: confirmCost };
  if (md.kind === 'video') body.seconds = +o.seconds || 4;
  if (md.kind === 'image' && o.size) body.size = o.size;
  if (o.seed != null) body.seed = o.seed;
  if (job.pics && job.pics.inputs.length) {
    // starting from pictures: the result keeps the first picture's shape
    Object.assign(body, { images: job.pics.inputs, mode: job.pics.mode });
    delete body.aspect;
    if (job.pics.mode === 'img2img' && job.pics.strength) body.strength = job.pics.strength;
  }
  let r: Response, j: any;
  try {
    r = await apiFetch('/media/generate', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) });
    j = await r.json().catch(() => ({}));
  } catch (_) {
    return fail(msgId, 'Couldn’t reach the server.');
  }
  const stopped = () => findMsg(msgId)?.meta?.media?.state === 'cancelled' || !live.has(msgId);   // not live: its chat was deleted
  if (stopped()) {
    // Cancel was pressed before the server had answered: stop the job it just created
    if (j && j.job_id) apiFetch(`/media/jobs/${encodeURIComponent(j.job_id)}`, { method: 'DELETE' }).catch(() => {});
    return;
  }
  if (r.status === 409 && j.needs_confirm) {
    patchMedia(msgId, { state: 'confirm', provider: j.provider, cloudModel: j.model });
    return;
  }
  if (r.status === 409 && j.not_loaded) return notLoaded(msgId, j.not_loaded);
  if (!r.ok) return fail(msgId, j.error || 'HTTP ' + r.status, !!j.not_setup);
  job.jobId = j.job_id;
  patchMedia(msgId, { state: 'running', model: j.model, text: j.cloud ? 'Sent to the cloud…' : 'Sent to the model on this PC…' });
  try {
    const res = await apiFetch(`/media/jobs/${encodeURIComponent(j.job_id)}`);
    if (!res.ok) throw new Error('HTTP ' + res.status);
    await readSSE(res, (ev, d) => {
      if (ev === 'progress') {
        patchMedia(msgId, { text: d.text || undefined, pct: typeof d.pct === 'number' ? d.pct : null });
      } else if (ev === 'done') {
        const cur: MediaMeta | undefined = findMsg(msgId)?.meta?.media;
        if (!cur || cur.state === 'cancelled') return;   // stopped meanwhile
        patchMedia(msgId, { state: 'done', model: d.model, where: d.where, files: d.files, secs: Math.round((Date.now() - (cur.t0 || Date.now())) / 1000) },
          d.markdown || '', { model: d.model });
        persistMedia(msgId);
      } else if (ev === 'error') {
        if (d.not_loaded) notLoaded(msgId, d.not_loaded);
        else fail(msgId, d.message || 'failed', false, !!d.cancelled);
      }
    });
    // the stream ended without a result: do not leave the card spinning
    const end: MediaMeta | undefined = findMsg(msgId)?.meta?.media;
    if (live.has(msgId) && end && (end.state === 'running' || end.state === 'starting')) fail(msgId, 'The connection ended before the result arrived.');
  } catch (_) {
    const cur: MediaMeta | undefined = findMsg(msgId)?.meta?.media;
    if (cur && cur.state === 'running') fail(msgId, 'Lost the connection while waiting - the file may still appear in your files.');
  }
}

/** The local image/video model is loaded by hand: ask the server to load it, then start the job by itself. */
export async function loadModelAndRun(msgId: string): Promise<void> {
  const md: MediaMeta | undefined = findMsg(msgId)?.meta?.media;
  if (!md || !md.lane) return;
  patchMedia(msgId, { loading: true, t0: Date.now() });
  try {
    const r = await apiFetch(`/control/lanes/${encodeURIComponent(md.lane)}/load`, { method: 'POST' });
    const j = await r.json().catch(() => ({}));
    if (!r.ok || j.error) throw new Error(j.error || 'HTTP ' + r.status);
  } catch (e: any) {
    return fail(msgId, e.message);
  }
  void waitLoaded(msgId);
}

async function waitLoaded(msgId: string): Promise<void> {
  const kind = findMsg(msgId)?.meta?.media?.kind as MediaKind | undefined;
  if (!kind) return;
  for (let i = 0; i < 400; i++) {   // up to ~20 minutes
    if (findMsg(msgId)?.meta?.media?.state !== 'not_loaded') return;   // stopped while waiting
    await new Promise((res) => setTimeout(res, 3000));
    const s = (await loadMediaStatus())?.[kind];
    if (!s) continue;
    if (findMsg(msgId)?.meta?.media?.state !== 'not_loaded') return;
    if (s.state === 'loaded' || (s.ready && !s.needs_load)) {
      patchMedia(msgId, { loading: false });
      return startJob(msgId, false);
    }
    if (s.state === 'failed') return fail(msgId, 'The model failed to load — see Settings → Models & Jobs for why.');
  }
}

/** Card buttons. */
export function confirmCloud(msgId: string): void { void startJob(msgId, true); }
export function retryMedia(msgId: string): void {
  const job = live.get(msgId);
  if (job) { job.saved = false; void startJob(msgId, false); return; }
  // a card restored after a reload has no running state: start it afresh
  const md: MediaMeta | undefined = findMsg(msgId)?.meta?.media;
  if (!md) return;
  if (md.opts?.mode) {
    // it started from pictures, which are not kept after a reload: a plain text-to-image would be a different request
    useAppStore.getState().showToast('The pictures are no longer attached. Attach them again and use /image.', 'warn');
    return;
  }
  void runMedia(md.kind, md.prompt).then((err) => { if (err) useAppStore.getState().showToast(err, 'warn'); });
}
export function dismissMedia(msgId: string): void { fail(msgId, '', false, true); }

export function cancelMedia(msgId: string): void {
  const job = live.get(msgId);
  // the server cancels the job on the model too
  if (job?.jobId) apiFetch(`/media/jobs/${encodeURIComponent(job.jobId)}`, { method: 'DELETE' }).catch(() => {});
  fail(msgId, 'Stopped.', false, true);
}

/**
 * Stop every image/video still in progress for one chat. The caller must call this when the chat is deleted
 * (store deleteSession) or cleared, so the model is not left working for a chat nobody can see. -> how many stopped
 */
export function cancelMediaForSession(sid: string | number | null | undefined): number {
  if (sid == null) return 0;
  let n = 0;
  for (const [msgId, job] of Array.from(live)) {
    if (job.sid == null || String(job.sid) !== String(sid)) continue;
    live.delete(msgId);   // startJob / the progress stream notice this and stop touching the card
    const st = findMsg(msgId)?.meta?.media?.state;
    if (st === 'done' || st === 'error' || st === 'cancelled') continue;
    n++;
    if (job.jobId) apiFetch(`/media/jobs/${encodeURIComponent(job.jobId)}`, { method: 'DELETE' }).catch(() => {});   // the model stops too
    patchMedia(msgId, { state: 'cancelled', error: 'Stopped.', loading: false }, '');   // nothing is saved: the chat is gone
  }
  externalBusy.delete(sessionKey(sid));
  return n;
}

/** "Edit description": put /image or /video and the old description back in the composer. */
export function editMediaPrompt(kind: MediaKind, prompt: string): void {
  // ifEmpty: a draft the person is writing is never overwritten
  window.dispatchEvent(new CustomEvent('edit-user-prompt', { detail: { text: `/${kind} ${prompt}`, ifEmpty: true } }));
}

/** "Make another": same description, a new random seed, and the same pictures when it started from some. */
export function makeAnother(msgId: string): void {
  const md: MediaMeta | undefined = findMsg(msgId)?.meta?.media;
  if (!md) return;
  const pics = live.get(msgId)?.pics;
  void runMedia(md.kind, md.prompt, { seed: Math.floor(Math.random() * 2147483647), pics });
}

/** "Edit this picture": the result becomes the starting picture of the next /image. */
export function editResultPicture(msgId: string): void {
  const f = (findMsg(msgId)?.meta?.media?.files || []).find((x: { mime?: string }) => /^image\//.test(x.mime || ''));
  if (!f) return;
  setEditPics([{ path: f.path, name: f.name || 'picture' }]);
  setCompMode('image');
  window.dispatchEvent(new CustomEvent('edit-user-prompt', { detail: { focusOnly: true } }));   // no text: the draft stays
}

/**
 * /image or /video <description>. Adds the user line and a progress card to the open chat, then follows the job.
 * Returns an error string for the caller to toast (nothing was started), or '' when the card took over.
 */
export async function runMedia(kind: MediaKind, prompt: string, extra?: { seed?: number; pics?: MediaPics }): Promise<string> {
  prompt = (prompt || '').trim();
  if (!prompt) return `Describe the ${MEDIA_WORD[kind]} after /${kind}, e.g. /${kind} a red bus in the Dhaka rain`;
  let st0 = statusCache || (await loadMediaStatus());
  if (!mediaReady(kind, st0)) st0 = await loadMediaStatus();
  if (!mediaReady(kind, st0)) return setupMessage(kind === 'image' ? 'Making images' : 'Making videos');

  const pics = kind === 'image' ? extra?.pics : undefined;
  const timeNow = new Date().toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' });
  const stamp = Date.now();
  const note = pics ? ` · 🖼 ${pics.count} picture${pics.count > 1 ? 's' : ''}` : '';
  const userMsg: ChatMessage = {
    id: `user-${stamp}`, role: 'user', content: `/${kind} ${prompt}${note}`, timestamp: timeNow, createdAt: stamp,
    meta: { timeStr: timeNow, displayContent: `/${kind} ${prompt}${note}` },
  };
  const msgId = `assistant-${stamp}`;
  const opts: MediaOpts = { ...mediaOpts(kind) };
  if (extra?.seed != null) opts.seed = extra.seed;
  if (pics) { opts.mode = pics.mode; opts.strength = pics.strength; }
  const botMsg: ChatMessage = {
    id: msgId, role: 'assistant', content: '', timestamp: timeNow, createdAt: stamp,
    meta: { media: { kind, prompt, opts, state: 'starting', text: 'Starting…', t0: stamp } as MediaMeta },
  };
  useAppStore.setState((s) => ({ messages: [...s.messages, userMsg, botMsg] }));
  if (pics) setEditPics([]);   // the pictures leave the composer with this request

  // registered before the await so deleting the chat meanwhile cancels the job (the sid is filled in below)
  const job: LiveJob = { kind, prompt, sid: useAppStore.getState().activeSessionId || null, saved: false, pics };
  live.set(msgId, job);
  const sid = await ensureSessionId(prompt.slice(0, 60));
  if (!live.has(msgId)) return '';   // cancelled while the chat was being created
  job.sid = sid;
  if (sid) externalBusy.add(sessionKey(sid));
  if (sid) api.sessions.persistMessage(sid, 'user', userMsg.content, userMsg.meta);
  void startJob(msgId, false);
  return '';
}

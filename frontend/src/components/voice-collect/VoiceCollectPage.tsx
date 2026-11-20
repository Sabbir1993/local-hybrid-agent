// Public voice-collection page (/voice?k=<code>): a colleague agrees to the consent text, enters name + employee ID +
// how they speak at home, then reads 15 lines aloud. Each line is recorded as 16 kHz mono WAV in the browser (the same
// worklet + encoder as voice chat) and uploaded as soon as it is accepted, so a closed tab loses nothing.
// Served without the app's sign-in (main.tsx routes /voice here); every request uses credentials: 'omit', so a
// colleague's own app session is never sent. Server side: routes/speech_corpus.py.
import { useEffect, useRef, useState } from 'react';
import { audioWavBytes, STT_RATE } from '../../services/voice/wav';
import workletUrl from '../../services/voice/voice-worklet.js?url';

type Options = {
  regions: Record<string, string>;
  genders: string[];
  age_bands: string[];
  devices: string[];
  lines_per_session: number;
  max_clip_seconds: number;
};
type Line = { id: number; text: string };
type Step = 'loading' | 'closed' | 'unreachable' | 'badlink' | 'consent' | 'form' | 'record' | 'done';

const CODE_KEY = 'voice_collect_code';

const REGION_BN: Record<string, string> = {
  standard: 'প্রমিত বাংলা',
  ctg_noakhali: 'চট্টগ্রাম / নোয়াখালী',
  sylhet: 'সিলেট',
  barishal_khulna: 'বরিশাল / খুলনা',
  rangpur_mymensingh_dhaka: 'রংপুর / ময়মনসিংহ / ঢাকা',
};

// The API (/media/voice-collect/*) always answers JSON. Anything else (a web server in front serving the app's HTML for a
// path it does not forward, or its own empty 404) means the request never reached the app: say so, never parse it.
const NOT_REACHED = 'The voice service is not reachable right now. Please tell the admin (the app may be restarting or running an older version).';

async function api(path: string, init: RequestInit = {}): Promise<{ ok: boolean; status: number; data: any; reached: boolean }> {
  try {
    const r = await fetch(path, { ...init, credentials: 'omit', cache: 'no-store' });
    if (!(r.headers.get('content-type') || '').includes('application/json')) {
      return { ok: false, status: r.status, data: { error: NOT_REACHED }, reached: false };
    }
    const data = await r.json().catch(() => null);
    if (!data || typeof data !== 'object') return { ok: false, status: r.status, data: { error: NOT_REACHED }, reached: false };
    return { ok: r.ok, status: r.status, data, reached: true };
  } catch {
    return { ok: false, status: 0, data: { error: 'Network problem. Check your connection and try again.' }, reached: true };
  }
}

/** Microphone -> 16 kHz mono Float32 frames, with a live level. One instance per page. */
class Recorder {
  private ctx: AudioContext | null = null;
  private stream: MediaStream | null = null;
  private node: AudioWorkletNode | null = null;
  private frames: Float32Array[] = [];
  private capturing = false;
  onLevel: (v: number) => void = () => {};

  async open(): Promise<void> {
    if (this.ctx) return;
    if (!navigator.mediaDevices?.getUserMedia) throw new Error('This browser cannot use the microphone. Use Chrome, Edge or Firefox over https.');
    this.stream = await navigator.mediaDevices.getUserMedia({ audio: { channelCount: 1, echoCancellation: true, noiseSuppression: true, autoGainControl: true } });
    const AC: typeof AudioContext = window.AudioContext || (window as any).webkitAudioContext;
    this.ctx = new AC();
    await this.ctx.audioWorklet.addModule(workletUrl);
    const src = this.ctx.createMediaStreamSource(this.stream);
    this.node = new AudioWorkletNode(this.ctx, 'voice-frames');
    const mute = this.ctx.createGain();
    mute.gain.value = 0;
    src.connect(this.node); this.node.connect(mute); mute.connect(this.ctx.destination);
    this.node.port.onmessage = (ev) => {
      const f = ev.data as Float32Array;
      let s = 0;
      for (let i = 0; i < f.length; i++) s += f[i] * f[i];
      this.onLevel(Math.min(1, Math.sqrt(s / f.length) * 6));
      if (this.capturing) this.frames.push(f);
    };
  }

  async start(): Promise<void> {
    await this.open();
    await this.ctx!.resume();
    this.frames = [];
    this.capturing = true;
  }

  seconds(): number {
    let n = 0;
    for (const f of this.frames) n += f.length;
    return n / STT_RATE;
  }

  stop(): Uint8Array<ArrayBuffer> {
    this.capturing = false;
    let n = 0;
    for (const f of this.frames) n += f.length;
    const all = new Float32Array(n);
    let o = 0;
    for (const f of this.frames) { all.set(f, o); o += f.length; }
    this.frames = [];
    return audioWavBytes(all, STT_RATE);
  }

  close(): void {
    this.capturing = false;
    try { this.node?.disconnect(); } catch { /* already gone */ }
    this.stream?.getTracks().forEach((t) => t.stop());
    try { void this.ctx?.close(); } catch { /* already closed */ }
    this.ctx = null; this.stream = null; this.node = null;
  }
}

const S = {
  // The app's global CSS locks <body> (overflow hidden, height 100%), so this page scrolls in its own layer. Fixed to
  // the visible screen (inset 0), not 100vh: on phones 100vh is taller than what is visible while the browser bar
  // shows, which hid the bottom of the form. Bottom padding clears the phone's gesture bar.
  page: { position: 'fixed' as const, inset: 0, overflowY: 'auto' as const, overflowX: 'hidden' as const, WebkitOverflowScrolling: 'touch' as const, overscrollBehavior: 'contain' as const, background: 'var(--bg, #0f1218)', color: 'var(--text, #e6e8eb)', padding: '20px 12px calc(40px + env(safe-area-inset-bottom, 0px))', boxSizing: 'border-box' as const },
  card: { width: '100%', maxWidth: 640, margin: '0 auto', display: 'flex', flexDirection: 'column' as const, gap: 16, boxSizing: 'border-box' as const },
  h1: { fontSize: 22, margin: 0 },
  en: { fontSize: 13, opacity: 0.7, margin: 0 },
  box: { background: 'var(--panel, #171b23)', border: '1px solid var(--border, #2a303b)', borderRadius: 12, padding: 16, display: 'flex', flexDirection: 'column' as const, gap: 12 },
  label: { display: 'flex', flexDirection: 'column' as const, gap: 4, fontSize: 13 },
  // 16px: smaller text makes iPhone Safari zoom in on focus
  input: { width: '100%', boxSizing: 'border-box' as const, padding: '11px 12px', fontSize: 16, borderRadius: 8, border: '1px solid var(--border, #2a303b)', background: 'var(--bg, #0f1218)', color: 'inherit' },
  btn: { padding: '12px 18px', fontSize: 15, borderRadius: 10, border: '1px solid var(--border, #2a303b)', background: 'transparent', color: 'inherit', cursor: 'pointer' },
  primary: { padding: '12px 18px', fontSize: 15, borderRadius: 10, border: 'none', background: 'var(--accent, #4f8cff)', color: '#fff', cursor: 'pointer', fontWeight: 600 },
  line: { fontSize: 'clamp(19px, 5.5vw, 24px)', lineHeight: 1.5, overflowWrap: 'anywhere' as const, padding: '18px 16px', borderRadius: 12, background: 'var(--panel, #171b23)', border: '1px solid var(--border, #2a303b)' },
  err: { color: 'var(--red, #f87171)', fontSize: 14 },
  ok: { color: 'var(--green, #4ade80)', fontSize: 14 },
};

export function VoiceCollectPage() {
  const [step, setStep] = useState<Step>('loading');
  const [opts, setOpts] = useState<Options | null>(null);
  const [code] = useState(() => new URL(window.location.href).searchParams.get('k') || sessionStorage.getItem(CODE_KEY) || '');
  const [form, setForm] = useState({ name: '', employee_id: '', region: '', district: '', gender: '', age_band: '', device: '' });
  const [agree, setAgree] = useState(false);
  const [error, setError] = useState('');
  const [busy, setBusy] = useState(false);
  const [token, setToken] = useState('');
  const [instruction, setInstruction] = useState<'read' | 'dialect'>('read');
  const [lines, setLines] = useState<Line[]>([]);
  const [idx, setIdx] = useState(0);
  const [done, setDone] = useState<Record<number, boolean>>({});
  const [recState, setRecState] = useState<'idle' | 'recording' | 'review'>('idle');
  const [level, setLevel] = useState(0);
  const [secs, setSecs] = useState(0);
  const [take, setTake] = useState<Uint8Array<ArrayBuffer> | null>(null);
  const [takeUrl, setTakeUrl] = useState('');
  const [testUrl, setTestUrl] = useState('');
  const [testing, setTesting] = useState(false);
  const rec = useRef<Recorder | null>(null);
  const tick = useRef(0);

  // the code arrives in the link; keep it for this tab only and take it out of the address bar
  useEffect(() => {
    if (new URL(window.location.href).searchParams.has('k')) {
      sessionStorage.setItem(CODE_KEY, code);
      window.history.replaceState({}, '', '/voice');
    }
    document.title = 'ভয়েস সংগ্রহ · Voice collection';
    (async () => {
      const r = await api('/media/voice-collect/options');
      if (!r.reached) { setStep('unreachable'); return; }
      if (!r.ok || !r.data.regions || typeof r.data.regions !== 'object') { setStep('closed'); return; }
      setOpts(r.data);
      setStep(code ? 'consent' : 'badlink');
    })();
    return () => { rec.current?.close(); window.clearInterval(tick.current); };
  }, [code]);

  useEffect(() => () => { if (takeUrl) URL.revokeObjectURL(takeUrl); }, [takeUrl]);
  useEffect(() => () => { if (testUrl) URL.revokeObjectURL(testUrl); }, [testUrl]);

  const recorder = () => {
    if (!rec.current) {
      rec.current = new Recorder();
      rec.current.onLevel = (v) => setLevel(v);
    }
    return rec.current;
  };

  const micError = (e: unknown) => {
    const m = (e as Error)?.message || String(e);
    setError(/denied|permission|notallowed/i.test(m)
      ? 'মাইক্রোফোনের অনুমতি দিন। Allow the microphone in the browser (the icon left of the address) and try again.'
      : 'মাইক্রোফোন চালু করা যায়নি। ' + m);
  };

  const testMic = async () => {
    setError('');
    setTesting(true);
    try {
      const r = recorder();
      await r.start();
      await new Promise((res) => setTimeout(res, 3000));
      const wav = r.stop();
      setTestUrl(URL.createObjectURL(new Blob([wav], { type: 'audio/wav' })));
    } catch (e) {
      micError(e);
    } finally {
      setTesting(false);
    }
  };

  const start = async () => {
    setError('');
    if (!form.name.trim() || !form.employee_id.trim() || !form.region) {
      setError('নাম, এমপ্লয়ি আইডি এবং আপনার ভাষার ধরন দিন। Name, employee ID and how you speak are required.');
      return;
    }
    setBusy(true);
    const r = await api('/media/voice-collect/start', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ ...form, code, consent: agree }),
    });
    setBusy(false);
    if (!r.ok) {
      if (r.status === 403) { sessionStorage.removeItem(CODE_KEY); setStep('badlink'); return; }
      if (r.reached && r.status === 404) { setStep('closed'); return; }
      setError(r.data.error || 'Could not start.');
      return;
    }
    setToken(r.data.token);
    setInstruction(r.data.instruction === 'dialect' ? 'dialect' : 'read');
    setLines(r.data.lines || []);
    setIdx(0);
    setDone({});
    setRecState('idle');
    setStep('record');
  };

  const maxSecs = opts?.max_clip_seconds || 20;

  const startRec = async () => {
    setError('');
    try {
      const r = recorder();
      await r.start();
      setRecState('recording');
      setSecs(0);
      window.clearInterval(tick.current);
      tick.current = window.setInterval(() => {
        const s = r.seconds();
        setSecs(s);
        if (s >= maxSecs - 0.2) stopRec();
      }, 200);
    } catch (e) {
      micError(e);
    }
  };

  const stopRec = () => {
    window.clearInterval(tick.current);
    const r = rec.current;
    if (!r) return;
    const wav = r.stop();
    setTake(wav);
    setTakeUrl(URL.createObjectURL(new Blob([wav], { type: 'audio/wav' })));
    setRecState('review');
  };

  const next = () => {
    setTake(null);
    setTakeUrl('');
    setRecState('idle');
    setError('');
    const left = lines.findIndex((l, i) => i > idx && !done[l.id]);
    if (left >= 0) setIdx(left);
    else {
      const any = lines.findIndex((l) => !done[l.id]);
      if (any >= 0) setIdx(any); else void finish();
    }
  };

  const submit = async () => {
    if (!take) return;
    setBusy(true);
    setError('');
    const fd = new FormData();
    fd.append('token', token);
    fd.append('prompt_id', String(lines[idx].id));
    fd.append('file', new Blob([take], { type: 'audio/wav' }), 'line.wav');
    const r = await api('/media/voice-collect/recording', { method: 'POST', body: fd });
    setBusy(false);
    if (!r.ok) {
      if (r.status === 401 || r.status === 404) { setError(r.data.error || 'Your session has ended.'); return; }
      setError(r.data.error || 'Could not save. Please try again.');
      return;
    }
    const nowDone = { ...done, [lines[idx].id]: true };
    setDone(nowDone);
    if (lines.every((l) => nowDone[l.id])) { void finish(); return; }
    next();
  };

  const finish = async () => {
    window.clearInterval(tick.current);
    await api('/media/voice-collect/finish', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ token }) });
    rec.current?.close();
    rec.current = null;
    setStep('done');
  };

  const regionLabel = (k: string) => `${REGION_BN[k] || ''}${REGION_BN[k] ? ' · ' : ''}${opts?.regions[k] || k}`;
  const doneCount = lines.filter((l) => done[l.id]).length;

  return (
    <div style={S.page}>
      <div style={S.card}>
        <div>
          <h1 style={S.h1}>ভয়েস সংগ্রহ</h1>
          <p style={S.en}>Voice collection for our Bangla speech model</p>
        </div>

        {step === 'loading' && <div style={S.box}>Loading…</div>}

        {step === 'unreachable' && (
          <div style={S.box}>
            <div>ভয়েস সার্ভিসের সাথে সংযোগ হচ্ছে না। অ্যাডমিনকে জানান।</div>
            <div style={S.en}>{NOT_REACHED}</div>
          </div>
        )}

        {step === 'closed' && (
          <div style={S.box}>
            <div>ভয়েস সংগ্রহ এখন বন্ধ আছে।</div>
            <div style={S.en}>Voice collection is closed right now.</div>
          </div>
        )}

        {step === 'badlink' && (
          <div style={S.box}>
            <div>এই লিংকটি আর কাজ করছে না। বর্তমান লিংকটি চেয়ে নিন।</div>
            <div style={S.en}>This link is not valid. Ask for the current link.</div>
          </div>
        )}

        {step === 'consent' && (
          <div style={S.box}>
            <strong>সম্মতি · Consent</strong>
            <div style={{ fontSize: 14, lineHeight: 1.6 }}>
              আপনি কিছু বাক্য পড়বেন, আর আপনার কণ্ঠ রেকর্ড হবে। এই রেকর্ডিং শুধু আমাদের নিজস্ব বাংলা স্পিচ-টু-টেক্সট মডেল
              তৈরিতে ব্যবহার হবে, কোম্পানির বাইরে যাবে না। আপনার নাম ও এমপ্লয়ি আইডি রেকর্ডিংয়ের সাথে রাখা হবে। যেকোনো সময়
              অ্যাডমিনকে বললে আপনার সব রেকর্ডিং মুছে ফেলা হবে।
              <br /><strong>কোনো আসল কার্ড নম্বর, অ্যাকাউন্ট নম্বর বা গ্রাহকের তথ্য বলবেন না।</strong>
            </div>
            <div style={S.en}>
              You will read some sentences aloud and your voice will be recorded. The recordings are used only to build our own
              Bangla speech-to-text model and stay inside the company. Your name and employee ID are stored with them. Ask the
              admin at any time to delete all your recordings. Never say a real card number, account number or customer detail.
            </div>
            <label style={{ display: 'flex', gap: 8, alignItems: 'center', fontSize: 15 }}>
              <input type="checkbox" checked={agree} onChange={(e) => setAgree(e.target.checked)} style={{ width: 20, height: 20 }} />
              আমি রাজি · I agree
            </label>
            <button type="button" style={{ ...S.primary, opacity: agree ? 1 : 0.5 }} disabled={!agree} onClick={() => setStep('form')}>
              পরবর্তী · Next
            </button>
          </div>
        )}

        {step === 'form' && opts && (
          <div style={S.box}>
            <label style={S.label}>নাম · Name *
              <input style={S.input} value={form.name} maxLength={80} onChange={(e) => setForm({ ...form, name: e.target.value })} />
            </label>
            <label style={S.label}>এমপ্লয়ি আইডি · Employee ID *
              <input style={S.input} value={form.employee_id} maxLength={32} onChange={(e) => setForm({ ...form, employee_id: e.target.value })} />
            </label>
            <label style={S.label}>বাড়িতে আপনি কোন ভাষায় কথা বলেন? · How do you speak at home? *
              <select style={S.input} value={form.region} onChange={(e) => setForm({ ...form, region: e.target.value })}>
                <option value="">—</option>
                {Object.keys(opts.regions).map((k) => <option key={k} value={k}>{regionLabel(k)}</option>)}
              </select>
            </label>
            <label style={S.label}>জেলা (ঐচ্ছিক) · Home district (optional)
              <input style={S.input} value={form.district} maxLength={40} onChange={(e) => setForm({ ...form, district: e.target.value })} />
            </label>
            <div style={{ display: 'flex', gap: 10, flexWrap: 'wrap' }}>
              <label style={{ ...S.label, flex: 1, minWidth: 140 }}>লিঙ্গ · Gender (optional)
                <select style={S.input} value={form.gender} onChange={(e) => setForm({ ...form, gender: e.target.value })}>
                  <option value="">—</option>
                  {(opts.genders || []).map((g) => <option key={g} value={g}>{g}</option>)}
                </select>
              </label>
              <label style={{ ...S.label, flex: 1, minWidth: 140 }}>বয়স · Age (optional)
                <select style={S.input} value={form.age_band} onChange={(e) => setForm({ ...form, age_band: e.target.value })}>
                  <option value="">—</option>
                  {(opts.age_bands || []).map((g) => <option key={g} value={g}>{g}</option>)}
                </select>
              </label>
              <label style={{ ...S.label, flex: 1, minWidth: 140 }}>মাইক · Microphone
                <select style={S.input} value={form.device} onChange={(e) => setForm({ ...form, device: e.target.value })}>
                  <option value="">—</option>
                  {(opts.devices || []).map((g) => <option key={g} value={g}>{g}</option>)}
                </select>
              </label>
            </div>
            <div style={{ display: 'flex', gap: 10, alignItems: 'center', flexWrap: 'wrap' }}>
              <button type="button" style={S.btn} disabled={testing} onClick={testMic}>
                {testing ? 'কথা বলুন… · Speak…' : 'মাইক পরীক্ষা (৩ সেকেন্ড) · Test mic (3 s)'}
              </button>
              {testing && <Meter level={level} />}
              {testUrl && !testing && <audio src={testUrl} controls style={{ height: 36, width: '100%', maxWidth: 320 }} />}
            </div>
            {error && <div style={S.err}>{error}</div>}
            <button type="button" style={S.primary} disabled={busy} onClick={start}>
              {busy ? '…' : 'শুরু করুন · Start'}
            </button>
          </div>
        )}

        {step === 'record' && lines.length > 0 && (
          <>
            <div style={{ display: 'flex', justifyContent: 'space-between', fontSize: 14, opacity: 0.8 }}>
              <span>লাইন {idx + 1} / {lines.length}</span>
              <span>সম্পন্ন · done: {doneCount}</span>
            </div>
            <div style={{ fontSize: 14, padding: '8px 12px', borderRadius: 8, background: 'rgba(79,140,255,0.12)' }}>
              {instruction === 'dialect'
                ? <>নিচের বাক্যটি <strong>আপনার নিজের আঞ্চলিক ভাষায়</strong> বলুন, যেভাবে বাড়িতে বলেন। অর্থ একই রাখুন।<div style={S.en}>Say it in your own home dialect, the way you talk at home. Keep the meaning.</div></>
                : <>নিচের বাক্যটি স্বাভাবিকভাবে পড়ুন। ইংরেজি শব্দগুলো যেভাবে সাধারণত বলেন সেভাবেই বলুন।<div style={S.en}>Read it naturally. Say English words the way you usually do.</div></>}
            </div>
            <div style={S.line} lang="bn">{lines[idx].text}</div>
            <div style={{ display: 'flex', gap: 10, alignItems: 'center', flexWrap: 'wrap' }}>
              {recState === 'idle' && (
                <button type="button" style={{ ...S.primary, padding: '18px 28px', fontSize: 18, flex: '1 1 220px' }} onClick={startRec}>● রেকর্ড · Record</button>
              )}
              {recState === 'recording' && (
                <>
                  <button type="button" style={{ ...S.primary, padding: '18px 28px', fontSize: 18, background: 'var(--red, #ef4444)', flex: '1 1 220px' }} onClick={stopRec}>■ থামুন · Stop</button>
                  <Meter level={level} />
                  <span style={{ fontVariantNumeric: 'tabular-nums' }}>{secs.toFixed(1)} / {maxSecs}s</span>
                </>
              )}
              {recState === 'review' && (
                <>
                  {takeUrl && <audio src={takeUrl} controls style={{ height: 40, width: '100%', maxWidth: 360 }} />}
                  <button type="button" style={S.btn} disabled={busy} onClick={() => { setRecState('idle'); setTake(null); setTakeUrl(''); }}>↺ আবার · Re-record</button>
                  <button type="button" style={{ ...S.primary, flex: '1 1 160px' }} disabled={busy} onClick={submit}>{busy ? '…' : '✓ জমা দিন · Submit'}</button>
                </>
              )}
              {recState !== 'recording' && (
                <button type="button" style={{ ...S.btn, marginLeft: 'auto', opacity: 0.7 }} disabled={busy} onClick={next}>বাদ দিন · Skip</button>
              )}
            </div>
            {error && <div style={S.err}>{error}</div>}
            {done[lines[idx].id] && <div style={S.ok}>এই লাইনটি জমা হয়েছে। · This line is saved.</div>}
            <button type="button" style={{ ...S.btn, opacity: 0.7 }} disabled={busy} onClick={() => void finish()}>শেষ করুন · Finish now</button>
          </>
        )}

        {step === 'done' && (
          <div style={S.box}>
            <div style={{ fontSize: 18 }}>ধন্যবাদ! 🙏</div>
            <div>আপনার রেকর্ডিং জমা হয়েছে।</div>
            <div style={S.en}>Thank you! Your recordings are saved.</div>
            <button type="button" style={S.primary} onClick={() => { setStep('form'); setError(''); setTestUrl(''); }}>
              আরও {opts?.lines_per_session || 15}টি লাইন রেকর্ড করুন · Record {opts?.lines_per_session || 15} more
            </button>
          </div>
        )}
      </div>
    </div>
  );
}

function Meter({ level }: { level: number }) {
  return (
    <div style={{ width: 120, height: 10, borderRadius: 5, background: 'rgba(255,255,255,0.12)', overflow: 'hidden' }} aria-label="microphone level">
      <div style={{ width: `${Math.round(level * 100)}%`, height: '100%', background: level > 0.9 ? '#ef4444' : '#4ade80', transition: 'width 80ms linear' }} />
    </div>
  );
}

export default VoiceCollectPage;

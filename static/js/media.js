/* ---------------- media.js: 🎤 dictation, audio attachments, /image and /video ----------------
 * Server side: routes/media.py + core/media.py. Each feature appears only once
 * a model is set up for it in Settings -> Models & Jobs (GET /media/status).
 *   🎤 button   record -> 16 kHz WAV (audio-wav.js) -> /media/transcribe -> text at the cursor
 *   audio file  attached like a document, as its transcript
 *   /image, /video <description>  -> a progress card in the chat, then the result
 *   composer select "🖼 Image"     -> Send makes an image from the typed text
 * An image model on this PC (stable-diffusion.cpp) is loaded by hand: the card
 * offers ▶ Load and starts the image once it's ready. */

const MEDIA = { status: null, rec: null, timer: 0, live: new Set() };
const MEDIA_SETUP_URL = '/settings#sec-lanes';
const MIC_MAX_S = 10 * 60;
const STT_LANGS = [['auto', 'Auto'], ['en', 'English'], ['bn', 'বাংলা']];

function _mediaSetupToast(what) {
  if (typeof toast !== 'function') return;
  toast(`${what} isn't set up yet.`, {
    duration: 9000, actions: [{ label: 'Set it up', onClick: () => { location.href = MEDIA_SETUP_URL; } }] });
}

async function mediaLoadStatus() {
  try {
    const r = await fetch('/media/status');
    if (r.ok) MEDIA.status = await r.json();
  } catch (_) {}
  _paintMic();
  _paintCompMode();
  return MEDIA.status;
}
window.mediaLoadStatus = mediaLoadStatus;

/* ---------------- composer select: 💬 Chat / 🖼 Image ---------------- */

function mediaComposeMode() {
  const sel = $('comp-mode');
  return sel && !sel.hidden && sel.value === 'image' && mediaReady('image') ? 'image' : 'chat';
}
window.mediaComposeMode = mediaComposeMode;

function _paintCompMode() {
  const sel = $('comp-mode');
  if (!sel) return;
  const ok = mediaReady('image');
  sel.hidden = !ok;
  if (!ok) sel.value = 'chat';
  const input = $('input');
  if (input) {
    if (!input.dataset.chatPh) input.dataset.chatPh = input.placeholder || '';
    input.placeholder = sel.value === 'image' && ok ? 'Describe the image…' : input.dataset.chatPh;
  }
  const st = MEDIA.status && MEDIA.status.image;
  sel.title = ok ? `What Send does — Chat, or make an image with ${_whereText(st)}` : '';
  if (typeof mediaPaintPics === 'function') mediaPaintPics();
}

function mediaReady(key) {
  return !!(MEDIA.status && MEDIA.status[key] && MEDIA.status[key].ready);
}
window.mediaReady = mediaReady;

function _whereText(st) {
  if (!st) return '';
  return st.cloud ? `${st.model} (${st.provider}, cloud)` : `${st.model} (on this PC)`;
}

/* ---------------- 🎤 dictation ---------------- */

function _paintMic() {
  const b = $('btn-mic');
  if (!b) return;
  const st = MEDIA.status && MEDIA.status.transcribe;
  b.hidden = !(st && st.ready);
  const recording = !!MEDIA.rec;
  b.classList.toggle('recording', recording);
  b.setAttribute('aria-pressed', recording ? 'true' : 'false');
  const label = recording ? 'Stop recording and turn it into text'
    : `Dictate — speak and it's typed for you${st && st.ready ? ' · ' + _whereText(st) : ''}`;
  b.title = label + (recording ? ' (Esc cancels)' : '');
  b.setAttribute('aria-label', label);
}

function _sttLang() {
  try { return localStorage.getItem('stt_lang') || 'auto'; } catch (_) { return 'auto'; }
}

function _pill() {
  let p = $('mic-pill');
  if (!p) {
    p = document.createElement('div');
    p.id = 'mic-pill';
    p.className = 'mic-pill';
    p.setAttribute('role', 'status');
    p.setAttribute('aria-live', 'polite');
    const comp = document.querySelector('.composer');
    const actions = comp && comp.querySelector('.comp-actions');
    if (comp && actions) comp.insertBefore(p, actions); else document.body.appendChild(p);
  }
  return p;
}

function _pillShow(html) { const p = _pill(); p.innerHTML = html; p.hidden = false; }
function _pillHide() { const p = $('mic-pill'); if (p) { p.hidden = true; p.innerHTML = ''; } }

function _fmtClock(s) {
  s = Math.max(0, Math.floor(s));
  return `${Math.floor(s / 60)}:${String(s % 60).padStart(2, '0')}`;
}

function _recPillHtml() {
  const lang = _sttLang();
  const opts = STT_LANGS.map(([v, l]) => `<option value="${v}"${v === lang ? ' selected' : ''}>${l}</option>`).join('');
  return `<span class="mic-dot" aria-hidden="true"></span>
    <span class="mic-time" data-mic-time>0:00</span>
    <span class="mic-level" aria-hidden="true"><span data-mic-level></span></span>
    <span class="mic-label">Listening…</span>
    <label class="mic-lang"><span class="sr-only">Language</span>
      <select data-mic-lang aria-label="Language you're speaking">${opts}</select></label>
    <button type="button" class="btn accent mic-btn" data-mic="stop">■ Done</button>
    <button type="button" class="btn ghost mic-btn" data-mic="cancel">Cancel</button>`;
}

async function micStart() {
  if (MEDIA.rec) return;
  if (!mediaReady('transcribe')) { _mediaSetupToast('Speech to text'); return; }
  if (!navigator.mediaDevices || !navigator.mediaDevices.getUserMedia || typeof MediaRecorder === 'undefined') {
    toast('This browser can’t record audio. Try Chrome or Edge.', true);
    return;
  }
  let stream;
  try {
    stream = await navigator.mediaDevices.getUserMedia({
      audio: { channelCount: 1, echoCancellation: true, noiseSuppression: true } });
  } catch (e) {
    const denied = e && (e.name === 'NotAllowedError' || e.name === 'SecurityError');
    toast(denied ? 'The microphone is blocked. Allow it from the 🔒 icon in the address bar, then try again.'
      : 'No microphone found.', true);
    return;
  }
  const chunks = [];
  const rec = new MediaRecorder(stream);
  const Ctx = window.AudioContext || window.webkitAudioContext;
  const actx = Ctx ? new Ctx() : null;
  let analyser = null;
  if (actx) {
    analyser = actx.createAnalyser();
    analyser.fftSize = 512;
    actx.createMediaStreamSource(stream).connect(analyser);
  }
  const state = { rec, stream, actx, analyser, chunks, t0: Date.now(), cancelled: false, raf: 0 };
  MEDIA.rec = state;
  rec.ondataavailable = e => { if (e.data && e.data.size) chunks.push(e.data); };
  rec.onstop = () => _recFinished(state);
  rec.start(1000);
  _pillShow(_recPillHtml());
  _paintMic();
  const buf = analyser ? new Uint8Array(analyser.fftSize) : null;
  // a timer, not requestAnimationFrame: it keeps counting (and stops at the
  // limit) while the tab is in the background
  const tick = () => {
    if (MEDIA.rec !== state) { clearInterval(state.raf); return; }
    const secs = (Date.now() - state.t0) / 1000;
    const t = document.querySelector('[data-mic-time]');
    if (t) t.textContent = _fmtClock(secs);
    if (analyser) {
      analyser.getByteTimeDomainData(buf);
      let peak = 0;
      for (let i = 0; i < buf.length; i++) peak = Math.max(peak, Math.abs(buf[i] - 128));
      const lv = document.querySelector('[data-mic-level]');
      if (lv) lv.style.width = Math.min(100, Math.round(peak / 128 * 180)) + '%';
    }
    if (secs >= MIC_MAX_S) { toast('Stopped after 10 minutes.'); micStop(); }
  };
  state.raf = setInterval(tick, 150);
}

function micStop(cancel) {
  const s = MEDIA.rec;
  if (!s) return;
  s.cancelled = !!cancel;
  try { s.rec.stop(); } catch (_) { _recFinished(s); }
}
window.micStart = micStart;
window.micStop = micStop;

async function _recFinished(s) {
  if (MEDIA.rec !== s) return;
  MEDIA.rec = null;
  clearInterval(s.raf);
  s.stream.getTracks().forEach(t => t.stop());
  try { s.actx && s.actx.close(); } catch (_) {}
  _paintMic();
  if (s.cancelled || !s.chunks.length) { _pillHide(); return; }
  const blob = new Blob(s.chunks, { type: s.rec.mimeType || 'audio/webm' });
  const st = MEDIA.status && MEDIA.status.transcribe;
  _pillShow(`<span class="ac-shimmer"></span><span>Turning speech into text with <b>${esc(_whereText(st))}</b>…</span>`);
  try {
    const text = await mediaTranscribeBlob(blob);
    _pillHide();
    if (!text) { toast('No speech was heard. Try again a little closer to the microphone.'); return; }
    _insertAtCursor(text);
  } catch (e) {
    _pillHide();
    toast(`Couldn’t turn the recording into text: ${e.message}`, true);
  }
}

function _insertAtCursor(text) {
  const input = $('input');
  if (!input) return;
  // where the caret was before the mic button took focus (else: the end of the text)
  const sel = MEDIA.sel && MEDIA.sel[2] === input.value ? MEDIA.sel : [input.value.length, input.value.length];
  const s = sel[0], e = sel[1];
  const before = input.value.slice(0, s), after = input.value.slice(e);
  const pad = before && !/\s$/.test(before) ? ' ' : '';
  input.value = before + pad + text + (after && !/^\s/.test(after) ? ' ' : '') + after;
  const pos = (before + pad + text).length;
  input.focus();
  input.setSelectionRange(pos, pos);
  input.dispatchEvent(new Event('input', { bubbles: true }));
}

/* any audio Blob/File -> transcript text (throws Error with a plain message) */
async function mediaTranscribeBlob(blob, language) {
  let conv;
  try {
    conv = await audioBlobToSttWav(blob);
  } catch (_) {
    throw new Error('this audio format can’t be read by the browser - try mp3, m4a, wav or ogg');
  }
  const cap = ((MEDIA.status && MEDIA.status.max_audio_mb) || 25) * 1024 * 1024;
  if (conv.wav.size > cap) {
    throw new Error(`it's too long (${Math.round(conv.seconds / 60)} min) - the limit is about ${Math.floor(cap / 32000 / 60)} minutes`);
  }
  const fd = new FormData();
  fd.append('file', conv.wav, 'audio.wav');
  fd.append('language', language || _sttLang());
  const r = await fetch('/media/transcribe', { method: 'POST', body: fd });
  const j = await r.json().catch(() => ({}));
  if (!r.ok) throw new Error(j.error || ('HTTP ' + r.status));
  return (j.text || '').trim();
}
window.mediaTranscribeBlob = mediaTranscribeBlob;

/* attach an audio file: it goes into the prompt as its transcript */
function mediaAttachAudio(f) {
  if (!mediaReady('transcribe')) { _mediaSetupToast('Speech to text (for audio files)'); return; }
  if (f.size > 200 * 1024 * 1024) { toast(`"${f.name}" is too large (200 MB max).`, true); return; }
  const att = { name: f.name || 'audio', size: f.size, isImage: false, isAudio: true, uploading: true };
  attachments.push(att);
  refreshAttachUI();
  att.transcribePromise = (async () => {
    try {
      const text = await mediaTranscribeBlob(f);
      att.content = `(transcript of the audio file ${att.name})\n${text || '(no speech found)'}`;
      toast(`🎤 ${att.name} transcribed`);
    } catch (e) {
      att.content = `(could not transcribe ${att.name}: ${e.message})`;
      toast(`Couldn’t transcribe ${att.name}: ${e.message}`, true);
    } finally {
      att.uploading = false;
      refreshAttachUI();
    }
  })();
}
window.mediaAttachAudio = mediaAttachAudio;


/* ---------------- pictures to start from: change one / combine several ---------------- */
/* Local only: the server sends them to the image model on this PC, never to the cloud. */

MEDIA.editPics = [];      // [{path, name}] from "✏️ Edit this picture" on a result
const PIC_OK = /^image\/(png|jpe?g|webp)$/i;

function mediaPics() {
  const att = (typeof attachments !== 'undefined' ? attachments : []).filter(a => a.isImage && a.b64);
  return [...MEDIA.editPics.map(p => ({ path: p.path, name: p.name, thumb: '/agent/raw?path=' + encodeURIComponent(p.path) })),
          ...att.map(a => ({ att: a, name: a.name, thumb: a.dataUrl, bad: !PIC_OK.test(a.mime || '') }))];
}
window.mediaPics = mediaPics;

function _picCaps() {
  const st = MEDIA.status && MEDIA.status.image;
  return (st && st.edit) || { img2img: false, refs: false, max_refs: 0 };
}
function _picPref(k, d) { try { return localStorage.getItem('media_pic_' + k) || d; } catch (_) { return d; } }
function _picSet(k, v) { try { localStorage.setItem('media_pic_' + k, v); } catch (_) {} }
function _picMode(n) {
  const caps = _picCaps();
  if (n > 1) return 'edit';
  const want = _picPref('mode', '');
  if (want === 'edit' && caps.refs) return 'edit';
  if (want === 'img2img' && caps.img2img) return 'img2img';
  return caps.refs ? 'edit' : 'img2img';
}
function _picStrength() {
  const v = parseFloat(_picPref('strength', '0.6'));
  return v >= 0.05 && v <= 1 ? v : 0.6;
}
function _picsOn() {
  const cmd = window.getArmedCmd && window.getArmedCmd();
  return mediaComposeMode() === 'image' || !!(cmd && cmd.name === 'image');
}

/* the bar above the composer: <image1> <image2>… · Change / Combine · how much */
function mediaPaintPics() {
  const bar = $('media-pics-bar');
  if (!bar) return;
  const pics = mediaPics();
  const input = $('input');
  const armed = window.getArmedCmd && window.getArmedCmd();
  const chipRow = !!(armed && armed.name === 'image');       // the /image chip has its own options row
  if (!_picsOn() || !mediaReady('image') || (!pics.length && chipRow)) {
    bar.hidden = true; bar.innerHTML = '';
    if (input && input.dataset.picPh) { input.placeholder = input.dataset.picPh; delete input.dataset.picPh; }
    return;
  }
  const o = mediaOpts('image');
  if (!pics.length) {
    // 🖼 mode, text only: shape + size
    bar.innerHTML = `<div class="media-pics media-opts" role="group" aria-label="Image options">
        ${_shapeSegHtml('image', o)}${_sizeSelHtml(o, o.aspect)}</div>`;
    bar.hidden = false;
    if (input && input.dataset.picPh) { input.placeholder = input.dataset.picPh; delete input.dataset.picPh; }
    return;
  }
  const caps = _picCaps(), n = pics.length, mode = _picMode(n);
  const chips = pics.map((p, i) => `<span class="media-pic${p.bad ? ' bad' : ''}" title="${esc(p.name)}">
      <img src="${esc(p.thumb || '')}" alt=""><code>&lt;image${i + 1}&gt;</code>
      ${p.path ? `<button type="button" data-mpic="rm" data-i="${i}" aria-label="Don’t use ${esc(p.name)}">✕</button>` : ''}</span>`).join('');
  const seg = (v, label, ok, why) => `<button type="button" class="media-seg${mode === v ? ' on' : ''}" data-mpic="mode" data-val="${v}"
      aria-pressed="${mode === v}" ${ok ? '' : `disabled title="${esc(why)}"`}>${label}</button>`;
  const modes = seg('img2img', '🎨 Change this picture', caps.img2img && n === 1, n > 1 ? 'Only with one picture' : 'The image model can’t do this')
    + seg('edit', '🧩 Edit / combine', caps.refs, 'Needs the model’s vision weights — add them in Settings → Models & Jobs');
  const st = _picStrength();
  const how = mode === 'img2img'
    ? `<label class="media-strength">How much to change <input type="range" min="0.1" max="1" step="0.05" value="${st}" data-mpic="strength"
         aria-label="How much to change"> <output>${Math.round(st * 100)}%</output></label>`
    : `<span class="dim">Say what to do, e.g. “put the logo from &lt;image2&gt; on the shirt in &lt;image1&gt;”</span>`;
  let warn = '';
  if (pics.some(p => p.bad)) warn = 'Only PNG, JPEG or WEBP pictures can be used.';
  else if (n > 1 && !caps.refs) warn = 'This image model can’t combine pictures yet — add its vision weights in Settings → Models & Jobs.';
  else if (mode === 'edit' && caps.max_refs && n > caps.max_refs) warn = `This model takes at most ${caps.max_refs} pictures.`;
  else if (!caps.img2img && !caps.refs) warn = 'Load the image model on this PC to work from pictures.';
  bar.innerHTML = `<div class="media-pics" role="group" aria-label="Pictures to start from">
      <span class="media-pics-t">🖼 Start from</span>${chips}${modes}${how}${_sizeSelHtml(o, null)}
      <span class="dim media-pics-note" title="Pictures are never sent to a cloud model">🔒 stays on this PC</span>
      ${warn ? `<div class="media-pics-warn" role="alert">⚠ ${esc(warn)}</div>` : ''}</div>`;
  bar.hidden = false;
  if (input) {
    if (!input.dataset.picPh) input.dataset.picPh = input.placeholder;
    input.placeholder = mode === 'img2img' ? 'Describe how it should look…' : 'Describe the change…';
  }
}
window.mediaPaintPics = mediaPaintPics;

document.addEventListener('click', e => {
  const b = e.target.closest && e.target.closest('[data-mpic]');
  if (!b || b.tagName === 'INPUT') return;
  if (b.dataset.mpic === 'rm') {
    const p = mediaPics()[+b.dataset.i];
    if (p && p.path) MEDIA.editPics = MEDIA.editPics.filter(x => x.path !== p.path);
  } else if (b.dataset.mpic === 'mode') _picSet('mode', b.dataset.val);
  mediaPaintPics();
  const i = $('input'); if (i) i.focus();
});
document.addEventListener('input', e => {
  const r = e.target;
  if (!r || !r.matches || !r.matches('input[data-mpic="strength"]')) return;
  _picSet('strength', r.value);
  const o = r.parentElement && r.parentElement.querySelector('output');
  if (o) o.textContent = Math.round(+r.value * 100) + '%';
});

/* make an upload small (the server re-checks and re-encodes everything anyway) */
function _shrinkPic(dataUrl, mime) {
  return new Promise(res => {
    const raw = () => res(String(dataUrl).split(',')[1] || '');
    const img = new Image();
    img.onload = () => {
      const s = Math.min(1, 1536 / Math.max(img.naturalWidth, img.naturalHeight));
      if (s >= 1) return raw();
      const c = document.createElement('canvas');
      c.width = Math.max(1, Math.round(img.naturalWidth * s));
      c.height = Math.max(1, Math.round(img.naturalHeight * s));
      c.getContext('2d').drawImage(img, 0, 0, c.width, c.height);
      res(c.toDataURL(/png|webp/i.test(mime) ? 'image/png' : 'image/jpeg', 0.92).split(',')[1] || '');
    };
    img.onerror = raw;
    img.src = dataUrl;
  });
}

/* the pictures for this request (null = none); they leave the composer */
async function mediaTakePics() {
  const pics = mediaPics();
  if (!pics.length) return null;
  if (pics.some(p => p.bad)) { toast('Only PNG, JPEG or WEBP pictures can be used.', true); return false; }
  const mode = _picMode(pics.length);
  const inputs = [];
  for (const p of pics) inputs.push(p.path ? { path: p.path } : { b64: await _shrinkPic(p.att.dataUrl, p.att.mime) });
  MEDIA.editPics = [];
  if (typeof attachments !== 'undefined') {
    for (const p of pics) { const k = attachments.indexOf(p.att); if (k >= 0) attachments.splice(k, 1); }
    if (typeof refreshAttachUI === 'function') refreshAttachUI();
  }
  mediaPaintPics();
  return { inputs, mode, strength: mode === 'img2img' ? _picStrength() : undefined, count: inputs.length };
}
window.mediaTakePics = mediaTakePics;

/* ---------------- /image and /video ---------------- */

const MEDIA_WORD = { image: 'image', video: 'video' };

function mediaOpts(kind) {
  let o = {};
  try { o = JSON.parse(localStorage.getItem('media_opts_' + kind) || '{}') || {}; } catch (_) {}
  const out = { aspect: o.aspect || (kind === 'video' ? 'wide' : 'square'), seconds: o.seconds || 4 };
  if (kind === 'image') out.size = MEDIA_SIZES[o.size] ? o.size : '';       // '' = the model's default
  return out;
}

/* image size (models on this PC). Mirrors core/media.py size_for: same pixels, shape changes */
const MEDIA_SIZES = { small: 512, medium: 768, large: 1024, xlarge: 1280 };
const MEDIA_SIZE_WORD = { small: 'Small', medium: 'Medium', large: 'Large', xlarge: 'Extra large' };
const _SHAPE_R = { square: 1, wide: 1.5, tall: 2 / 3 };
function _m32(v) { return Math.max(64, Math.floor(v / 32) * 32); }
function mediaSizeFor(size, aspect) {
  const side = MEDIA_SIZES[size] || 1024, r = _SHAPE_R[aspect] || 1;
  return [_m32(side * Math.sqrt(r)), _m32(side / Math.sqrt(r))];
}
window.mediaSizeFor = mediaSizeFor;

/* the size picker; shape = null when the pictures decide the shape */
function _sizeSelHtml(o, shape) {
  const st = MEDIA.status && MEDIA.status.image;
  if (st && st.cloud) return '';                         // cloud models pick their own sizes
  const def = (st && MEDIA_SIZES[st.default_size]) ? st.default_size : 'large';
  const label = k => shape ? mediaSizeFor(k, shape).join('×') : `~${+(MEDIA_SIZES[k] ** 2 / 1e6).toFixed(2)} MP`;
  const first = `<option value=""${o.size ? '' : ' selected'}>Model default · ${MEDIA_SIZE_WORD[def]} ${label(def)}</option>`;
  const opt = first + Object.keys(MEDIA_SIZES).map(k => {
    const d = shape ? mediaSizeFor(k, shape).join('×') : `~${+(MEDIA_SIZES[k] ** 2 / 1e6).toFixed(2)} MP`;
    return `<option value="${k}"${o.size === k ? ' selected' : ''}>${MEDIA_SIZE_WORD[k]} · ${d}</option>`;
  }).join('');
  return `<label class="media-len" title="Bigger images take longer (Large is about 4× Small)">Size
    <select data-mopt="size" data-kind="image" aria-label="Image size">${opt}</select></label>`;
}

function _saveOpts(kind, o) {
  try { localStorage.setItem('media_opts_' + kind, JSON.stringify(o)); } catch (_) {}
}

/* options row shown under the armed <image>/<video> chip */
function mediaOptsHtml(kind) {
  const o = mediaOpts(kind);
  const st = MEDIA.status && MEDIA.status[kind];
  const seg = _shapeSegHtml(kind, o);
  const len = kind === 'video'
    ? `<label class="media-len">Length <select data-mopt="seconds" aria-label="Video length">${[4, 8, 12].map(s =>
        `<option value="${s}"${+o.seconds === s ? ' selected' : ''}>${s} s</option>`).join('')}</select></label>` : '';
  const who = st && st.ready
    ? `<span class="media-who">${st.cloud ? '☁' : '🖥'} ${esc(_whereText(st))}</span>`
    : `<a class="media-who warn" href="${MEDIA_SETUP_URL}">Not set up yet — set it up</a>`;
  const size = kind === 'image' ? _sizeSelHtml(o, o.aspect) : '';
  return `<div class="media-opts" role="group" aria-label="${kind} options">${seg}${size}${len}${who}</div>`;
}

function _shapeSegHtml(kind, o) {
  return [['square', '◻ Square'], ['wide', '▭ Wide'], ['tall', '▯ Tall']].map(([v, l]) =>
    `<button type="button" class="media-seg${o.aspect === v ? ' on' : ''}" data-mopt="aspect" data-kind="${kind}" data-val="${v}" aria-pressed="${o.aspect === v}">${l}</button>`).join('');
}
window.mediaOptsHtml = mediaOptsHtml;

document.addEventListener('click', e => {
  const b = e.target.closest && e.target.closest('[data-mopt]');
  if (!b || b.tagName === 'SELECT') return;
  const kind = _optKind(b);
  if (!kind) return;
  const o = mediaOpts(kind);
  o[b.dataset.mopt] = b.dataset.val;
  _saveOpts(kind, o);
  if (typeof cmdChipRender === 'function') cmdChipRender();
  mediaPaintPics();
  const i = $('input'); if (i) i.focus();
});
document.addEventListener('change', e => {
  const s = e.target;
  if (s && s.matches && s.matches('select[data-mopt]')) {
    const kind = _optKind(s);
    if (!kind) return;
    const o = mediaOpts(kind);
    o[s.dataset.mopt] = s.value;
    _saveOpts(kind, o);
  }
  if (s && s.matches && s.matches('select[data-mic-lang]')) {
    try { localStorage.setItem('stt_lang', s.value); } catch (_) {}
  }
});

function _optKind(el) {
  if (el.dataset.kind && MEDIA_WORD[el.dataset.kind]) return el.dataset.kind;
  const cmd = window.getArmedCmd && window.getArmedCmd();
  return cmd && MEDIA_WORD[cmd.name] ? cmd.name : null;
}

function _mediaRender(list, idx) {
  if (typeof messages === 'undefined' || messages !== list) return;   // another chat is open
  if (idx === messages.length - 1) { renderLast(); return; }
  if (typeof generating !== 'undefined' && generating) {
    // don't rebuild the chat under a streaming answer: update the card in place
    const el = document.querySelector(`[data-media-card="${idx}"]`);
    if (el) {
      const tmp = document.createElement('div');
      tmp.innerHTML = mediaCardHtml(messages[idx], idx);
      if (tmp.firstElementChild) el.replaceWith(tmp.firstElementChild);
    }
    const m = messages[idx];
    if (m.media && m.media.state === 'done') setTimeout(() => _mediaRender(list, idx), 1500);
    return;
  }
  renderAll();
}

async function mediaRun(kind, prompt, extra) {
  prompt = (prompt || '').trim();
  if (!prompt) { toast(`Describe the ${MEDIA_WORD[kind]} after /${kind}, e.g. /${kind} a red bus in the Dhaka rain`); return; }
  if (!MEDIA.status) await mediaLoadStatus();
  if (!mediaReady(kind)) { _mediaSetupToast(kind === 'image' ? 'Making images' : 'Making videos'); return; }
  extra = Object.assign({}, extra || {});
  let pics = extra.pics || null;
  if (extra.takePics && kind === 'image') {
    pics = await mediaTakePics();
    if (pics === false) return;
  }
  delete extra.pics; delete extra.takePics;
  const opts = Object.assign(mediaOpts(kind), extra);
  if (pics) Object.assign(opts, { mode: pics.mode, strength: pics.strength });
  const list = messages;
  const note = pics ? ` · 🖼 ${pics.count} picture${pics.count > 1 ? 's' : ''}` : '';
  const userMsg = { role: 'user', content: `/${kind} ${prompt}${note}`, displayContent: `/${kind} ${prompt}${note}` };
  const m = { role: 'assistant', content: '', acts: [],
    media: { kind, prompt, opts, state: 'starting', text: 'Starting…', t0: Date.now(),
             inputs: pics ? pics.inputs : undefined } };
  list.push(userMsg, m);
  renderAll();
  let sid = null;
  const noProject = typeof agentMode !== 'undefined' && agentMode && (!curProject || !curProject.id);
  if (!noProject && typeof ensureSession === 'function') {
    const s = await ensureSession(prompt.slice(0, 60));
    sid = s ? s.id : null;
    if (sid) persistMsgForSession(sid, 'user', userMsg.content, { displayContent: userMsg.displayContent });
  }
  m.media.sid = sid;
  await _mediaStart(list, m, false);
}
window.mediaRun = mediaRun;

function _idxOf(list, m) { return list.indexOf(m); }

async function _mediaStart(list, m, confirmCost) {
  const md_ = m.media;
  md_.state = 'starting';
  md_.t0 = Date.now();
  md_.error = '';
  // which chat it belongs to (not enumerable: never saved or copied with the message)
  Object.defineProperty(md_, 'list', { value: list, writable: true, configurable: true, enumerable: false });
  _mediaLive(m);
  _mediaRender(list, _idxOf(list, m));
  const o = md_.opts || {};
  const body = { kind: md_.kind, prompt: md_.prompt, aspect: o.aspect, confirm_cost: !!confirmCost };
  if (md_.kind === 'video') body.seconds = +o.seconds || 4;
  if (md_.kind === 'image' && MEDIA_SIZES[o.size]) body.size = o.size;
  if (o.seed != null) body.seed = o.seed;
  if (md_.inputs && md_.inputs.length) {
    // starting from pictures: the result keeps the first picture's shape
    Object.assign(body, { images: md_.inputs, mode: o.mode || 'edit' });
    delete body.aspect;
    if (o.mode === 'img2img' && o.strength) body.strength = o.strength;
  }
  let r, j;
  try {
    r = await fetch('/media/generate', { method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body) });
    j = await r.json().catch(() => ({}));
  } catch (e) {
    return _mediaFail(list, m, 'Couldn’t reach the server.');
  }
  if (r.status === 409 && j.needs_confirm) {
    Object.assign(md_, { state: 'confirm', provider: j.provider, cloudModel: j.model });
    return _mediaRender(list, _idxOf(list, m));
  }
  if (r.status === 409 && j.not_loaded) return _mediaNotLoaded(list, m, j.not_loaded);
  if (r.status === 409 && j.needs_edit_model) return _mediaFail(list, m, j.error || 'This model can’t work from pictures.');
  if (!r.ok) return _mediaFail(list, m, j.error || ('HTTP ' + r.status), j.not_setup);
  Object.assign(md_, { state: 'running', jobId: j.job_id, model: j.model, cloud: j.cloud,
    text: j.cloud ? 'Sent to the cloud…' : 'Sent to the model on this PC…' });
  _mediaRender(list, _idxOf(list, m));
  _ensureTimer();
  try {
    const res = await fetch(`/media/jobs/${encodeURIComponent(j.job_id)}`);
    if (!res.ok) throw new Error('HTTP ' + res.status);
    await readSSE(res, (ev, d) => {
      if (ev === 'progress') {
        md_.text = d.text || md_.text;
        md_.pct = (typeof d.pct === 'number') ? d.pct : null;
        _mediaRender(list, _idxOf(list, m));
      } else if (ev === 'done') {
        _mediaDone(list, m, d);
      } else if (ev === 'error') {
        if (d.not_loaded) _mediaNotLoaded(list, m, d.not_loaded);
        else _mediaFail(list, m, d.message || 'failed', false, d.cancelled);
      }
    });
  } catch (e) {
    if (md_.state === 'running') _mediaFail(list, m, 'Lost the connection while waiting - the file may still appear in your files.');
  }
}

/* the local image/video model isn't loaded: offer ▶ Load, then start by itself */
function _mediaNotLoaded(list, m, nl) {
  Object.assign(m.media, { state: 'not_loaded', lane: nl.lane, laneLabel: nl.label, canLoad: !!nl.can_load,
    loading: !!nl.loading, error: '' });
  _mediaRender(list, _idxOf(list, m));
  if (nl.loading) _mediaWaitLoaded(list, m);
}

async function _mediaLoadAndRun(list, m) {
  const md_ = m.media;
  md_.loading = true;
  md_.t0 = Date.now();
  _mediaRender(list, _idxOf(list, m));
  _ensureTimer();
  try {
    const r = await fetch(`/control/lanes/${encodeURIComponent(md_.lane)}/load`, { method: 'POST' });
    const j = await r.json().catch(() => ({}));
    if (!r.ok || j.error) throw new Error(j.error || ('HTTP ' + r.status));
  } catch (e) {
    md_.loading = false;
    return _mediaFail(list, m, e.message);
  }
  _mediaWaitLoaded(list, m);
}

async function _mediaWaitLoaded(list, m) {
  const md_ = m.media;
  const kind = md_.kind;
  for (let i = 0; i < 400 && md_.state === 'not_loaded'; i++) {       // up to ~20 minutes
    await new Promise(res => setTimeout(res, 3000));
    const st = await mediaLoadStatus();
    const s = st && st[kind];
    if (!s) continue;
    if (md_.state !== 'not_loaded') return;                // stopped while waiting
    if (s.state === 'loaded' || (s.ready && !s.needs_load)) {
      md_.loading = false;
      md_.saved = false;
      return _mediaStart(list, m, false);                 // ready: make the image now
    }
    if (s.state === 'failed') {
      md_.loading = false;
      return _mediaFail(list, m, 'The model failed to load — see Settings → Models & Jobs for why.');
    }
  }
}

function _mediaDone(list, m, d) {
  const md_ = m.media;
  if (!MEDIA.live.has(m)) return;                        // stopped meanwhile
  _mediaUnlive(m);
  Object.assign(md_, { state: 'done', model: d.model, where: d.where, ms: d.ms, files: d.files,
    source: d.source, secs: Math.round((Date.now() - md_.t0) / 1000) });
  m.content = d.markdown || '';
  m.modelDisplay = d.model;
  m.modelSource = d.source;
  _persistMedia(m);
  _mediaRender(list, _idxOf(list, m));
}

function _mediaFail(list, m, msg, notSetup, cancelled) {
  if (m.media.state === 'cancelled' && !MEDIA.live.has(m)) return;   // already stopped: keep "Stopped."
  _mediaUnlive(m);
  Object.assign(m.media, { state: cancelled ? 'cancelled' : 'error', error: msg, notSetup: !!notSetup });
  m.content = '';
  _persistMedia(m);
  _mediaRender(list, _idxOf(list, m));
}

/* ---- stopping: the chat's Stop, Clear, or deleting the chat also stops its images ---- */
function _mediaLive(m) { MEDIA.live.add(m); mediaSyncStop(); }
function _mediaUnlive(m) { MEDIA.live.delete(m); mediaSyncStop(); }

/* stop every image/video in progress that which(m) picks. -> how many */
function mediaStopJobs(which) {
  let n = 0;
  for (const m of Array.from(MEDIA.live)) {
    if (!which(m)) continue;
    const md_ = m.media;
    n++;
    // the server cancels the job on the model too (sd-server's own cancel)
    if (md_.jobId) fetch(`/media/jobs/${encodeURIComponent(md_.jobId)}`, { method: 'DELETE' }).catch(() => {});
    _mediaFail(md_.list || [], m, 'Stopped.', false, true);
  }
  return n;
}
window.mediaStopJobs = mediaStopJobs;

/* the images of one chat (by saved chat id, or by message list for a chat not saved yet) */
function mediaStopChat(sid, list) {
  return mediaStopJobs(m => (sid != null && m.media.sid != null && String(m.media.sid) === String(sid))
    || (!!list && m.media.list === list));
}
window.mediaStopChat = mediaStopChat;

function mediaBusyIn(list) {
  for (const m of MEDIA.live) if (m.media.list === list) return true;
  return false;
}
window.mediaBusyIn = mediaBusyIn;

/* Stop shows while the open chat has an image in progress (Send stays: you can keep chatting) */
function mediaSyncStop() {
  const b = $('btn-abort');
  if (!b || (typeof generating !== 'undefined' && generating)) return;
  b.style.display = typeof messages !== 'undefined' && mediaBusyIn(messages) ? '' : 'none';
}
window.mediaSyncStop = mediaSyncStop;

function _mediaMeta(md_) {
  return { kind: md_.kind, prompt: md_.prompt, opts: md_.opts, state: md_.state, model: md_.model,
    where: md_.where, secs: md_.secs, error: md_.error || undefined, files: md_.files };
}

function _persistMedia(m) {
  const md_ = m.media;
  if (!md_.sid || md_.saved) return;
  md_.saved = true;
  const text = md_.state === 'done' ? m.content
    : `(${md_.state === 'cancelled' ? 'cancelled' : 'couldn’t make the ' + md_.kind}: ${md_.error || ''})`;
  persistMsgForSession(md_.sid, 'assistant', text,
    { media: _mediaMeta(md_), modelDisplay: m.modelDisplay, modelSource: m.modelSource });
}

function _ensureTimer() {
  if (MEDIA.timer) return;
  MEDIA.timer = setInterval(() => {
    const els = document.querySelectorAll('[data-media-elapsed]');
    if (!els.length) { clearInterval(MEDIA.timer); MEDIA.timer = 0; return; }
    els.forEach(el => {
      const m = typeof messages !== 'undefined' && messages[+el.dataset.mediaElapsed];
      if (m && m.media && m.media.t0) el.textContent = _fmtClock((Date.now() - m.media.t0) / 1000);
    });
  }, 1000);
}

/* the assistant bubble while a job runs / asks / failed (chat.js bubbleHtml) */
function mediaCardHtml(m, idx) {
  const md_ = m.media || {};
  const word = MEDIA_WORD[md_.kind] || 'file';
  const icon = md_.kind === 'video' ? '🎬' : '🎨';
  const prompt = `<div class="media-prompt">“${esc(md_.prompt || '')}”</div>`;
  if (md_.state === 'confirm') {
    return `<div class="media-live media-confirm" data-media-card="${idx}" role="alert">
      <div>💳 This ${word} will be made by <b>${esc(md_.provider || 'a cloud provider')}</b>${md_.cloudModel ? ` (${esc(md_.cloudModel)})` : ''} in the cloud and <b>may cost money</b>.</div>
      ${prompt}
      <div class="media-actions-row">
        <button type="button" class="btn accent" data-media="confirm" data-idx="${idx}">Make it</button>
        <button type="button" class="btn ghost" data-media="dismiss" data-idx="${idx}">Cancel</button>
      </div></div>`;
  }
  if (md_.state === 'not_loaded') {
    const what = md_.laneLabel ? `<b>${esc(md_.laneLabel)}</b>` : `the ${word} model`;
    if (md_.loading) {
      const t = md_.t0 ? _fmtClock((Date.now() - md_.t0) / 1000) : '0:00';
      return `<div class="media-live" data-media-card="${idx}" role="status" aria-live="polite">
        <div class="media-live-head"><span class="ac-shimmer"></span>
          <span>${icon} Loading ${what} on this PC · <span data-media-elapsed="${idx}">${t}</span></span></div>
        ${prompt}
        <div class="media-live-text dim">Big models take a few minutes to load. Your ${word} starts as soon as it’s ready, and the model stays loaded for the next one.</div>
      </div>`;
    }
    const action = md_.canLoad
      ? `<button type="button" class="btn accent" data-media="load" data-idx="${idx}">▶ Load it and make the ${word}</button>`
      : `<span class="dim">Ask an admin to load it.</span>`;
    return `<div class="media-live media-confirm" data-media-card="${idx}" role="note">
      <div>${icon} ${what} runs on this PC and isn’t loaded yet.</div>${prompt}
      <div class="media-actions-row">${action}
        <button type="button" class="btn ghost" data-media="dismiss" data-idx="${idx}">Cancel</button>
      </div></div>`;
  }
  if (md_.state === 'error' || md_.state === 'cancelled') {
    const setup = md_.notSetup ? ` <a href="${MEDIA_SETUP_URL}">Set it up</a>` : '';
    const head = md_.state === 'cancelled' ? `⏹ Stopped waiting for the ${word}.`
      : `⚠ Couldn’t make the ${word}: ${esc(md_.error || '')}${setup}`;
    return `<div class="media-live media-error" data-media-card="${idx}" role="note">
      <div>${head}</div>${prompt}
      <div class="media-actions-row">
        <button type="button" class="btn ghost" data-media="retry" data-idx="${idx}">↻ Try again</button>
        <button type="button" class="btn ghost" data-media="edit" data-idx="${idx}">✎ Edit description</button>
      </div></div>`;
  }
  const who = md_.model ? ` with <b>${esc(md_.model)}</b>` : '';
  const pct = typeof md_.pct === 'number'
    ? `<div class="media-bar" role="progressbar" aria-valuemin="0" aria-valuemax="100" aria-valuenow="${md_.pct}"><span style="width:${Math.max(3, Math.min(100, md_.pct))}%"></span></div>` : '';
  const t = md_.t0 ? _fmtClock((Date.now() - md_.t0) / 1000) : '0:00';
  return `<div class="media-live" data-media-card="${idx}" role="status" aria-live="polite">
    <div class="media-live-head"><span class="ac-shimmer"></span>
      <span>${icon} Making ${word === 'image' ? 'an image' : 'a video'}${who} · <span data-media-elapsed="${idx}">${t}</span></span></div>
    ${prompt}
    <div class="media-live-text dim">${esc(md_.text || '')}${md_.kind === 'video' ? ' · videos can take several minutes' : ''}</div>
    ${pct}
    <div class="media-actions-row">
      <button type="button" class="btn ghost" data-media="cancel" data-idx="${idx}">Cancel</button>
    </div></div>`;
}
window.mediaCardHtml = mediaCardHtml;

/* small row under a finished image/video */
function mediaActionsHtml(m, idx) {
  const md_ = m.media;
  if (!md_ || md_.state !== 'done') return '';
  const secs = md_.secs ? ` · ${md_.secs}s` : '';
  return `<div class="media-done-row">
    <span class="dim">Made with ${esc(md_.model || '?')}${md_.where ? ' (' + esc(md_.where) + ')' : ''}${secs}</span>
    <button type="button" class="btn ghost media-mini" data-media="again" data-idx="${idx}" title="Same description, new variation">↻ Make another</button>
    ${md_.kind === 'image' && (md_.files || []).some(f => /^image\//.test(f.mime || '')) && (_picCaps().img2img || _picCaps().refs)
      ? `<button type="button" class="btn ghost media-mini" data-media="editpic" data-idx="${idx}" title="Start a new image from this one">✏️ Edit this picture</button>` : ''}
    <button type="button" class="btn ghost media-mini" data-media="edit" data-idx="${idx}">✎ Edit description</button>
    <button type="button" class="btn ghost media-mini" data-media="copy" data-idx="${idx}">⧉ Copy description</button>
  </div>`;
}
window.mediaActionsHtml = mediaActionsHtml;

function mediaBusy(m) {
  return !!(m && m.media && m.media.state !== 'done');
}
window.mediaBusy = mediaBusy;

document.addEventListener('click', async e => {
  const b = e.target.closest && e.target.closest('[data-media]');
  if (!b || typeof messages === 'undefined') {
    const mb = e.target.closest && e.target.closest('[data-mic]');
    if (mb) micStop(mb.dataset.mic === 'cancel');
    return;
  }
  const list = messages;
  const m = list[+b.dataset.idx];
  if (!m || !m.media) return;
  const md_ = m.media;
  const act = b.dataset.media;
  if (act === 'cancel' && md_.jobId) {
    b.disabled = true;
    fetch(`/media/jobs/${encodeURIComponent(md_.jobId)}`, { method: 'DELETE' }).catch(() => {});
  } else if (act === 'confirm') {
    _mediaStart(list, m, true);
  } else if (act === 'load') {
    b.disabled = true;
    _mediaLoadAndRun(list, m);
  } else if (act === 'dismiss') {
    _mediaFail(list, m, '', false, true);
  } else if (act === 'retry') {
    if (md_.restored) { mediaRun(md_.kind, md_.prompt); return; }
    md_.saved = false;
    _mediaStart(list, m, false);
  } else if (act === 'again') {
    const again = { seed: Math.floor(Math.random() * 2147483647) };
    if (md_.inputs && md_.inputs.length)
      again.pics = { inputs: md_.inputs, mode: (md_.opts || {}).mode, strength: (md_.opts || {}).strength, count: md_.inputs.length };
    mediaRun(md_.kind, md_.prompt, again);
  } else if (act === 'editpic') {
    const f = (md_.files || []).find(x => /^image\//.test(x.mime || ''));
    if (!f) return;
    MEDIA.editPics = [{ path: f.path, name: f.name || 'picture' }];
    const cm = $('comp-mode');
    if (cm && !cm.hidden) { cm.value = 'image'; try { localStorage.setItem('comp_mode', 'image'); } catch (_) {} }
    _paintCompMode();
    const input = $('input');
    if (input) { input.value = ''; input.focus(); }
  } else if (act === 'edit') {
    const input = $('input');
    if (!input) return;
    input.value = md_.prompt;
    if (typeof armCmd === 'function') { armCmd(md_.kind); input.value = md_.prompt; }
    input.focus();
    input.dispatchEvent(new Event('input', { bubbles: true }));
  } else if (act === 'copy') {
    try { await navigator.clipboard.writeText(md_.prompt); toast('Description copied'); } catch (_) {}
  }
});

/* ---------------- init ---------------- */
(function initMedia() {
  const b = $('btn-mic');
  if (b) b.addEventListener('click', () => { if (MEDIA.rec) micStop(false); else micStart(); });
  const input = $('input');
  if (input) input.addEventListener('blur', () => { MEDIA.sel = [input.selectionStart, input.selectionEnd, input.value]; });
  document.addEventListener('keydown', e => {
    if (e.key === 'Escape' && MEDIA.rec) { e.preventDefault(); micStop(true); }
  });
  const cm = $('comp-mode');
  if (cm) {
    try { if (localStorage.getItem('comp_mode') === 'image') cm.value = 'image'; } catch (_) {}
    cm.addEventListener('change', () => {
      try { localStorage.setItem('comp_mode', cm.value); } catch (_) {}
      _paintCompMode();
      if (input) input.focus();
    });
  }
  mediaLoadStatus();
  // pick up models added in Settings (another tab) when coming back
  document.addEventListener('visibilitychange', () => { if (!document.hidden) mediaLoadStatus(); });
})();

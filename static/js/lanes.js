/* ---------------- lanes.js (Settings → Models & Jobs) ----------------
 * Plain-language front end for core/lanes.py:
 *   "model"  = a lane (a local llama-server or one of your cloud models)
 *   "job"    = a fixed kind of work (summarizing, commit messages, checking answers…)
 * Simple view: preset + three dropdowns. Advanced view: every model, every job.
 * Every save shows an Undo toast; job changes are previewed in the
 * "who handles what" map before they are saved. */

const LN = {
  d: null,               // GET /control/lanes
  view: 'simple',
  draft: {},             // job -> lane, unsaved
  asDefault: false,
  files: null,           // /control/lanes/files (model files in Models/orchestrator), loaded on demand
  vram: null,            // /control/vram
  tests: {},             // lane -> {ok, text}
  presetPreview: null,
};

const KIND_TEXT = { chat: 'Text', vision: 'Image reader', embed: 'Search memory',
                    image_gen: 'Image maker', video_gen: 'Video maker', stt: 'Speech to text' };
const KIND_NEED = { chat: 'a text model', vision: 'an image model', embed: 'a search-memory (embedding) model',
                    image_gen: 'an image maker', video_gen: 'a video maker', stt: 'a speech-to-text model' };
const KIND_OK = { chat: ['chat', 'vision'], vision: ['vision'], embed: ['embed'],
                  image_gen: ['image_gen'], video_gen: ['video_gen'], stt: ['stt'] };
const MEDIA_KINDS = ['image_gen', 'video_gen', 'stt'];
const MEDIA_JOBS = [
  { job: 'image_gen', kind: 'image_gen', icon: '🎨', title: 'Images',
    hint: 'Type /image and a description in chat, or let the agent make one.' },
  { job: 'video_gen', kind: 'video_gen', icon: '🎬', title: 'Videos',
    hint: 'Type /video and a description in chat. Clips take a few minutes.' },
  { job: 'transcribe', kind: 'stt', icon: '🎤', title: 'Speech to text',
    hint: 'The 🎤 button next to Send, and audio files you attach.' },
];
const PURPOSES = [
  { v: 'text', icon: '💬', t: 'Chat & tools', h: 'Answers, agent steps, reading images, search memory' },
  { v: 'image_gen', icon: '🎨', t: 'Make images', h: '/image in chat and the agent' },
  { v: 'video_gen', icon: '🎬', t: 'Make videos', h: '/video in chat and the agent' },
  { v: 'stt', icon: '🎤', t: 'Speech to text', h: 'The 🎤 button and attached audio' },
];
/* cloud models don't say what they do: guess media ones by name, so image/video/speech
   models aren't offered as a text model (and the right ones come first for media) */
const MEDIA_MODEL_RX = {
  image_gen: /(gpt-image|dall-?e|imagen|flux|stable-diffusion|sdxl|seedream|ideogram|image-gen)/i,
  video_gen: /(sora|veo-|kling|runway|seedance|wan-?2|ltx-?video|hailuo)/i,
  stt: /(whisper|transcribe|speech-to-text|\bstt\b)/i,
};
function guessMediaKind(c) {
  const s = `${c.key} ${c.display}`;
  for (const [k, rx] of Object.entries(MEDIA_MODEL_RX)) if (rx.test(s)) return k;
  return null;
}
function textCloudModels() {
  return (LN.d.cloud_models || []).filter(c => !guessMediaKind(c) && !/(-tts\b|text-to-speech)/i.test(`${c.key} ${c.display}`));
}
/* stable-diffusion.cpp model presets (image models on this PC). Files come from
   Models/orchestrator/image-models; the numbers are each family's usual defaults. */
const SD_PRESETS = [
  { v: 'z-image', t: 'Z-Image Turbo', h: 'Fast (8 steps), fits one A770',
    steps: 8, cfg_scale: 1.0, sampler: 'euler', flow_shift: null, offload_to_cpu: false },
  { v: 'qwen-image', t: 'Qwen Image 2.1', h: 'Best quality, slower — keeps weights in RAM. Add its vision weights to edit / combine pictures',
    steps: 20, cfg_scale: 6.0, sampler: 'euler', flow_shift: null, offload_to_cpu: true },
  { v: 'custom', t: 'Custom', h: 'Set the numbers yourself' },
];
/* image sizes (square; core/media.py SIZE_SIDES) */
const SD_SIZES = [['small', 'Small · 512×512 (fastest)'], ['medium', 'Medium · 768×768'],
  ['large', 'Large · 1024×1024'], ['xlarge', 'Extra large · 1280×1280 (slowest)']];
/* which file is which (the user can change each pick) */
function sdGuessRole(name) {
  const n = String(name || '').toLowerCase();
  if (/mmproj|vision|[^a-z]vit[^a-z]/.test(n)) return 'llm_vision';
  if (/(^|[^a-z])(vae|ae)[^a-z]|vae|_ae\.|^ae\./.test(n)) return 'vae';
  if (/(qwen2\.5-vl|qwen3|umt5|t5xxl|text[_-]?encoder|clip|llm|gemma|mistral)/.test(n)) return 'llm';
  return 'diffusion_model';
}
function sdEngine(l) { return l && l.engine === 'sdcpp'; }

function purposeOf(kind) { return MEDIA_KINDS.includes(kind) ? kind : 'text'; }
function isMedia(kind) { return MEDIA_KINDS.includes(kind); }

const PRESETS = [
  { mode: 'all-local', icon: '🔒', name: 'Private', sub: 'Everything on this PC',
    what: ['The main model and all helpers run on this PC.', 'Nothing is sent to a cloud provider by the agent.', 'Needs the main model loaded from the top bar.',
           'Images, videos and speech keep the models you picked under “Create & listen”.'] },
  { mode: 'main-local-rest-cloud', icon: '⚖', name: 'Balanced', sub: 'Local main model, cloud helpers',
    what: ['The main model runs on this PC.', 'Quick helper steps use your cloud helper model.', 'Card numbers are always masked before anything leaves this PC.'], needs: 'helper' },
  { mode: 'main-cloud-rest-local', icon: '✨', name: 'Best quality', sub: 'Cloud main model, local helpers',
    what: ['Your cloud model does the thinking and planning.', 'Quick helper steps stay on this PC.', 'Card numbers are always masked before anything leaves this PC.'], needs: 'main' },
  { mode: 'no-orchestration', icon: '🎯', name: 'Main model only', sub: 'One model does every step',
    what: ['Every step goes to your main model — on this PC or in the cloud, whichever is picked at the top.', 'No helper model is used.', 'Card numbers are always masked before anything leaves this PC.'] },
];

/* ---------- data ---------- */
async function lanesLoad() {
  const box = $('lanes-content');
  if (!box) return;
  try {
    const r = await fetch('/control/lanes');
    const d = await r.json();
    if (!r.ok || d.error) throw new Error(d.error || ('HTTP ' + r.status));
    LN.d = d;
    try { LN.view = localStorage.getItem('lanes_view') || 'simple'; } catch (_) {}
    lanesRender();
    lnPollLoading();
  } catch (e) {
    box.innerHTML = `<div class="mon-empty">Couldn't load models: ${esc(e.message)}</div>`;
  }
}
window.lanesLoad = lanesLoad;

/* an sd.cpp model takes a while to load: refresh until it's done */
let _lnPoll = null;
function lnPollLoading() {
  clearTimeout(_lnPoll);
  if ((LN.d.lanes || []).some(l => l.state === 'loading')) _lnPoll = setTimeout(lanesLoad, 2000);
}
async function lnLoadModel(name) {
  try {
    await lanesApi(`/control/lanes/${encodeURIComponent(name)}/load`, 'POST');
    toast('Loading — it stays loaded until you unload it');
  } catch (e) { toast('Couldn’t load: ' + e.message, true); }
  lanesRender();
  lnPollLoading();
}

async function lanesApi(url, method, body) {
  const r = await fetch(url, { method, headers: { 'Content-Type': 'application/json' },
                               body: body ? JSON.stringify(body) : undefined });
  const d = await r.json().catch(() => ({}));
  if (!r.ok || d.error) throw new Error(d.error || ('HTTP ' + r.status));
  if (d.lanes) LN.d = Object.assign(LN.d || {}, d);
  return d;
}

function laneByName(n) { return (LN.d.lanes || []).find(l => l.name === n); }
function jobSpec(j) { return (LN.d.jobs || []).find(x => x.job === j); }
function effMap() { return Object.assign({}, LN.d.role_map, LN.draft); }
function where(l) { return l && l.cloud_key ? 'cloud' : 'local'; }
function laneLabel(n) { const l = laneByName(n); return l ? l.label : n; }

/* why `lane` can't do `job` (null = it can) -- mirrors core/lanes.validate_mapping */
function whyNot(job, lane) {
  const s = jobSpec(job), l = laneByName(lane);
  if (!s || !l) return 'not available';
  if (!(KIND_OK[s.kind] || [s.kind]).includes(l.kind)) return 'needs ' + KIND_NEED[s.kind];
  if (s.local_only && !l.local) return 'always runs on this PC';
  if (s.local_first && !l.local && !((LN.d.media || {}).allow_cloud_audio)) return 'stays on this PC unless an admin allows cloud speech';
  if (job === 'agent.reason' && l.local && l.name !== 'main' && !l.cloud_key) return 'needs the main model or a cloud model';
  return null;
}

/* ---------- undo ---------- */
function undoToast(msg, undoFn) {
  toast(msg, { duration: 7000, actions: [{ label: 'Undo', onClick: async () => {
    try { await undoFn(); toast('Undone'); lanesRender(); } catch (e) { toast('Undo failed: ' + e.message, true); }
  } }] });
}

/* ---------- render ---------- */
function lanesRender() {
  const box = $('lanes-content');
  if (!box || !LN.d) return;
  let intro = '';
  try {
    if (!localStorage.getItem('lanes_intro_seen')) {
      intro = `<div class="ln-intro" role="note">
        <b>New:</b> you can now add more models and choose which one does each job.
        Start with the <b>Simple</b> view below — nothing changes until you pick something.
        <button type="button" class="btn ghost ln-small" data-ln="intro-ok">Got it</button></div>`;
    }
  } catch (_) {}
  const simple = LN.view !== 'advanced';
  box.innerHTML = `${intro}
    <div class="ln-top">
      <div class="ln-seg" role="radiogroup" aria-label="Settings view">
        <button type="button" role="radio" aria-checked="${simple}" class="${simple ? 'on' : ''}" data-ln="view" data-v="simple">Simple</button>
        <button type="button" role="radio" aria-checked="${!simple}" class="${!simple ? 'on' : ''}" data-ln="view" data-v="advanced">Advanced</button>
      </div>
      <span class="dim ln-top-hint">${simple ? 'The three choices most people need.' : 'Every model and every job.'}</span>
    </div>
    ${mapHtml()}
    ${simple ? simpleHtml() : advancedHtml()}`;
}

function mapHtml() {
  const m = effMap();
  const chips = (LN.d.jobs || []).map(j => {
    const pending = Object.prototype.hasOwnProperty.call(LN.draft, j.job);
    if (!m[j.job]) {
      return `<li class="ln-chip ln-off${pending ? ' ln-pending' : ''}" title="${esc(j.hint)}">
        <span class="ln-chip-job">${esc(j.label)}</span>
        <span class="ln-chip-arrow" aria-hidden="true">→</span>
        <span class="ln-chip-lane">○ not set up</span>
        ${pending ? '<span class="ln-chip-tag">not saved</span>' : ''}</li>`;
    }
    const l = laneByName(m[j.job]);
    const w = where(l);
    return `<li class="ln-chip ln-${w}${pending ? ' ln-pending' : ''}" title="${esc(j.hint)}">
      <span class="ln-chip-job">${esc(j.label)}</span>
      <span class="ln-chip-arrow" aria-hidden="true">→</span>
      <span class="ln-chip-lane">${w === 'cloud' ? '☁' : '🖥'} ${esc(l ? l.label : m[j.job])}<span class="sr-only">${w === 'cloud' ? ' (cloud)' : ' (on this PC)'}</span></span>
      ${pending ? '<span class="ln-chip-tag">not saved</span>' : ''}
    </li>`;
  }).join('');
  return `<section class="ln-map" aria-label="Who handles what">
    <div class="ln-sec-title">Who handles what <span class="dim">· <span class="ln-legend ln-local">🖥 on this PC</span> <span class="ln-legend ln-cloud">☁ cloud</span> <span class="ln-legend ln-off">○ not set up</span></span></div>
    <ul class="ln-chips">${chips}</ul></section>`;
}

/* ---------- simple view ---------- */
function curPreset() {
  try { return localStorage.getItem('agent_engine') || 'all-local'; } catch (_) { return 'all-local'; }
}

function simpleHtml() {
  const d = LN.d;
  const main = laneByName('main') || {};
  const exec = laneByName('executor') || {};
  const cms = textCloudModels();
  const cur = curPreset();
  const pv = LN.presetPreview;
  const presetCards = PRESETS.map(p => `
    <button type="button" role="radio" aria-checked="${cur === p.mode}" class="ln-preset${cur === p.mode ? ' on' : ''}${pv === p.mode ? ' preview' : ''}" data-ln="preset" data-mode="${p.mode}">
      <span class="ln-preset-icon" aria-hidden="true">${p.icon}</span>
      <span class="ln-preset-name">${esc(p.name)}</span>
      <span class="ln-preset-sub">${esc(p.sub)}</span>
      ${cur === p.mode ? '<span class="ln-preset-cur">In use</span>' : ''}
    </button>`).join('');
  let preview = '';
  if (pv && pv !== cur) {
    const p = PRESETS.find(x => x.mode === pv);
    const missing = (p.needs === 'main' && !main.cloud_key) ? 'Pick a cloud <b>Main model</b> below first, or it will use the one on this PC.'
      : (p.needs === 'helper' && !(exec.cloud_key)) ? 'Pick a cloud <b>Helper model</b> below first, or helpers will stay on this PC.' : '';
    preview = `<div class="ln-preview" role="region" aria-label="What changes">
      <div class="ln-sec-title">What changes with “${esc(p.name)}”</div>
      <ul>${p.what.map(w => `<li>${esc(w)}</li>`).join('')}</ul>
      ${missing ? `<div class="ln-warn">⚠ ${missing}</div>` : ''}
      <div class="ln-row-btns">
        <button type="button" class="btn accent ln-small" data-ln="preset-apply" data-mode="${p.mode}">Use ${esc(p.name)}</button>
        <button type="button" class="btn ghost ln-small" data-ln="preset-cancel">Cancel</button>
      </div></div>`;
  }
  const opt = (v, label, sel, dis) => `<option value="${esc(v)}"${sel ? ' selected' : ''}${dis ? ' disabled' : ''}>${esc(label)}</option>`;
  const cloudOpts = sel => cms.map(c => opt(c.key, `☁ ${c.display} · ${c.provider}`, sel === c.key)).join('');
  const noCloud = !cms.length
    ? `<div class="dim ln-hint">${(d.cloud_models || []).length ? 'No cloud text models yet (your others make images, videos or speech)'
        : 'No cloud models yet'} — <a href="#sec-cloud" data-ln="goto-cloud">add a cloud provider</a> to see them here.</div>` : '';
  const helperAuto = !exec.cloud_key || exec.cloud_key === main.cloud_key;
  const vf = d.verification || {};
  const verifyLane = effMap().verify;
  const checkers = (d.lanes || []).filter(l => !whyNot('verify', l.name));
  const vmode = vf.mode || 'off';
  const radio = (v, label, hint) => `<label class="ln-radio"><input type="radio" name="ln-vmode" value="${v}"${vmode === v ? ' checked' : ''} data-ln="vmode"> <span><b>${label}</b><small>${hint}</small></span></label>`;
  return `
    <section class="ln-sec" aria-labelledby="ln-p-title">
      <div class="ln-sec-title" id="ln-p-title">Where should the agent run?</div>
      <div class="ln-presets" role="radiogroup" aria-labelledby="ln-p-title">${presetCards}</div>
      ${preview}
    </section>
    <div class="ln-cards">
      <section class="ln-card" aria-labelledby="ln-main-t">
        <div class="ln-card-t" id="ln-main-t">🧠 Main model</div>
        <div class="ln-hint dim">Does the thinking: chat answers and the agent's hard steps.</div>
        <label class="sr-only" for="ln-main-sel">Main model</label>
        <select id="ln-main-sel" data-ln="main-sel">
          ${opt('local', '🖥 On this PC — ' + (main.model ? String(main.model).split(/[\\/]/).pop() : 'model from the top bar'), !main.cloud_key)}
          ${cloudOpts(main.cloud_key)}
        </select>${noCloud}
        ${statusLine(main)}
      </section>
      <section class="ln-card" aria-labelledby="ln-exec-t">
        <div class="ln-card-t" id="ln-exec-t">⚡ Helper model</div>
        <div class="ln-hint dim">Quick jobs: routine tool calls, summaries, commit messages.</div>
        <label class="sr-only" for="ln-exec-sel">Helper model</label>
        <select id="ln-exec-sel" data-ln="exec-sel">
          ${opt('auto', 'Same place as the main model', helperAuto && !!main.cloud_key)}
          ${opt('local', '🖥 On this PC — ' + (exec.model ? String(exec.model).split(/[\\/]/).pop() : 'Fast helper'), !exec.cloud_key)}
          ${cloudOpts(helperAuto ? null : exec.cloud_key)}
        </select>
        ${statusLine(exec)}
      </section>
      <section class="ln-card" aria-labelledby="ln-ver-t">
        <div class="ln-card-t" id="ln-ver-t">🛡 Answer check</div>
        <div class="ln-hint dim">A second model double-checks answers. You can also switch it per message with 🛡 next to Send.</div>
        <div role="radiogroup" aria-labelledby="ln-ver-t" class="ln-radios">
          ${radio('off', 'Off', 'Answers are shown as they are.')}
          ${radio('badge', 'Check after', 'The answer appears right away; a ✅ or ⚠ badge follows.')}
          ${radio('gate', 'Check before showing', 'You see “Double-checking…” first, then the checked (and fixed) answer.')}
        </div>
        <label class="ln-lbl" for="ln-ver-sel">Checked by</label>
        <select id="ln-ver-sel" data-ln="verify-sel" ${vmode === 'off' ? 'disabled' : ''}>
          ${checkers.map(l => opt(l.name, (where(l) === 'cloud' ? '☁ ' : '🖥 ') + l.label, verifyLane === l.name)).join('')}
        </select>
        ${verifyLane === 'main' && vmode !== 'off' ? '<div class="dim ln-hint">Tip: a different model than the one answering catches more mistakes.</div>' : ''}
      </section>
    </div>
    ${mediaSimpleHtml()}`;
}

/* ---------- create & listen: images, videos, speech ---------- */
function mediaSimpleHtml() {
  const d = LN.d, m = effMap();
  const cards = MEDIA_JOBS.map(mj => {
    const cur = m[mj.job];
    const l = cur ? laneByName(cur) : null;
    const choices = (d.lanes || []).filter(x => x.kind === mj.kind && !whyNot(mj.job, x.name));
    const canAdd = d.can_edit_local || mj.kind !== 'stt' || (d.media || {}).allow_cloud_audio || (d.cloud_models || []).length;
    const sel = choices.length ? `<label class="sr-only" for="ln-mj-${mj.job}">Model for ${esc(mj.title)}</label>
      <select id="ln-mj-${mj.job}" data-ln="media-job" data-job="${mj.job}">
        <option value=""${l ? '' : ' selected'}>Off — not set up</option>
        ${choices.map(x => `<option value="${esc(x.name)}"${l && l.name === x.name ? ' selected' : ''}>${where(x) === 'cloud' ? '☁ ' : '🖥 '}${esc(x.label)}</option>`).join('')}
      </select>` : '';
    return `<section class="ln-card" aria-labelledby="ln-mj-t-${mj.job}">
      <div class="ln-card-t" id="ln-mj-t-${mj.job}">${mj.icon} ${esc(mj.title)}</div>
      <div class="ln-hint dim">${esc(mj.hint)}</div>
      ${sel}
      ${l ? statusLine(l) : '<div class="ln-status ln-st-idle"><span aria-hidden="true">○</span> Not set up yet</div>'}
      ${l && l.edit_caps ? editCapsHtml(l) : ''}
      <div class="ln-row-btns">
        ${canAdd ? `<button type="button" class="btn ${choices.length ? 'ghost' : 'accent'} ln-small" data-ln="add-media" data-kind="${mj.kind}">＋ ${choices.length ? 'Add another' : 'Set it up'}</button>` : ''}
        ${l && sdEngine(l) && d.can_edit_local ? sdLoadBtn(l) : ''}
        ${l ? `<button type="button" class="btn ghost ln-small" data-ln="test" data-name="${esc(l.name)}">Test</button>` : ''}
      </div>
      ${l ? testHtml(l.name) : ''}
      ${mj.kind === 'stt' ? sttSetupHtml(l) : ''}
      ${mj.kind === 'image_gen' ? sdSetupHtml(l) : ''}
    </section>`;
  }).join('');
  return `<section class="ln-sec" aria-labelledby="ln-media-t">
    <div class="ln-sec-title" id="ln-media-t">Create &amp; listen</div>
    <div class="ln-cards">${cards}</div>
    ${d.can_edit_local ? mediaAdminHtml() : ''}
  </section>`;
}

function sdLoadBtn(l) {
  if (l.state === 'loading') {
    const secs = l.loading_since ? Math.max(0, Math.round(Date.now() / 1000 - l.loading_since)) : 0;
    return `<button type="button" class="btn ghost ln-small" disabled>Loading… ${Math.floor(secs / 60)}:${String(secs % 60).padStart(2, '0')}</button>`;
  }
  if (l.state === 'loaded') return `<button type="button" class="btn ghost ln-small" data-ln="stop" data-name="${esc(l.name)}">■ Unload</button>`;
  return `<button type="button" class="btn accent ln-small" data-ln="load" data-name="${esc(l.name)}"${l.files_ok ? '' : ' disabled'}>▶ Load</button>`;
}

function sdSetupHtml(l) {
  const media = LN.d.media || {};
  if (media.sd_found || (l && !sdEngine(l))) return '';
  if (!LN.d.can_edit_local) return '';
  const dirs = (media.sd_dirs || []).map(x => `<li><code>${esc(x)}</code></li>`).join('');
  const folder = (media.media_dirs || {}).image_gen || 'E:\\AI\\Models\\orchestrator\\image-models';
  return `<details class="ln-adv ln-setup"${l ? ' open' : ''}><summary>How to make images on this PC (one time)</summary>
    <ol class="ln-steps-list">
      <li>Get stable-diffusion.cpp's server program, <code>sd-server.exe</code>, in a <b>Vulkan</b> build (it uses the Arc GPUs).</li>
      <li>Put it (with the DLLs next to it) in one of these folders:<ul>${dirs}</ul></li>
      <li>Put the model files in <code>${esc(folder)}</code> — for Z-Image Turbo or Qwen Image 2.1: the diffusion model (.gguf), its text encoder (.gguf) and its VAE (.safetensors).</li>
      <li>Click <b>Set it up</b> above, choose <b>This PC</b>, and pick the files. Then press <b>▶ Load</b>.</li>
    </ol></details>`;
}

function sttSetupHtml(l) {
  const media = LN.d.media || {};
  if (media.whisper_found || (l && where(l) === 'cloud')) return '';
  if (!LN.d.can_edit_local) return '<div class="dim ln-hint">Ask an admin to set up Whisper for speech to text.</div>';
  const dirs = (media.whisper_dirs || []).map(x => `<li><code>${esc(x)}</code></li>`).join('');
  return `<details class="ln-adv ln-setup"${l ? ' open' : ''}><summary>How to set up Whisper (one time)</summary>
    <ol class="ln-steps-list">
      <li>Get whisper.cpp's server program, <code>whisper-server.exe</code>. A <b>Vulkan</b> build uses the Arc GPUs; the plain CPU build also works, just slower.</li>
      <li>Put it (with the DLLs next to it) in one of these folders:<ul>${dirs}</ul></li>
      <li>Download a model file into <code>${esc((media.media_dirs || {}).stt || helperFolder() + '/voice-models')}</code>, e.g. <code>ggml-large-v3-turbo-q5_0.bin</code> (about 550 MB) — best for Bangla and English.</li>
      <li>Click <b>Set it up</b> above and pick that file.</li>
    </ol></details>`;
}

function mediaAdminHtml() {
  const media = LN.d.media || {};
  const lim = media.limits || {};
  return `<details class="ln-adv"><summary>Admin: cloud limits and cloud speech</summary>
    <label class="ln-check"><input type="checkbox" data-ln="allow-cloud-audio"${media.allow_cloud_audio ? ' checked' : ''}>
      Allow cloud speech to text</label>
    <div class="ln-warn">⚠ Recordings can't be checked for card numbers or personal details before they are sent.
      Leave this off unless the provider is approved for this data (PCI DSS / Bangladesh Bank rules).</div>
    <div class="ln-lim">
      <label class="ln-lbl" for="ln-lim-image">Cloud images per person per day (0 = no limit)</label>
      <input id="ln-lim-image" type="number" min="0" max="10000" data-ln="lim-image" value="${esc(lim.image_per_day ?? 50)}">
      <label class="ln-lbl" for="ln-lim-video">Cloud videos per person per day (0 = no limit)</label>
      <input id="ln-lim-video" type="number" min="0" max="1000" data-ln="lim-video" value="${esc(lim.video_per_day ?? 5)}">
    </div>
    <div class="dim ln-hint">Image models and Whisper on this PC have no daily limit.</div>
  </details>`;
}

function testHtml(name) {
  const t = LN.tests[name];
  if (!t) return '';
  const img = t.image ? `<img class="ln-test-img" src="${esc(t.image)}" alt="Image made by the test">` : '';
  const full = t.needsConfirm ? ` <button type="button" class="ln-link" data-ln="test-full" data-name="${esc(name)}">Run a real test (makes a short video — may take minutes)</button>` : '';
  return `<div class="ln-test ${t.ok ? 'ok' : 'bad'}" role="status">${t.ok ? '✅' : '✕'} ${esc(t.text)}${full}${img}</div>`;
}

/* "Edits pictures: ✓ change · ✓ combine (up to 4)" on a local image model */
function editCapsHtml(l) {
  const c = l.edit_caps || {};
  const combine = c.refs ? `✓ edit / combine (up to ${c.max_refs})`
    : '✕ edit / combine — add its vision weights (Edit)';
  return `<div class="ln-hint dim">Works from pictures: ${c.img2img ? '✓ change a picture' : '✕ change'} · ${esc(combine)} · 🔒 on this PC only</div>`;
}

function statusLine(l) {
  if (!l || !l.status) return '';
  const icon = { ok: '●', idle: '◐', warn: '▲', error: '✕' }[l.status.level] || '●';
  let fix = '';
  if (l.status.level === 'error' && l.local && l.name !== 'main' && LN.d.can_edit_local) {
    fix = ` <button type="button" class="ln-link" data-ln="edit" data-name="${esc(l.name)}">Fix</button>`;
  } else if (l.status.level === 'warn') {
    fix = ` <a href="#sec-cloud" class="ln-link" data-ln="goto-cloud">Fix</a>`;
  }
  return `<div class="ln-status ln-st-${l.status.level}"><span aria-hidden="true">${icon}</span> ${esc(l.status.text)}${fix}</div>`;
}

/* ---------- advanced view ---------- */
function advancedHtml() {
  const d = LN.d;
  const cards = (d.lanes || []).map(l => {
    const used = (l.used_by || []).map(j => (jobSpec(j) || {}).label || j);
    const canEdit = (l.owner === 'user') || (l.local && l.name !== 'main' && d.can_edit_local);
    const canDel = !l.builtin && canEdit;
    return `<li class="ln-model">
      <div class="ln-model-head">
        <span class="ln-model-name">${where(l) === 'cloud' ? '☁' : '🖥'} ${esc(l.label)}</span>
        <span class="ln-tag">${esc(KIND_TEXT[l.kind] || l.kind)}</span>
        <span class="ln-tag">${where(l) === 'cloud' ? 'Cloud' : 'This PC'}</span>
        ${l.owner === 'user' ? '<span class="ln-tag">Yours</span>' : ''}
        <code class="ln-id">${esc(l.name)}</code>
      </div>
      ${statusLine(l)}
      <div class="dim ln-hint">${used.length ? 'Does: ' + used.map(esc).join(', ') : 'Not used by any job yet.'}
        ${l.fallback ? ` · If it fails: ${esc(laneLabel(l.fallback))}` : ''}</div>
      ${testHtml(l.name)}
      <div class="ln-row-btns">
        <button type="button" class="btn ghost ln-small" data-ln="test" data-name="${esc(l.name)}">Test</button>
        ${sdEngine(l) && LN.d.can_edit_local ? sdLoadBtn(l)
          : l.local && l.name !== 'main' && l.loaded ? `<button type="button" class="btn ghost ln-small" data-ln="stop" data-name="${esc(l.name)}">Unload</button>` : ''}
        ${canEdit ? `<button type="button" class="btn ghost ln-small" data-ln="edit" data-name="${esc(l.name)}">Edit</button>` : ''}
        ${canDel ? `<button type="button" class="btn ghost ln-small ln-danger" data-ln="del" data-name="${esc(l.name)}">Remove</button>` : ''}
      </div></li>`;
  }).join('');

  const m = effMap();
  const rows = (d.jobs || []).map(j => {
    // media jobs only list models of their own type (and can be off)
    const pool = (d.lanes || []).filter(l => !isMedia(j.kind) || l.kind === j.kind);
    const opts = (d.defaults[j.job] ? '' : `<option value=""${m[j.job] ? '' : ' selected'}>Off — not set up</option>`)
      + pool.map(l => {
      const why = whyNot(j.job, l.name);
      return `<option value="${esc(l.name)}"${m[j.job] === l.name ? ' selected' : ''}${why ? ' disabled' : ''}>${where(l) === 'cloud' ? '☁ ' : '🖥 '}${esc(l.label)}${why ? ' — ' + esc(why) : ''}</option>`;
    }).join('');
    const isDefault = (m[j.job] || null) === (d.defaults[j.job] || null);
    return `<tr>
      <th scope="row"><span class="ln-job">${esc(j.label)}</span>
        <span class="ln-info" tabindex="0" role="img" aria-label="${esc(j.hint)}" title="${esc(j.hint)}">ⓘ</span>
        <div class="dim ln-hint">${esc(j.hint)}</div></th>
      <td><label class="sr-only" for="ln-job-${esc(j.job)}">Model for ${esc(j.label)}</label>
        <select id="ln-job-${esc(j.job)}" data-ln="job" data-job="${esc(j.job)}">${opts}</select>
        ${isDefault || !d.defaults[j.job] ? '' : `<button type="button" class="ln-link" data-ln="job-reset" data-job="${esc(j.job)}">Reset to recommended</button>`}</td>
    </tr>`;
  }).join('');
  const nDraft = Object.keys(LN.draft).length;
  return `
    <section class="ln-sec" aria-labelledby="ln-models-t">
      <div class="ln-sec-head"><div class="ln-sec-title" id="ln-models-t">Models</div>
        <button type="button" class="btn accent ln-small" data-ln="add">＋ Add a model</button></div>
      <ul class="ln-models">${cards}</ul>
    </section>
    <section class="ln-sec" aria-labelledby="ln-jobs-t">
      <div class="ln-sec-title" id="ln-jobs-t">Jobs</div>
      <table class="ln-jobs"><tbody>${rows}</tbody></table>
      ${nDraft ? `<div class="ln-savebar" role="region" aria-label="Unsaved changes">
        <span>${nDraft} unsaved change${nDraft > 1 ? 's' : ''} — see the map above</span>
        ${d.can_edit_local ? `<label class="ln-check"><input type="checkbox" data-ln="as-default"${LN.asDefault ? ' checked' : ''}> Make this the default for everyone</label>` : ''}
        <button type="button" class="btn accent ln-small" data-ln="jobs-save">Save</button>
        <button type="button" class="btn ghost ln-small" data-ln="jobs-discard">Discard</button></div>` : ''}
    </section>
    <section class="ln-sec">
      <label class="ln-check"><input type="checkbox" data-ln="fb-local"${d.fallback_local ? ' checked' : ''}>
        If a cloud model fails, retry on this PC</label>
      <div class="dim ln-hint">Off: a failed cloud request shows an error instead.</div>
    </section>`;
}

/* ---------- actions ---------- */
async function setMain(val) {
  const prev = (laneByName('main') || {}).cloud_key;
  const body = val === 'local' ? { clear: ['main'] } : { main: val };
  await lanesApi('/control/cloud/lanes', 'POST', body);
  await lanesLoad();
  undoToast('Main model changed', () => lanesApi('/control/cloud/lanes', 'POST', prev ? { main: prev } : { clear: ['main'] }).then(lanesLoad));
}

async function setHelper(val) {
  const e = laneByName('executor') || {};
  const wasAuto = !e.cloud_key || e.cloud_key === (laneByName('main') || {}).cloud_key;
  const prev = wasAuto ? { routing_mode: 'auto' } : { set: { executor: e.cloud_key }, routing_mode: 'custom' };
  let body;
  if (val === 'auto') body = { routing_mode: 'auto' };
  else if (val === 'local') body = { clear: ['executor'], routing_mode: 'custom' };
  else body = { set: { executor: val }, routing_mode: 'custom' };
  await lanesApi('/control/cloud/lanes', 'POST', body);
  if (val !== 'auto' && val !== 'local') { try { localStorage.setItem('cloud_model_override', val); } catch (_) {} }
  await lanesLoad();
  undoToast('Helper model changed', () => lanesApi('/control/cloud/lanes', 'POST', prev).then(lanesLoad));
}

async function saveVerify(upd) {
  const prev = Object.assign({}, LN.d.verification);
  await lanesApi('/control/verification', 'POST', upd);
  lanesRender();
  undoToast('Answer check updated', () => lanesApi('/control/verification', 'POST', { mode: prev.mode }));
}

async function saveJobs(map, asDefault) {
  const before = {};
  Object.keys(map).forEach(j => { before[j] = LN.d.role_map[j] === LN.d.defaults[j] ? null : LN.d.role_map[j]; });
  await lanesApi('/control/lanes/roles', 'POST', { map, as_default: !!asDefault });
  LN.draft = {};
  lanesRender();
  undoToast('Jobs saved', () => lanesApi('/control/lanes/roles', 'POST', { map: before, as_default: !!asDefault }));
}

async function testLane(name, full) {
  const l = laneByName(name) || {};
  LN.tests[name] = { ok: true, text: isMedia(l.kind) && l.kind !== 'stt'
    ? 'Testing… (making a small test image can take a little while)'
    : 'Testing… (a sleeping model may take a minute to start)' };
  lanesRender();
  try {
    const r = await lanesApi(`/control/lanes/${encodeURIComponent(name)}/test${full ? '?full=1' : ''}`, 'POST');
    if (!r.ok) LN.tests[name] = { ok: false, text: r.error || 'It did not answer.' };
    else if (isMedia(l.kind)) LN.tests[name] = { ok: true, text: `${r.sample || 'Works'} (${r.ms} ms)`, image: r.image,
                                                  needsConfirm: !!r.needs_confirm };
    else LN.tests[name] = { ok: true, text: `Works (${r.ms} ms)${r.sample ? ' — replied “' + r.sample + '”' : ''}` };
  } catch (e) {
    LN.tests[name] = { ok: false, text: e.message };
  }
  await lanesLoad();
}

/* ---------- modal helpers ---------- */
let _lnLastFocus = null;
function openModal(html, label) {
  closeModal();
  _lnLastFocus = document.activeElement;
  const wrap = document.createElement('div');
  wrap.id = 'ln-modal';
  wrap.className = 'ln-modal-bg';
  wrap.innerHTML = `<div class="ln-modal" role="dialog" aria-modal="true" aria-label="${esc(label)}">${html}</div>`;
  document.body.appendChild(wrap);
  wrap.addEventListener('mousedown', e => { if (e.target === wrap) closeModal(); });
  const first = wrap.querySelector('input:not([type=hidden]), select, button, [tabindex]');
  if (first) first.focus();
  return wrap;
}
function closeModal() {
  const m = document.getElementById('ln-modal');
  if (m) m.remove();
  if (_lnLastFocus && _lnLastFocus.focus) { try { _lnLastFocus.focus(); } catch (_) {} }
}
document.addEventListener('keydown', e => {
  const m = document.getElementById('ln-modal');
  if (!m) return;
  if (e.key === 'Escape') { e.preventDefault(); closeModal(); return; }
  if (e.key === 'Tab') {   // keep focus inside the dialog
    const f = [...m.querySelectorAll('button, select, input, a[href], [tabindex]:not([tabindex="-1"])')].filter(x => !x.disabled && x.offsetParent !== null);
    if (!f.length) return;
    const i = f.indexOf(document.activeElement);
    if (e.shiftKey && (i <= 0)) { e.preventDefault(); f[f.length - 1].focus(); }
    else if (!e.shiftKey && i === f.length - 1) { e.preventDefault(); f[0].focus(); }
  }
});

/* ---------- delete ---------- */
function deleteDialog(name) {
  const l = laneByName(name);
  const used = l.used_by || [];
  const targets = (LN.d.lanes || []).filter(x => x.name !== name && used.every(j => !whyNot(j, x.name)));
  const moveSel = used.length ? `
    <label class="ln-lbl" for="ln-move">${used.length} job${used.length > 1 ? 's use' : ' uses'} this model. Move ${used.length > 1 ? 'them' : 'it'} to:</label>
    <select id="ln-move">${targets.map(t => `<option value="${esc(t.name)}"${t.name === 'main' ? ' selected' : ''}>${esc(t.label)}</option>`).join('')}</select>
    <div class="dim ln-hint">${used.map(j => esc((jobSpec(j) || {}).label || j)).join(', ')}</div>` : '';
  openModal(`<h3>Remove “${esc(l.label)}”?</h3>
    ${moveSel}
    <div class="ln-row-btns ln-modal-btns">
      <button type="button" class="btn red ln-small" id="ln-del-go">Remove</button>
      <button type="button" class="btn ghost ln-small" id="ln-del-cancel">Cancel</button></div>`, 'Remove model');
  $('ln-del-cancel').onclick = closeModal;
  $('ln-del-go').onclick = async () => {
    const mv = $('ln-move') ? $('ln-move').value : '';
    try {
      await lanesApi(`/control/lanes?name=${encodeURIComponent(name)}${mv ? '&move_to=' + encodeURIComponent(mv) : ''}`, 'DELETE');
      closeModal();
      toast(`Removed “${l.label}”`);
      await lanesLoad();
    } catch (e) { toast(e.message, true); }
  };
}

/* ---------- add / edit wizard ---------- */
const WZ = { step: 1, data: {}, editing: null };

/* model files the wizard may pick (all under Models/orchestrator) */
function helperFiles(key) { return ((LN.files || {})[key]) || []; }
function helperFolder() { return (LN.files || {}).folder || 'E:\\AI\\Models\\orchestrator'; }

async function ensureLocalInventory() {
  if (!LN.files) {
    try {
      const r = await fetch('/control/lanes/files');
      LN.files = r.ok ? await r.json() : {};
    } catch (_) { LN.files = {}; }
  }
  if (!LN.vram) {
    try { const r = await fetch('/control/vram'); LN.vram = r.ok ? ((await r.json()).devices || []) : []; } catch (_) { LN.vram = []; }
  }
}

function slug(s) {
  let v = String(s || '').toLowerCase().replace(/[^a-z0-9]+/g, '-').replace(/^-+|-+$/g, '').slice(0, 32);
  if (!/^[a-z]/.test(v)) v = 'm-' + v;
  return v.length < 2 ? 'model-' + Date.now() % 1000 : v;
}

async function openWizard(editName, presetKind) {
  WZ.editing = editName || null;
  const l = editName ? laneByName(editName) : null;
  const canLocal = !!LN.d.can_edit_local;
  if (l) {
    WZ.data = {
      purpose: purposeOf(l.kind), backend: l.local ? 'local' : 'cloud', kind: l.kind, label: l.label, name: l.name,
      model: l.model, mmproj: l.mmproj, gpu: l.gpu, port: l.port, ctx: l.ctx, idle_unload_s: l.idle_unload_s,
      cloud: l.cloud_binding || l.cloud_key, fallback: l.fallback || '', jobs: [],
      timeout_s: l.timeout_s,
      threads: l.threads, language: l.language,
    };
    if (sdEngine(l)) {
      Object.assign(WZ.data, { backend: 'sdcpp', diffusion_model: l.diffusion_model, llm: l.llm, vae: l.vae,
        llm_vision: l.llm_vision, edit_refs: !!l.edit_refs, max_refs: l.max_refs, default_size: l.default_size,
        steps: l.steps, cfg_scale: l.cfg_scale, flow_shift: l.flow_shift, sampler: l.sampler,
        offload_to_cpu: !!l.offload_to_cpu, sd_preset: 'custom' });
    }
  } else {
    const purpose = presetKind ? purposeOf(presetKind) : 'text';
    WZ.data = { purpose, kind: purpose === 'text' ? 'chat' : purpose, jobs: [], fallback: purpose === 'text' ? 'main' : '',
                backend: canLocal ? 'local' : 'cloud' };
    _wzFixBackend(WZ.data);
  }
  WZ.step = editName ? 2 : 1;
  if (WZ.data.backend !== 'cloud') await ensureLocalInventory();
  wizardRender();
}

/* the backends a purpose can use, for this user: [{v, ok, why}] */
function _wzBackends(purpose) {
  const canLocal = !!LN.d.can_edit_local;
  const cloudOk = purpose !== 'stt' || !!(LN.d.media || {}).allow_cloud_audio;
  const out = [];
  // stable-diffusion.cpp on this PC: images now, videos next
  if (purpose === 'image_gen') out.push({ v: 'sdcpp', ok: canLocal, why: 'Only an admin can add these.' });
  if (purpose === 'video_gen') out.push({ v: 'sdcpp', ok: false, why: 'Coming next — use the cloud for now.' });
  // images and videos on this PC only come from stable-diffusion.cpp (above)
  if (purpose === 'text' || purpose === 'stt') out.push({ v: 'local', ok: canLocal, why: 'Only an admin can add these.' });
  out.push({ v: 'cloud', ok: cloudOk, why: 'An admin has to allow cloud speech first — recordings can’t be checked for card numbers.' });
  return out;
}

function _wzFixBackend(D) {
  const opts = _wzBackends(D.purpose);
  const cur = opts.find(o => o.v === D.backend);
  if (!cur || !cur.ok) {
    const first = opts.find(o => o.ok);
    D.backend = first ? first.v : D.backend;
  }
}

function wizardRender() {
  const s = WZ.step, D = WZ.data;
  const steps = ['What & where', 'Set it up', 'Jobs & backup'];
  const bar = `<ol class="ln-steps" aria-label="Steps">${steps.map((t, i) =>
    `<li class="${i + 1 === s ? 'on' : (i + 1 < s ? 'done' : '')}"${i + 1 === s ? ' aria-current="step"' : ''}><span>${i + 1}</span> ${t}</li>`).join('')}</ol>`;
  let body = '';
  if (s === 1) {
    const LOCAL_TXT = {
      text: ['🖥', 'This PC', 'Runs on your GPU. Private, no per-use cost.'],
      stt: ['🖥', 'This PC (Whisper)', 'Private — the audio never leaves this PC.'],
    };
    const CLOUD_TXT = {
      text: 'One of your cloud provider’s models. Only you see it.',
      image_gen: 'e.g. gpt-image-1 (OpenAI) or Imagen (Google). May cost money.',
      video_gen: 'e.g. Sora (OpenAI) or Veo (Google). Costs money per clip.',
      stt: 'e.g. whisper-1 or gpt-4o-mini-transcribe. The audio leaves this PC.',
    };
    const purposes = PURPOSES.map(p => `<button type="button" role="radio" aria-checked="${D.purpose === p.v}" class="ln-choice${D.purpose === p.v ? ' on' : ''}" data-wz="purpose" data-v="${p.v}">
        <span class="ln-choice-i">${p.icon}</span><b>${esc(p.t)}</b><small>${esc(p.h)}</small></button>`).join('');
    const lt = LOCAL_TXT[D.purpose];
    const SD_TXT = ['🖥', 'This PC (stable-diffusion.cpp)', 'Runs on an Arc GPU. Private, no per-use cost. You load it by hand.'];
    const backs = _wzBackends(D.purpose).map(o => {
      const [ic, t, h] = o.v === 'sdcpp' ? SD_TXT : o.v === 'local' ? lt : ['☁', 'Cloud', CLOUD_TXT[D.purpose]];
      return `<button type="button" role="radio" aria-checked="${D.backend === o.v}" class="ln-choice${D.backend === o.v ? ' on' : ''}" data-wz="backend" data-v="${o.v}" ${o.ok ? '' : 'disabled'}>
        <span class="ln-choice-i">${ic}</span><b>${esc(t)}</b><small>${esc(o.ok ? h : o.why)}</small></button>`;
    }).join('');
    body = `<fieldset class="ln-fs"><legend>What should it do?</legend>
        <div class="ln-choices ln-choices-4" role="radiogroup" aria-label="What should it do?">${purposes}</div></fieldset>
      <fieldset class="ln-fs"><legend>Where does it run?</legend>
        <div class="ln-choices" role="radiogroup" aria-label="Where does it run?">${backs}</div></fieldset>`;
  } else if (s === 2) {
    if (D.purpose === 'text') body = D.backend === 'local' ? wizardLocalHtml() : wizardCloudHtml();
    else if (D.backend === 'cloud') body = wizardCloudMediaHtml();
    else if (D.backend === 'sdcpp') body = wizardSdHtml();
    else body = wizardWhisperHtml();
  } else {
    const kind = D.kind;
    const fake = { kind, local: D.backend !== 'cloud', cloud_key: D.backend === 'cloud' ? D.cloud : null, name: D.name };
    const jobs = (LN.d.jobs || []).filter(j => {
      if (!(KIND_OK[j.kind] || []).includes(kind)) return false;
      if (j.local_only && !fake.local) return false;
      if (j.job === 'agent.reason' && fake.local) return false;
      if (j.local_first && !fake.local && !(LN.d.media || {}).allow_cloud_audio) return false;
      return true;
    });
    const media = isMedia(kind);
    const fbCur = D.fallback == null ? (media ? '' : 'main') : D.fallback;
    const fbOpts = (media ? `<option value=""${fbCur ? '' : ' selected'}>No backup</option>` : '')
      + (LN.d.lanes || []).filter(l => l.name !== D.name && (media ? l.kind === kind : (KIND_OK[kind] || [kind]).includes(l.kind)))
      .map(l => `<option value="${esc(l.name)}"${fbCur === l.name ? ' selected' : ''}>${where(l) === 'cloud' ? '☁ ' : '🖥 '}${esc(l.label)}</option>`).join('');
    body = `<fieldset class="ln-fs"><legend>Which jobs should it do? <span class="dim">(you can change this later)</span></legend>
      ${jobs.length ? jobs.map(j => `<label class="ln-check ln-jobpick"><input type="checkbox" data-wz="job" value="${esc(j.job)}"${D.jobs.includes(j.job) ? ' checked' : ''}>
        <span><b>${esc(j.label)}</b><small class="dim">${esc(j.hint)}</small></span></label>`).join('') : '<div class="dim">No jobs need this kind of model right now.</div>'}
      </fieldset>
      <label class="ln-lbl" for="wz-fb">If it fails, use</label>
      <select id="wz-fb" data-wz="fallback">${fbOpts}</select>`;
  }
  const err = D._err ? `<div class="ln-test bad" role="alert">✕ ${esc(D._err)}</div>` : '';
  const ok = D._ok ? `<div class="ln-test ok" role="status">✅ ${esc(D._ok)}${D._img ? `<img class="ln-test-img" src="${esc(D._img)}" alt="Image made by the test">` : ''}</div>` : '';
  const last = s === 3 || (WZ.editing && s === 2);
  openModal(`<h3>${WZ.editing ? 'Edit “' + esc(D.label || D.name) + '”' : 'Add a model'}</h3>${WZ.editing ? '' : bar}
    <div class="ln-wz-body">${body}</div>${err}${ok}
    <div class="ln-row-btns ln-modal-btns">
      ${s > 1 && !WZ.editing ? '<button type="button" class="btn ghost ln-small" data-wz="back">Back</button>' : ''}
      <span style="flex:1"></span>
      <button type="button" class="btn ghost ln-small" data-wz="cancel">${D._ok ? 'Close' : 'Cancel'}</button>
      ${D._ok ? '' : `<button type="button" class="btn accent ln-small" data-wz="${last ? 'finish' : 'next'}" ${D._busy ? 'disabled' : ''}>${D._busy ? 'Saving & testing…' : (last ? 'Save & test' : 'Next')}</button>`}
    </div>`, WZ.editing ? 'Edit model' : 'Add a model');
  const modal = document.getElementById('ln-modal');
  modal.addEventListener('click', wizardClick);
  modal.addEventListener('change', wizardChange);
  modal.addEventListener('input', wizardChange);
}

function fitText(sizeGb, gpu) {
  const dev = (LN.vram || []).find(v => v.index === Number(gpu));
  if (!dev || sizeGb == null) return '';
  const need = sizeGb + 1.0;   // weights + a rough KV/compute allowance
  if (need <= dev.free_gb) return `<span class="ln-fit ok">✓ fits (${dev.free_gb} GB free)</span>`;
  if (need <= dev.total_gb) return `<span class="ln-fit warn">▲ tight — ${dev.free_gb} GB free now; it may need the main model unloaded</span>`;
  return `<span class="ln-fit bad">✕ too big for this GPU (${dev.total_gb} GB)</span>`;
}

function wizardLocalHtml() {
  const D = WZ.data;
  const builtinKind = WZ.editing && laneByName(WZ.editing) && laneByName(WZ.editing).builtin;
  const kinds = [['chat', 'Text', 'Answers and tool calls'], ['vision', 'Image reader', 'Describes pictures (needs an mmproj file)'], ['embed', 'Search memory', 'Embeddings for memory search']];
  const models = helperFiles('models');
  const gpus = LN.d.gpus || [];
  if (D.gpu == null && gpus.length) D.gpu = gpus[gpus.length - 1];
  const modelOpts = models.map(m => {
    const rel = m.path;
    const sel = D.model && (rel.replace(/\\/g, '/').endsWith(String(D.model).replace(/\\/g, '/')));
    return `<option value="${esc(rel)}"${sel ? ' selected' : ''}>${esc(m.display || m.name)} — ${esc(m.name)}${m.size_gb != null ? ' · ' + m.size_gb + ' GB' : ''}</option>`;
  }).join('');
  const chosen = models.find(m => D.model && m.path.replace(/\\/g, '/').endsWith(String(D.model).replace(/\\/g, '/')));
  const gpuOpts = gpus.map(g => {
    const dev = (LN.vram || []).find(v => v.index === g);
    return `<option value="${g}"${Number(D.gpu) === g ? ' selected' : ''}>GPU ${g}${dev ? ' — ' + esc(dev.name) + ' · ' + dev.free_gb + ' GB free' : ''}</option>`;
  }).join('');
  return `
    ${builtinKind ? '' : `<fieldset class="ln-fs"><legend>What kind of model?</legend><div class="ln-kinds">
      ${kinds.map(([k, t, h]) => `<label class="ln-radio"><input type="radio" name="wz-kind" value="${k}" data-wz="kind"${D.kind === k ? ' checked' : ''}${WZ.editing ? ' disabled' : ''}> <span><b>${t}</b><small>${h}</small></span></label>`).join('')}
    </div></fieldset>`}
    <label class="ln-lbl" for="wz-model">Model file</label>
    <select id="wz-model" data-wz="model"><option value="">Choose a .gguf file…</option>${modelOpts}</select>
    ${models.length ? '' : `<div class="dim ln-hint">No model files in <code>${esc(helperFolder())}</code> — put the .gguf there, then reopen this.</div>`}
    ${D.kind === 'vision' ? `<label class="ln-lbl" for="wz-mmproj">Image projector (mmproj)</label>
      <select id="wz-mmproj" data-wz="mmproj"><option value="">${chosen && chosen.mmproj_path ? 'Use the one next to the model' : 'Choose…'}</option>
      ${helperFiles('mmproj').map(m => `<option value="${esc(m.path)}"${D.mmproj && m.path === String(D.mmproj).replace(/\\/g, '/') ? ' selected' : ''}>${esc(m.name)}</option>`).join('')}</select>` : ''}
    <label class="ln-lbl" for="wz-gpu">GPU</label>
    <select id="wz-gpu" data-wz="gpu">${gpuOpts}</select>
    <div class="ln-hint">${chosen ? fitText(chosen.size_gb, D.gpu) : ''}</div>
    <label class="ln-lbl" for="wz-label">Name it</label>
    <input id="wz-label" data-wz="label" maxlength="40" value="${esc(D.label || '')}" placeholder="e.g. Coder, Reviewer, Big reader" autocomplete="off">
    <details class="ln-adv"><summary>More settings</summary>
      <label class="ln-lbl" for="wz-port">Port</label>
      <input id="wz-port" data-wz="port" type="number" min="1025" max="65535" value="${esc(D.port || LN.d.suggested_port)}">
      <label class="ln-lbl" for="wz-ctx">Context size (tokens)</label>
      <input id="wz-ctx" data-wz="ctx" type="number" min="512" max="262144" step="512" value="${esc(D.ctx || 8192)}">
      <label class="ln-lbl" for="wz-idle">Unload after idle (seconds, 0 = never)</label>
      <input id="wz-idle" data-wz="idle_unload_s" type="number" min="0" max="86400" value="${esc(D.idle_unload_s ?? '')}" placeholder="default">
    </details>`;
}

function wizardCloudHtml() {
  const D = WZ.data;
  const cms = textCloudModels();
  if (!cms.length) {
    return `<div class="ln-warn">You don't have any cloud models yet.</div>
      <p class="dim">Add a provider (its web address and API key) in <a href="#sec-cloud" data-wz="goto-cloud">Cloud Models</a>.
      The key is stored in Windows Credential Manager, never in a file. Then come back here.</p>`;
  }
  return `<fieldset class="ln-fs"><legend>What kind of model?</legend><div class="ln-kinds">
      <label class="ln-radio"><input type="radio" name="wz-kind" value="chat" data-wz="kind"${D.kind !== 'vision' ? ' checked' : ''}${WZ.editing ? ' disabled' : ''}> <span><b>Text</b><small>Answers and tool calls</small></span></label>
      <label class="ln-radio"><input type="radio" name="wz-kind" value="vision" data-wz="kind"${D.kind === 'vision' ? ' checked' : ''}${WZ.editing ? ' disabled' : ''}> <span><b>Image reader</b><small>Can also read pictures</small></span></label>
    </div></fieldset>
    <label class="ln-lbl" for="wz-cloud">Cloud model</label>
    <select id="wz-cloud" data-wz="cloud"><option value="">Choose…</option>
      ${cms.map(c => `<option value="${esc(c.key)}"${D.cloud === c.key ? ' selected' : ''}>${esc(c.display)} · ${esc(c.provider)}</option>`).join('')}</select>
    <div class="dim ln-hint">Card numbers are masked before anything is sent to it.</div>
    <label class="ln-lbl" for="wz-label">Name it</label>
    <input id="wz-label" data-wz="label" maxlength="40" value="${esc(D.label || '')}" placeholder="e.g. Cloud reviewer" autocomplete="off">`;
}

function wizardSdHtml() {
  const D = WZ.data;
  const media = LN.d.media || {};
  const files = helperFiles(D.purpose === 'video_gen' ? 'video' : 'image');
  const folder = (media.media_dirs || {})[D.purpose] || '';
  const gpus = LN.d.gpus || [];
  if (D.gpu == null) {
    // the GPU with the most free memory right now (big weights)
    const free = g => ((LN.vram || []).find(v => v.index === g) || {}).free_gb || 0;
    D.gpu = gpus.length ? gpus.reduce((a, g) => (free(g) > free(a) ? g : a), gpus[gpus.length - 1]) : -1;
  }
  if (!D.sd_preset) sdApplyPreset(D, 'z-image');
  // first visit: pick each still-empty file by its name (the user can change them).
  // Also when editing, so a text encoder / VAE added to the folder later gets picked up.
  if (!D._sd_guessed) {
    D._sd_guessed = true;
    for (const f of files) {
      const role = sdGuessRole(f.name);
      if (!D[role]) D[role] = f.path;
    }
  }
  const pick = (key, label, optional) => `<label class="ln-lbl" for="wz-sd-${key}">${label}${optional ? ' <span class="dim">(optional)</span>' : ''}</label>
    <select id="wz-sd-${key}" data-wz="${key}"><option value="">${optional ? 'None / built into the model' : 'Choose…'}</option>
      ${files.map(f => `<option value="${esc(f.path)}"${D[key] === f.path ? ' selected' : ''}>${esc(f.name)}${f.size_gb != null ? ' · ' + f.size_gb + ' GB' : ''}</option>`).join('')}</select>`;
  const total = files.filter(f => [D.diffusion_model, D.llm, D.vae, D.llm_vision].includes(f.path)).reduce((a, f) => a + (f.size_gb || 0), 0);
  const gpuOpts = gpus.map(g => {
    const dev = (LN.vram || []).find(v => v.index === g);
    return `<option value="${g}"${Number(D.gpu) === g ? ' selected' : ''}>GPU ${g}${dev ? ' — ' + esc(dev.name) + ' · ' + dev.free_gb + ' GB free' : ''}</option>`;
  }).join('') + `<option value="-1"${Number(D.gpu) === -1 ? ' selected' : ''}>CPU only (very slow)</option>`;
  const missing = media.sd_found ? '' : `<div class="ln-warn">⚠ sd-server isn’t installed yet — you can save this now; it can be loaded once the program is in place.
      <details><summary>Where to put it</summary><ul>${(media.sd_dirs || []).map(x => `<li><code>${esc(x)}</code></li>`).join('')}</ul></details></div>`;
  const presets = SD_PRESETS.map(pr => `<label class="ln-radio"><input type="radio" name="wz-sdp" value="${pr.v}" data-wz="sd_preset"${D.sd_preset === pr.v ? ' checked' : ''}>
      <span><b>${esc(pr.t)}</b><small>${esc(pr.h)}</small></span></label>`).join('');
  return `${missing}
    <fieldset class="ln-fs"><legend>Which model family?</legend><div class="ln-kinds">${presets}</div></fieldset>
    ${files.length ? '' : `<div class="ln-warn">No model files in <code>${esc(folder)}</code> yet — put the .gguf / .safetensors files there, then reopen this.</div>`}
    ${pick('diffusion_model', 'Diffusion model')}
    ${pick('llm', 'Text encoder', true)}
    ${pick('vae', 'VAE', true)}
    ${D.purpose === 'image_gen' ? `${pick('llm_vision', 'Vision weights — only to edit / combine pictures', true)}
    <div class="dim ln-hint">For Qwen Image 2.1: <code>mmproj-Qwen3VL-8B-Instruct-F16.gguf</code> (from the Qwen3-VL-8B-Instruct GGUF files).
      Without it the model can still change one picture (“How much to change”), but not follow edit instructions or combine pictures.
      Pictures are only ever sent to this PC.</div>` : ''}
    ${D.purpose === 'image_gen' ? `<label class="ln-lbl" for="wz-size">Default image size</label>
    <select id="wz-size" data-wz="default_size">${SD_SIZES.map(([v, t]) =>
      `<option value="${v}"${(D.default_size || 'large') === v ? ' selected' : ''}>${t}</option>`).join('')}</select>
    <div class="dim ln-hint">Used when the chat is left on “Model default”. Bigger is slower: Large takes about 4× as long as Small.
      Other shapes (wide / tall) keep the same number of pixels.</div>` : ''}
    <label class="ln-lbl" for="wz-gpu">Run it on</label>
    <select id="wz-gpu" data-wz="gpu">${gpuOpts}</select>
    <label class="ln-check"><input type="checkbox" data-wz="offload_to_cpu"${D.offload_to_cpu ? ' checked' : ''}>
      Keep weights in RAM <span class="dim">(slower, needs much less GPU memory)</span></label>
    <div class="ln-hint">${total && !D.offload_to_cpu && Number(D.gpu) >= 0 ? fitText(total + 0.5, D.gpu) : ''}</div>
    <div class="dim ln-hint">It isn’t started by a request: press <b>▶ Load</b> on its card when you want to use it, and it stays loaded until you unload it.</div>
    <label class="ln-lbl" for="wz-label">Name it</label>
    <input id="wz-label" data-wz="label" maxlength="40" value="${esc(D.label || '')}" placeholder="e.g. Z-Image on this PC" autocomplete="off">
    <details class="ln-adv"${D.sd_preset === 'custom' ? ' open' : ''}><summary>More settings</summary>
      <label class="ln-lbl" for="wz-steps">Steps</label>
      <input id="wz-steps" data-wz="steps" type="number" min="1" max="150" value="${esc(D.steps ?? '')}" placeholder="20">
      <label class="ln-lbl" for="wz-cfg">Guidance (cfg scale)</label>
      <input id="wz-cfg" data-wz="cfg_scale" type="number" min="0" max="30" step="0.1" value="${esc(D.cfg_scale ?? '')}">
      <label class="ln-lbl" for="wz-sampler">Sampler</label>
      <input id="wz-sampler" data-wz="sampler" maxlength="24" value="${esc(D.sampler || '')}" placeholder="euler" autocomplete="off" spellcheck="false">
      <label class="ln-lbl" for="wz-flow">Flow shift <span class="dim">(empty = model default)</span></label>
      <input id="wz-flow" data-wz="flow_shift" type="number" min="0" max="20" step="0.1" value="${esc(D.flow_shift ?? '')}">
      <label class="ln-lbl" for="wz-port">Port</label>
      <input id="wz-port" data-wz="port" type="number" min="1025" max="65535" value="${esc(D.port || LN.d.suggested_port)}">
      ${D.purpose === 'image_gen' ? `<label class="ln-lbl" for="wz-maxrefs">Most pictures per request (edit / combine)</label>
      <input id="wz-maxrefs" data-wz="max_refs" type="number" min="1" max="10" value="${esc(D.max_refs || 4)}">
      <label class="ln-check"><input type="checkbox" data-wz="edit_refs"${D.edit_refs ? ' checked' : ''}>
        This model edits with reference pictures on its own <span class="dim">(e.g. FLUX Kontext — no vision weights needed)</span></label>` : ''}
    </details>`;
}

function sdApplyPreset(D, v) {
  D.sd_preset = v;
  const pr = SD_PRESETS.find(x => x.v === v);
  if (!pr || v === 'custom') return;
  for (const k of ['steps', 'cfg_scale', 'sampler', 'flow_shift', 'offload_to_cpu']) D[k] = pr[k];
  if (!D.label || SD_PRESETS.some(x => D.label === x.t)) D.label = pr.t;
}

function wizardWhisperHtml() {
  const D = WZ.data;
  const media = LN.d.media || {};
  const models = helperFiles('whisper');
  const gpus = LN.d.gpus || [];
  if (D.gpu == null) D.gpu = gpus.length ? gpus[gpus.length - 1] : -1;
  const hint = n => /large-v3-turbo|large-v3|large-v2/i.test(n) ? 'best for Bangla and English'
    : /medium/i.test(n) ? 'good, slower' : /small|base|tiny/i.test(n) ? 'fast, weak at Bangla' : '';
  const modelOpts = models.map(m => {
    const sel = D.model && m.path === String(D.model).replace(/\\/g, '/');
    const h = hint(m.name);
    return `<option value="${esc(m.path)}"${sel ? ' selected' : ''}>${esc(m.name)} · ${m.size_gb} GB${h ? ' — ' + h : ''}</option>`;
  }).join('');
  const gpuOpts = gpus.map(g => {
    const dev = (LN.vram || []).find(v => v.index === g);
    return `<option value="${g}"${Number(D.gpu) === g ? ' selected' : ''}>GPU ${g}${dev ? ' — ' + esc(dev.name) + ' · ' + dev.free_gb + ' GB free' : ''}</option>`;
  }).join('') + `<option value="-1"${Number(D.gpu) === -1 ? ' selected' : ''}>CPU only (slower, no GPU memory)</option>`;
  const langs = [['auto', 'Detect automatically'], ['en', 'English'], ['bn', 'বাংলা (Bangla)']];
  const voiceDir = (media.media_dirs || {}).stt || 'E:\\AI\\Models\\orchestrator\\voice-models';
  const missing = media.whisper_found ? '' : `<div class="ln-warn">⚠ The whisper.cpp <b>program</b> (<code>whisper-server.exe</code>) isn’t installed yet.
      The model file is fine — it comes from <code>${esc(voiceDir)}</code>. You can save this now; it starts working once the program is in place.
      <details><summary>Where to put the program</summary><ul>${(media.whisper_dirs || []).map(x => `<li><code>${esc(x)}</code></li>`).join('')}</ul>
      <div class="dim">Put <code>whisper-server.exe</code> and the DLLs that come with it in one of these folders, then restart the server.</div></details></div>`;
  return `${missing}
    <label class="ln-lbl" for="wz-wmodel">Whisper model file</label>
    <select id="wz-wmodel" data-wz="model"><option value="">Choose a ggml .bin file…</option>${modelOpts}</select>
    ${models.length ? '' : `<div class="dim ln-hint">No whisper models in <code>${esc(voiceDir)}</code>. Download e.g. <code>ggml-large-v3-turbo-q5_0.bin</code> (about 550 MB) into it, then reopen this.</div>`}
    <label class="ln-lbl" for="wz-gpu">Run it on</label>
    <select id="wz-gpu" data-wz="gpu">${gpuOpts}</select>
    <label class="ln-lbl" for="wz-lang">Usual language</label>
    <select id="wz-lang" data-wz="language">${langs.map(([v, t]) => `<option value="${v}"${(D.language || 'auto') === v ? ' selected' : ''}>${esc(t)}</option>`).join('')}</select>
    <div class="dim ln-hint">People can still pick a language each time they record.</div>
    <label class="ln-lbl" for="wz-label">Name it</label>
    <input id="wz-label" data-wz="label" maxlength="40" value="${esc(D.label || '')}" placeholder="e.g. Whisper" autocomplete="off">
    <details class="ln-adv"><summary>More settings</summary>
      <label class="ln-lbl" for="wz-port">Port</label>
      <input id="wz-port" data-wz="port" type="number" min="1025" max="65535" value="${esc(D.port || LN.d.suggested_port)}">
      <label class="ln-lbl" for="wz-threads">CPU threads</label>
      <input id="wz-threads" data-wz="threads" type="number" min="1" max="64" value="${esc(D.threads || 4)}">
      <label class="ln-lbl" for="wz-idle">Unload after idle (seconds, 0 = never)</label>
      <input id="wz-idle" data-wz="idle_unload_s" type="number" min="0" max="86400" value="${esc(D.idle_unload_s ?? '')}" placeholder="default">
    </details>`;
}

function wizardCloudMediaHtml() {
  const D = WZ.data;
  // likely matches first; anything else is still allowed (the name guess can be wrong)
  const all = LN.d.cloud_models || [];
  const cms = [...all.filter(c => guessMediaKind(c) === D.purpose), ...all.filter(c => guessMediaKind(c) !== D.purpose)];
  const eg = { image_gen: 'gpt-image-1 (OpenAI) or imagen-4.0-generate-001 (Google)',
               video_gen: 'sora-2 (OpenAI) or veo-3.0-generate-001 (Google)',
               stt: 'whisper-1 or gpt-4o-mini-transcribe (OpenAI), whisper-large-v3 (Groq)' }[D.purpose];
  if (!cms.length) {
    return `<div class="ln-warn">You don't have any cloud models yet.</div>
      <p class="dim">In <a href="#sec-cloud" data-wz="goto-cloud">Cloud Models</a>, add the provider and the model name (${esc(eg)}). Then come back here.</p>`;
  }
  return `<label class="ln-lbl" for="wz-cloud">Cloud model</label>
    <select id="wz-cloud" data-wz="cloud"><option value="">Choose…</option>
      ${cms.map(c => `<option value="${esc(c.key)}"${D.cloud === c.key ? ' selected' : ''}>${guessMediaKind(c) === D.purpose ? '★ ' : ''}${esc(c.display)} · ${esc(c.provider)}</option>`).join('')}</select>
    <div class="dim ln-hint">★ = looks like the right kind. Pick one that ${D.purpose === 'stt' ? 'transcribes audio' : 'makes ' + (D.purpose === 'video_gen' ? 'videos' : 'images')}, e.g. ${esc(eg)}.
      Not in the list? Add the model name to the provider in <a href="#sec-cloud" data-wz="goto-cloud">Cloud Models</a>.</div>
    ${D.purpose === 'stt' ? '<div class="ln-warn">⚠ The recording leaves this PC and can’t be checked for card numbers first.</div>'
      : '<div class="dim ln-hint">Descriptions are checked for card numbers before they’re sent. This can cost money per ' + (D.purpose === 'video_gen' ? 'clip' : 'picture') + '.</div>'}
    <label class="ln-lbl" for="wz-label">Name it</label>
    <input id="wz-label" data-wz="label" maxlength="40" value="${esc(D.label || '')}" placeholder="e.g. Cloud ${D.purpose === 'stt' ? 'speech' : D.purpose === 'video_gen' ? 'videos' : 'images'}" autocomplete="off">`;
}

function wizardChange(e) {
  const t = e.target, k = t.dataset && t.dataset.wz;
  if (!k) return;
  const D = WZ.data;
  D._err = null;
  if (k === 'job') {
    D.jobs = [...document.querySelectorAll('[data-wz="job"]:checked')].map(x => x.value);
    return;
  }
  if (k === 'kind') { D.kind = t.value; wizardRender(); return; }
  if (k === 'clear_auth') { D.clear_auth = t.checked; return; }
  if (k === 'offload_to_cpu') { D.offload_to_cpu = t.checked; D.sd_preset = 'custom'; wizardRender(); return; }
  if (k === 'edit_refs') { D.edit_refs = t.checked; return; }
  if (k === 'sd_preset') { sdApplyPreset(D, t.value); wizardRender(); return; }
  if (['steps', 'cfg_scale', 'flow_shift', 'sampler'].includes(k)) D.sd_preset = 'custom';
  if (['port', 'ctx', 'idle_unload_s', 'gpu', 'timeout_s', 'threads', 'steps', 'cfg_scale', 'flow_shift', 'max_refs'].includes(k)) D[k] = t.value === '' ? null : Number(t.value);
  else D[k] = t.value;
  if (k === 'model' && !D.label) {
    const m = helperFiles('models').find(x => x.path === t.value)
      || helperFiles('whisper').find(x => x.path === t.value);
    if (m) D.label = (m.display || m.name).replace(/\.(gguf|bin)$/i, '').replace(/^ggml-/, 'Whisper ').slice(0, 40);
  }
  if (k === 'cloud' && !D.label) {
    const c = (LN.d.cloud_models || []).find(x => x.key === t.value);
    if (c) D.label = c.display.slice(0, 40);
  }
  if (e.type === 'change' && ['model', 'gpu', 'cloud', 'diffusion_model', 'llm', 'vae'].includes(k)) wizardRender();
}

async function wizardClick(e) {
  const b = e.target.closest('[data-wz]');
  if (!b || b.tagName === 'INPUT' || b.tagName === 'SELECT') return;
  const D = WZ.data, k = b.dataset.wz;
  if (k === 'cancel') { closeModal(); if (D._ok) lanesLoad(); return; }
  if (k === 'goto-cloud') { closeModal(); switchSettingsTab('sec-cloud'); return; }
  if (k === 'purpose') {
    D.purpose = b.dataset.v;
    D.kind = D.purpose === 'text' ? (['chat', 'vision', 'embed'].includes(D.kind) ? D.kind : 'chat') : D.purpose;
    D.fallback = D.purpose === 'text' ? 'main' : '';
    D.jobs = [];
    _wzFixBackend(D);
    wizardRender(); return;
  }
  if (k === 'backend') {
    D.backend = b.dataset.v;
    if (D.backend === 'cloud' && D.kind === 'embed') D.kind = 'chat';
    if (D.backend !== 'cloud') await ensureLocalInventory();
    wizardRender(); return;
  }
  if (k === 'back') { WZ.step = Math.max(1, WZ.step - 1); D._err = null; wizardRender(); return; }
  if (k === 'next') {
    if (WZ.step === 2) {
      if (D.backend === 'sdcpp' && !D.diffusion_model) { D._err = 'Pick the diffusion model file.'; wizardRender(); return; }
      if (D.backend === 'local' && !D.model) { D._err = 'Pick a model file.'; wizardRender(); return; }
      if (D.backend === 'sdcpp' && !D.label) D.label = (SD_PRESETS.find(x => x.v === D.sd_preset) || {}).t || 'Images on this PC';
      if (D.backend === 'cloud' && !D.cloud) { D._err = 'Pick one of your cloud models.'; wizardRender(); return; }
      const base = { image_gen: 'image-maker', video_gen: 'video-maker', stt: 'speech' }[D.purpose];
      if (!D.name) D.name = slug(D.label || base || (D.backend === 'cloud' ? 'cloud-model' : 'local-model'));
      if (laneByName(D.name) && !WZ.editing) D.name = slug(D.name + '-' + ((Date.now() / 1000 | 0) % 1000));
      // an image model usually exists to read images; other jobs are opt-in
      if (!D.jobs.length && D.kind === 'vision' && !WZ.editing) D.jobs = ['vision'];
      // an image/video/speech model exists for its one job
      const mj = MEDIA_JOBS.find(x => x.kind === D.kind);
      if (mj && !D.jobs.length && !WZ.editing && !(LN.d.role_map || {})[mj.job]) D.jobs = [mj.job];
    }
    WZ.step += 1; D._err = null; wizardRender(); return;
  }
  if (k === 'finish') {
    D._busy = true; D._err = null; wizardRender();
    const media = isMedia(D.kind);
    const body = { name: D.name || slug(D.label), backend: D.backend === 'sdcpp' ? 'local' : D.backend, kind: D.kind,
                   label: D.label || undefined, fallback: D.fallback || (media ? null : 'main') };
    if (D.kind === 'vision' && !D.mmproj) {
      const m = helperFiles('models').find(x => D.model && x.path === String(D.model).replace(/\\/g, '/'));
      if (m && m.mmproj_path) D.mmproj = m.mmproj_path;
    }
    if (D.backend === 'sdcpp') {
      Object.assign(body, { engine: 'sdcpp', diffusion_model: D.diffusion_model, llm: D.llm || '', vae: D.vae || '',
                            gpu: D.gpu, port: D.port || undefined, offload_to_cpu: !!D.offload_to_cpu,
                            steps: D.steps ?? null, cfg_scale: D.cfg_scale ?? null, flow_shift: D.flow_shift ?? null,
                            sampler: D.sampler || '' });
      if (D.kind === 'image_gen') Object.assign(body, { llm_vision: D.llm_vision || '', edit_refs: !!D.edit_refs,
                                                        max_refs: D.max_refs || undefined,
                                                        default_size: D.default_size || 'large' });
    } else if (D.backend === 'local' && D.kind === 'stt') {
      Object.assign(body, { model: D.model, gpu: D.gpu, port: D.port || undefined, threads: D.threads || undefined,
                            language: D.language || 'auto', idle_unload_s: D.idle_unload_s ?? undefined });
    } else if (D.backend === 'local') Object.assign(body, { model: D.model, mmproj: D.mmproj || undefined, gpu: D.gpu,
                                                    port: D.port || undefined, ctx: D.ctx || undefined,
                                                    idle_unload_s: D.idle_unload_s ?? undefined });
    else body.cloud = D.cloud;
    if (D.jobs.length) body.jobs = D.jobs;
    try {
      await lanesApi('/control/lanes', 'POST', body);
      // a failed test is a normal answer here ({ok:false, error}), not an exception
      const tr = await fetch(`/control/lanes/${encodeURIComponent(body.name)}/test`, { method: 'POST' });
      const r = await tr.json().catch(() => ({ ok: false, error: 'HTTP ' + tr.status }));
      D._busy = false;
      if (r.needs_load) { D._ok = 'Saved. Press ▶ Load on its card to start it — it stays loaded until you unload it.'; D._img = null; }
      else if (r.ok && media) { D._ok = `Saved — ${r.sample || 'it works'}`; D._img = r.image || null; }
      else if (r.ok) D._ok = `Saved — it works (${r.ms} ms)${r.sample ? ': “' + r.sample + '”' : ''}`;
      else D._err = 'Saved, but the test failed: ' + (r.error || 'no answer');
      if (typeof mediaLoadStatus === 'function') mediaLoadStatus();
      WZ.editing = WZ.editing || body.name;
      wizardRender();
      lanesLoad();
    } catch (err) {
      D._busy = false; D._err = err.message; wizardRender();
    }
  }
}

/* ---------- page events ---------- */
document.addEventListener('click', async (e) => {
  const b = e.target.closest && e.target.closest('#lanes-content [data-ln]');
  if (!b || b.tagName === 'SELECT' || (b.tagName === 'INPUT')) return;
  const k = b.dataset.ln;
  try {
    if (k === 'intro-ok') { localStorage.setItem('lanes_intro_seen', '1'); lanesRender(); }
    else if (k === 'view') { LN.view = b.dataset.v; try { localStorage.setItem('lanes_view', LN.view); } catch (_) {} lanesRender(); }
    else if (k === 'preset') { LN.presetPreview = b.dataset.mode; lanesRender(); }
    else if (k === 'preset-cancel') { LN.presetPreview = null; lanesRender(); }
    else if (k === 'preset-apply') {
      const prev = curPreset();
      localStorage.setItem('agent_engine', b.dataset.mode);
      LN.presetPreview = null; lanesRender();
      const p = PRESETS.find(x => x.mode === b.dataset.mode);
      undoToast(`Now using “${p.name}” for the agent`, async () => { localStorage.setItem('agent_engine', prev); });
    }
    else if (k === 'goto-cloud') { e.preventDefault(); switchSettingsTab('sec-cloud'); }
    else if (k === 'test') await testLane(b.dataset.name);
    else if (k === 'stop') {
      try { await lanesApi(`/control/lanes/${encodeURIComponent(b.dataset.name)}/stop`, 'POST'); toast('Unloaded'); }
      catch (err) { toast(err.message, true); }
      lanesRender();
    }
    else if (k === 'load') await lnLoadModel(b.dataset.name);
    else if (k === 'edit') await openWizard(b.dataset.name);
    else if (k === 'add') await openWizard(null);
    else if (k === 'add-media') await openWizard(null, b.dataset.kind);
    else if (k === 'test-full') await testLane(b.dataset.name, true);
    else if (k === 'del') deleteDialog(b.dataset.name);
    else if (k === 'job-reset') { LN.draft[b.dataset.job] = LN.d.defaults[b.dataset.job]; lanesRender(); }
    else if (k === 'jobs-discard') { LN.draft = {}; lanesRender(); }
    else if (k === 'jobs-save') {
      const map = {};
      Object.entries(LN.draft).forEach(([j, l]) => { map[j] = l === LN.d.defaults[j] ? null : l; });
      await saveJobs(map, LN.asDefault);
    }
  } catch (err) { toast(err.message, true); }
});

document.addEventListener('change', async (e) => {
  const t = e.target;
  if (!t.closest || !t.closest('#lanes-content') || !t.dataset.ln) return;
  const k = t.dataset.ln;
  try {
    if (k === 'main-sel') await setMain(t.value);
    else if (k === 'exec-sel') await setHelper(t.value);
    else if (k === 'vmode') await saveVerify({ mode: t.value });
    else if (k === 'verify-sel') await saveJobs({ verify: t.value === LN.d.defaults.verify ? null : t.value }, false);
    else if (k === 'job') {
      const v = t.value || null;
      if (v === (LN.d.role_map[t.dataset.job] || null)) delete LN.draft[t.dataset.job];
      else LN.draft[t.dataset.job] = v;
      lanesRender();
      const again = document.getElementById('ln-job-' + t.dataset.job);
      if (again) again.focus();
    }
    else if (k === 'as-default') LN.asDefault = t.checked;
    else if (k === 'media-job') {
      const job = t.dataset.job, prev = LN.d.role_map[job] || null;
      await lanesApi('/control/lanes/roles', 'POST', { map: { [job]: t.value || null } });
      lanesRender();
      undoToast(t.value ? `${(jobSpec(job) || {}).label} now uses “${laneLabel(t.value)}”` : `${(jobSpec(job) || {}).label} turned off`,
                () => lanesApi('/control/lanes/roles', 'POST', { map: { [job]: prev } }));
    }
    else if (k === 'allow-cloud-audio') {
      if (t.checked && !confirm('Allow sending recordings to cloud speech-to-text?\n\nThey can’t be checked for card numbers or personal details before they leave this PC.')) {
        t.checked = false; return;
      }
      await lanesApi('/control/media-settings', 'POST', { allow_cloud_audio: t.checked });
      lanesRender();
      toast(t.checked ? 'Cloud speech to text is allowed' : 'Speech to text stays on this PC');
    }
    else if (k === 'lim-image' || k === 'lim-video') {
      const v = Math.max(0, parseInt(t.value || '0', 10) || 0);
      await lanesApi('/control/media-settings', 'POST', { [k === 'lim-image' ? 'image_per_day' : 'video_per_day']: v });
      toast('Daily limit saved');
    }
    else if (k === 'fb-local') {
      const prev = !t.checked;
      await lanesApi('/control/cloud/lanes', 'POST', { fallback_local: t.checked });
      await lanesLoad();
      undoToast(t.checked ? 'Cloud failures will retry on this PC' : 'Cloud failures will show an error',
                () => lanesApi('/control/cloud/lanes', 'POST', { fallback_local: prev }).then(lanesLoad));
    }
  } catch (err) { toast(err.message, true); lanesLoad(); }
});

/* load when the tab is opened (and once at start if it's the active one) */
document.addEventListener('click', (e) => {
  const b = e.target.closest && e.target.closest('.settings-tab-btn[data-tab="sec-lanes"]');
  if (b) lanesLoad();
});
if (window.__sessionReady) {
  window.__sessionReady.then(() => {
    const p = document.getElementById('sec-lanes');
    if (p) lanesLoad();
  });
}

/* dynamic model & profile list */
const CFG_DEFAULTS = {
  ctx: 32768, ngl: 999, threads: 0, tb: 0, batch: 2048, ubatch: 512,
  np: 1, kai: 25, ts: '9,11', sm: 'layer', fa: 'auto', ct: 'f16',
};

async function loadProfiles() {
  const sel = $('profile');
  if (!sel) return;

  // Nav bar (ui.html, no Load button there anymore): a trimmed, read-only-ish
  // list -- just what's already loaded/available, no local file browsing or
  // launch controls. The full picker below is settings.html's Model Config card.
  if (!$('btn-load-header')) {
    try {
      const d = await (await fetch('/control/available_models')).json();
      sel.innerHTML = '';
      (d.models || []).forEach(m => {
        const o = document.createElement('option');
        o.value = m.id;
        const isCloud = String(m.id).startsWith('cloud:') || m.kind === 'cloud';
        o.textContent = m.display + (isCloud ? '' : (m.currently_loaded ? ' (loaded)' : ' (not loaded)'));
        o.dataset.kind = isCloud ? 'cloud' : 'local';
        o.dataset.provider = m.provider_name || (isCloud ? 'Cloud' : 'Local');
        o.dataset.providerKey = m.provider || (isCloud ? 'cloud' : 'local');
        o.dataset.loaded = m.currently_loaded ? '1' : '0';
        sel.appendChild(o);
      });
      if (!sel.options.length) {
        const o = document.createElement('option');
        o.value = ''; o.textContent = 'No model available'; sel.appendChild(o);
      }
      let restored = null;
      try { restored = localStorage.getItem('app_model'); } catch (e) {}
      if (restored && [...sel.options].some(o => o.value === restored)) sel.value = restored;
    } catch (e) {}
    renderModelPicker();
    return;
  }

  try {
    const d = await (await fetch('/control/profiles')).json();
    sel.innerHTML = '';

    // Local GGUFs only -- this card edits llama-server launch parameters,
    // which are meaningless for cloud models (managed separately in the
    // ☁️ Cloud Models card below).
    const locals = (d.models || []).slice().sort((a, b) =>
      String(a.display || a.name || '').localeCompare(String(b.display || b.name || '')));
    // folders holding several quants show "<folder> · <file>" so they stay distinct
    const perFolder = {};
    locals.forEach(m => { if (m.folder) perFolder[m.folder] = (perFolder[m.folder] || 0) + 1; });
    locals.forEach(m => {
      const o = document.createElement('option');
      o.value = m.path;
      const base = (m.folder || m.name || '').trim();
      const name = m.folder && perFolder[m.folder] > 1 ? `${base} · ${m.name}` : base;
      const shortName = name.length > 28 ? (name.slice(0, 28) + '...') : name;
      const size = m.size_gb != null ? ` · ${m.size_gb}GB` : '';
      const tags = [];
      if (m.mmproj_available) tags.push('👁 Vision');
      if (m.tools_available) tags.push('🔨 Tools');
      if (m.mtp_available) tags.push('⚡ MTP');
      if (m.reasoning_available) tags.push('◵ Reasoning');
      const tagStr = tags.length ? (' · ' + tags.join(' ')) : '';
      o.textContent = `${shortName}${size}${tagStr}`;
      const notes = [m.mtp_note, m.mmproj_note].filter(Boolean);
      o.title = `${m.folder ? m.folder + '/' : ''}${m.name}${size}${tagStr}` +
        (notes.length ? `\n⚠ Not attached: ${notes.join('; ')}` : '');
      o.dataset.mtp = m.mtp_available ? '1' : '';
      o.dataset.mtpPath = m.mtp_draft_path || '';
      o.dataset.vision = m.mmproj_available ? '1' : '';
      o.dataset.mmproj = m.mmproj_path || '';
      o.dataset.tools = m.tools_available ? '1' : '';
      o.dataset.reasoning = m.reasoning_available ? '1' : '';
      o.dataset.kind = 'local';
      o.dataset.provider = 'Local';
      sel.appendChild(o);
    });

    if (!sel.options.length) {
      const o = document.createElement('option');
      o.value = '';
      o.textContent = 'No local models found';
      sel.appendChild(o);
    }

    let restored = null;
    try { restored = localStorage.getItem('app_model'); } catch (e) {}
    const has = v => !!v && [...sel.options].some(o => o.value === v);
    if (has(restored)) sel.value = restored;
    else if (sel.options.length) sel.value = sel.options[0].value;

    loadConfig();   // dropdown is ready — fetch its saved config
    updateModelCapabilitiesBar();
    renderModelPicker();
  } catch (e) {}
}

function updateModelCapabilitiesBar(caps) {
  const row = $('model-caps-row');
  const container = $('caps-pills');
  if (!row || !container) return;

  const sel = $('profile');
  const opt = (sel && sel.selectedIndex >= 0) ? sel.options[sel.selectedIndex] : null;

  let hasVision = false;
  let hasTools = false;
  let hasMtp = false;
  let hasReasoning = false;

  if (caps) {
    hasVision = !!(caps.vision_capable || caps.mmproj_available);
    hasTools = !!caps.tools_available;
    hasMtp = !!(caps.mtp_available || caps.mtp_draft_path);
    hasReasoning = !!caps.reasoning_available;
  } else if (opt) {
    hasVision = opt.dataset.vision === '1';
    hasTools = opt.dataset.tools === '1';
    hasMtp = opt.dataset.mtp === '1';
    hasReasoning = opt.dataset.reasoning === '1';
  }

  const visionSvg = `<svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="9"/><circle cx="12" cy="12" r="3.5" fill="currentColor"/></svg>`;
  const toolsSvg = `<svg width="13" height="13" viewBox="0 0 24 24" fill="currentColor"><path d="M19.7 4.3a2.5 2.5 0 0 0-3.5 0l-2.1 2.1 3.5 3.5 2.1-2.1a2.5 2.5 0 0 0 0-3.5zM13 7.4 4.7 15.7a1 1 0 0 0 0 1.4l2.2 2.2a1 1 0 0 0 1.4 0L16.6 11 13 7.4z"/></svg>`;
  const mtpSvg = `<svg width="13" height="13" viewBox="0 0 24 24" fill="currentColor"><path d="M13 2 4.5 13.5h6L9.5 22l9-12h-6.5L13 2z"/></svg>`;
  const reasoningSvg = `<svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="9"/><line x1="12" y1="3" x2="12" y2="21"/></svg>`;

  let html = '';
  if (hasVision) {
    html += `<span class="cap-pill vision" title="Vision Capable (Multimodal Projector)">${visionSvg} Vision</span>`;
  }
  if (hasTools) {
    html += `<span class="cap-pill tools" title="Tool Calling / Function Calling Supported">${toolsSvg} Tool Use</span>`;
  }
  if (hasMtp) {
    html += `<span class="cap-pill mtp" title="MTP Speculative Decoding Draft Detected">${mtpSvg} MTP</span>`;
  }
  if (hasReasoning) {
    html += `<span class="cap-pill reasoning" title="Reasoning / Chain-of-Thought Model">${reasoningSvg} Reasoning</span>`;
  }

  if (!html) {
    html = `<span style="font-size:11px; color:var(--dim); font-style:italic;">None detected</span>`;
  }

  container.innerHTML = html;
  row.style.display = 'flex';
}

$('profile').onchange = e => {
  e.target._user = true;
  try { localStorage.setItem('app_model', e.target.value); } catch (err) {}
  updateModelCapabilitiesBar();
  // Picking a cloud model means "this is my main lane": nudge the agent engine to
  // a mode that keeps the local model out of the way (the executor stays local
  // unless it is bound to the cloud in the Cloud Models card).
  if (typeof isCloudValue === 'function' && isCloudValue(e.target.value)) {
    const eng = $('agent-engine');
    if (eng && (eng.value === 'all-local' || eng.value === 'main-local-rest-cloud')) {
      eng.value = 'main-cloud-rest-local';
      try { localStorage.setItem('agent_engine', eng.value); } catch (err) {}
      toast('\u2601 Cloud main lane — engine set to "Main Cloud · Rest Local"');
    }
    // Cloud models cost nothing to "load" (no VRAM involved) - bind the main
    // lane immediately instead of making the user press the Load button too.
    if (typeof loadSelectedModel === 'function') loadSelectedModel();
  } else if (typeof curStatus !== 'undefined' && curStatus && curStatus.cloud_main) {
    // Switching back to a local model: release the cloud main-lane binding
    // right away so status/chat immediately show local (🖥) instead of the
    // stale cloud (☁) badge until the user also presses ▶ Load.
    fetch('/control/cloud/lanes', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ clear: ['main'] }),
    }).then(() => {
      if (typeof pollStatus === 'function') pollStatus();
      if (typeof loadCloudCard === 'function') loadCloudCard();
    }).catch(() => {});
  }
  loadConfig();   // show the newly selected model's saved config
  renderModelPicker();
};

/* ---------------- custom model picker (ui.html header only) ----------------
 * The native <select id="profile"> stays in the DOM (hidden) as the single
 * source of truth -- everything that reads/writes sel.value/.options keeps
 * working unchanged. This just renders a themed, iconed, truncating replica
 * on top of it and forwards clicks back onto the real select + 'change'.
 */
function _mpShorten(s, n = 26) {
  s = String(s || '');
  return s.length > n ? s.slice(0, n - 1).trimEnd() + '…' : s;
}

function _mpCleanLabel(text) {
  return String(text || '').replace(/\s*\((?:loaded|not loaded)\)\s*$/, '');
}

function _createPickerItem(o, sel, dd) {
  const isCloud = o.dataset.kind === 'cloud';
  const isLoaded = o.dataset.loaded === '1' || /\(loaded\)/.test(o.textContent);
  let raw = _mpCleanLabel(o.textContent);
  raw = raw.replace(/\s*·\s*(?:[👁🔨⚡◵].*)$/, '').trim();
  const item = document.createElement('button');
  item.type = 'button';
  item.className = 'model-picker-item' + (o.value === sel.value ? ' active' : '');
  item.title = raw + (o.dataset.provider ? ` (${o.dataset.provider})` : '');

  const visionSvg = `<svg width="11" height="11" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="9"/><circle cx="12" cy="12" r="3.5" fill="currentColor"/></svg>`;
  const toolsSvg = `<svg width="11" height="11" viewBox="0 0 24 24" fill="currentColor"><path d="M19.7 4.3a2.5 2.5 0 0 0-3.5 0l-2.1 2.1 3.5 3.5 2.1-2.1a2.5 2.5 0 0 0 0-3.5zM13 7.4 4.7 15.7a1 1 0 0 0 0 1.4l2.2 2.2a1 1 0 0 0 1.4 0L16.6 11 13 7.4z"/></svg>`;
  const mtpSvg = `<svg width="11" height="11" viewBox="0 0 24 24" fill="currentColor"><path d="M13 2 4.5 13.5h6L9.5 22l9-12h-6.5L13 2z"/></svg>`;
  const reasoningSvg = `<svg width="11" height="11" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="9"/><line x1="12" y1="3" x2="12" y2="21"/></svg>`;

  let caps = '';
  if (o.dataset.vision === '1') caps += `<span class="cap-pill vision" title="Vision">${visionSvg} Vision</span>`;
  if (o.dataset.tools === '1') caps += `<span class="cap-pill tools" title="Tool Use">${toolsSvg} Tool Use</span>`;
  if (o.dataset.mtp === '1') caps += `<span class="cap-pill mtp" title="MTP">${mtpSvg} MTP</span>`;
  if (o.dataset.reasoning === '1') caps += `<span class="cap-pill reasoning" title="Reasoning">${reasoningSvg} Reasoning</span>`;
  const capsWrap = caps ? `<div class="mpi-caps">${caps}</div>` : '';

  item.innerHTML = `<span class="mpi-icon">${isCloud ? '☁️' : '🖥'}</span>` +
    `<span class="mpi-label">${_mpShorten(raw, 32)}</span>` +
    capsWrap +
    (isLoaded ? '<span class="mpi-badge">loaded</span>' : '');
  item.onclick = () => {
    if (sel.value !== o.value) {
      sel.value = o.value;
      sel._user = true;
      sel.dispatchEvent(new Event('change'));
    }
    dd.classList.remove('open');
  };
  return item;
}

function renderModelPicker() {
  const sel = $('profile');
  const btn = $('model-picker-btn');
  const dd = $('model-picker-dropdown');
  if (!sel || !btn || !dd) return;

  initModelPickerToggle();

  const icon = $('model-picker-icon');
  const label = $('model-picker-label');

  dd.innerHTML = '';

  const opts = [...sel.options].filter(o => o.value !== '');
  const localOpts = opts.filter(o => o.dataset.kind !== 'cloud');
  const cloudOpts = opts.filter(o => o.dataset.kind === 'cloud');

  // 1. LOCAL SECTION
  if (localOpts.length > 0) {
    const localGroup = document.createElement('div');
    localGroup.className = 'model-picker-group';

    const localTitle = document.createElement('div');
    localTitle.className = 'model-picker-group-title';
    localTitle.innerHTML = `<span class="mpg-name">🖥 Local</span>` +
      `<span class="mpg-count">${localOpts.length}</span>`;
    localGroup.appendChild(localTitle);

    localOpts.forEach(o => {
      localGroup.appendChild(_createPickerItem(o, sel, dd));
    });
    dd.appendChild(localGroup);
  }

  // 2. PROVIDER-WISE CLUSTERS
  const providerGroups = new Map();
  cloudOpts.forEach(o => {
    const provName = o.dataset.provider || 'Cloud';
    if (!providerGroups.has(provName)) {
      providerGroups.set(provName, []);
    }
    providerGroups.get(provName).push(o);
  });

  providerGroups.forEach((provOpts, provName) => {
    const group = document.createElement('div');
    group.className = 'model-picker-group';

    const title = document.createElement('div');
    title.className = 'model-picker-group-title';
    title.innerHTML = `<span class="mpg-name">☁️ ${typeof esc === 'function' ? esc(provName) : provName}</span>` +
      `<span class="mpg-count">${provOpts.length}</span>`;
    group.appendChild(title);

    provOpts.forEach(o => {
      group.appendChild(_createPickerItem(o, sel, dd));
    });

    dd.appendChild(group);
  });

  const cur = sel.options[sel.selectedIndex];
  if (cur && cur.value) {
    const isCloud = cur.dataset.kind === 'cloud';
    if (icon) icon.textContent = isCloud ? '☁️' : '🖥';
    let raw = _mpCleanLabel(cur.textContent);
    raw = raw.replace(/\s*·\s*(?:[👁🔨⚡◵].*)$/, '').trim();
    if (label) {
      label.textContent = _mpShorten(raw, 26);
      label.title = raw + (cur.dataset.provider ? ` (${cur.dataset.provider})` : '');
    }

    const capsEl = $('model-picker-caps');
    if (capsEl) {
      const visionSvg = `<svg width="11" height="11" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="9"/><circle cx="12" cy="12" r="3.5" fill="currentColor"/></svg>`;
      const toolsSvg = `<svg width="11" height="11" viewBox="0 0 24 24" fill="currentColor"><path d="M19.7 4.3a2.5 2.5 0 0 0-3.5 0l-2.1 2.1 3.5 3.5 2.1-2.1a2.5 2.5 0 0 0 0-3.5zM13 7.4 4.7 15.7a1 1 0 0 0 0 1.4l2.2 2.2a1 1 0 0 0 1.4 0L16.6 11 13 7.4z"/></svg>`;
      const mtpSvg = `<svg width="11" height="11" viewBox="0 0 24 24" fill="currentColor"><path d="M13 2 4.5 13.5h6L9.5 22l9-12h-6.5L13 2z"/></svg>`;
      const reasoningSvg = `<svg width="11" height="11" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="9"/><line x1="12" y1="3" x2="12" y2="21"/></svg>`;

      let b = '';
      if (cur.dataset.vision === '1') b += `<span class="cap-pill vision">${visionSvg} Vision</span>`;
      if (cur.dataset.tools === '1') b += `<span class="cap-pill tools">${toolsSvg} Tool Use</span>`;
      if (cur.dataset.mtp === '1') b += `<span class="cap-pill mtp">${mtpSvg} MTP</span>`;
      if (cur.dataset.reasoning === '1') b += `<span class="cap-pill reasoning">${reasoningSvg} Reasoning</span>`;
      capsEl.innerHTML = b;
    }
  } else {
    if (icon) icon.textContent = '🖥';
    if (label) {
      label.textContent = 'Select a model…';
      label.title = '';
    }
    const capsEl = $('model-picker-caps');
    if (capsEl) capsEl.innerHTML = '';
  }
}

function initModelPickerToggle() {
  const btn = document.getElementById('model-picker-btn');
  const dd = document.getElementById('model-picker-dropdown');
  if (!btn || !dd) return;
  if (btn._pickerBound) return;
  btn._pickerBound = true;

  const position = () => {
    const r = btn.getBoundingClientRect();
    dd.style.top = (r.bottom + 6) + 'px';
    const targetW = Math.max(360, Math.min(r.width, 540));
    dd.style.width = targetW + 'px';
    const left = Math.min(r.left, window.innerWidth - targetW - 16);
    dd.style.left = Math.max(8, left) + 'px';
  };

  btn.addEventListener('click', (e) => {
    e.stopPropagation();
    if (dd.classList.contains('open')) { dd.classList.remove('open'); return; }
    position();
    dd.classList.add('open');
  });
  document.addEventListener('click', (e) => {
    if (dd.classList.contains('open') && !dd.contains(e.target) && e.target !== btn) {
      dd.classList.remove('open');
    }
  });
  window.addEventListener('resize', () => { if (dd.classList.contains('open')) position(); });
}

// Run init on initial script load
initModelPickerToggle();

/* ---------------- launch config ---------------- */
function restoreDefault(id, val) {
  // if user deleted a value, snap the field back to its default
  if ($(id).value.trim() === '') { $(id).value = val; return true; }
  return false;
}

async function loadConfig() {
  try {
    // The drawer edits the model selected in the dropdown. Pass it always
    // (?model=) so values shown/saved target it whether loaded or not.
    const sel = $('profile');
    const target = (sel && sel.value) ? sel.value : (curStatus && curStatus.model);
    let url = '/control/config';
    if (target) url += '?model=' + encodeURIComponent(target);
    const c = await (await fetch(url)).json();
    if (c.error) return;
    if (c.cloud) {
      applyCloudConfigUI(c);
      updateModelCapabilitiesBar(c);
      return;
    }
    clearCloudConfigUI();
    fillConfigForm(c);
    updateModelCapabilitiesBar(c);
  } catch (e) {}
}

/* Cloud model selected: llama-server launch params are meaningless -> banner + disabled */
function isCloudValue(v) {
  return String(v || '').startsWith('cloud:');
}

function applyCloudConfigUI(c) {
  const card = $('sec-cfg');
  if (!card) return;
  let note = $('cfg-cloud-note');
  if (!note) {
    note = document.createElement('div');
    note.id = 'cfg-cloud-note';
    note.className = 'cfg-note';
    const sum = card.querySelector('summary');
    if (sum && sum.nextSibling) card.insertBefore(note, sum.nextSibling);
    else card.appendChild(note);
  }
  note.innerHTML = `\u2601 <b>${esc(c.display || c.model || '')}</b> is served by
    <b>${esc(c.provider || '')}</b> (cloud) — llama-server launch options don't apply.
    Use the <b>Cloud Models</b> card below to bind lanes; endpoints are deleted from
    <code>providers.json</code>, never added to git.`;
  card.querySelectorAll('.cfg-grid input, .cfg-grid select, .cfg-grid button').forEach(el => {
    el.disabled = true; el.style.opacity = '0.45';
  });
  if (c.context_size) { curCtxMax = c.context_size; updateContextChip(); }
  const dl = $('btn-load-header');
  if (dl) dl.title = 'Cloud model — nothing to load into VRAM';
}

function clearCloudConfigUI() {
  const card = $('sec-cfg');
  if (!card) return;
  const note = $('cfg-cloud-note');
  if (note) note.remove();
  card.querySelectorAll('.cfg-grid input, .cfg-grid select, .cfg-grid button').forEach(el => {
    el.disabled = false; el.style.opacity = '';
  });
  if (typeof updateGpuDependentFields === 'function') updateGpuDependentFields();
}

function fillConfigForm(c) {
    $('cfg-ctx').value = c.context_size ?? CFG_DEFAULTS.ctx;
    // Keep the context chip n_ctx in sync as soon as we know the model's window size
    if (c.context_size && c.context_size > 0) {
      curCtxMax = c.context_size;
      updateContextChip();
    }
    $('cfg-ngl').value = c.n_gpu_layers ?? CFG_DEFAULTS.ngl;
    $('cfg-threads').value = c.threads ?? CFG_DEFAULTS.threads;
    $('cfg-tb').value = c.threads_batch ?? CFG_DEFAULTS.tb;
    $('cfg-batch').value = c.batch_size ?? CFG_DEFAULTS.batch;
    $('cfg-ubatch').value = c.ubatch_size ?? CFG_DEFAULTS.ubatch;
    $('cfg-np').value = c.n_slots ?? CFG_DEFAULTS.np;
    $('cfg-kai').value = c.keepalive_interval_s ?? CFG_DEFAULTS.kai;
    $('cfg-ts').value = c.tensor_split ?? CFG_DEFAULTS.ts;
    updateGpuDependentFields();
    $('cfg-sm').value = c.split_mode || CFG_DEFAULTS.sm;
    $('cfg-fa').value = c.flash_attn || CFG_DEFAULTS.fa;
    $('cfg-ct').value = c.kv_cache_type || CFG_DEFAULTS.ct;
    // MTP section
    const mtpAvail = !!c.mtp_available;
    const mtpRow = $('mtp-row');
    if (mtpRow) {
      // a rejected draft (wrong model) still shows the row, switched off, with the reason
      mtpRow.style.display = (mtpAvail || c.mtp_note) ? '' : 'none';
      $('mtp-file').textContent = mtpAvail && c.mtp_draft_path ? c.mtp_draft_path.split('\\').pop().split('/').pop() : '';
      showCompanionNote('mtp-note', mtpAvail ? '' : c.mtp_note);
    }
    if ($('cfg-mtp')) { $('cfg-mtp').checked = mtpAvail && !!c.mtp_enabled; $('cfg-mtp').disabled = !mtpAvail; }
    if ($('mtp-nmax')) $('mtp-nmax').value = c.mtp_draft_n_max ?? 3;

    // Vision section (multimodal projector)
    const visAvail = !!c.mmproj_available;
    const visRow = $('vision-row');
    if (visRow) {
      visRow.style.display = (visAvail || c.mmproj_note) ? '' : 'none';
      $('vision-file').textContent = visAvail && c.mmproj_path ? c.mmproj_path.split('\\').pop().split('/').pop() : '';
      showCompanionNote('vision-note', visAvail ? '' : c.mmproj_note);
    }
    if ($('cfg-vision')) { $('cfg-vision').checked = visAvail && !!c.vision_capable; $('cfg-vision').disabled = !visAvail; }
}

/* Companion file found in the model folder but rejected by the header check */
function showCompanionNote(id, note) {
  const el = $(id);
  if (!el) return;
  el.textContent = note ? `⚠ Not attached: ${note}` : '';
  el.style.display = note ? 'block' : 'none';
}

/* Tensor split drives GPU selection: 0,1 = GPU 2 only, 1,0 = GPU 1 only, 9,11 = dual */
function parseTensorSplit(v) {
  return String(v || '').split(',').map(s => s.trim()).filter(s => s !== '');
}

function activeGpuCount() {
  const shares = parseTensorSplit($('cfg-ts') ? $('cfg-ts').value : '');
  if (shares.length > 1) return shares.filter(s => parseInt(s) > 0).length;
  return 2;   // default dual setup
}

function updateGpuDependentFields() {
  const multi = activeGpuCount() > 1;
  const ts = $('cfg-ts'), sm = $('cfg-sm');
  if (sm) { sm.disabled = !multi; sm.style.opacity = multi ? '' : '0.45'; }
}

if ($('cfg-ts')) $('cfg-ts').addEventListener('input', updateGpuDependentFields);

if ($('btn-apply')) $('btn-apply').onclick = async () => {
  // restore defaults for any field the user cleared
  restoreDefault('cfg-ctx', CFG_DEFAULTS.ctx);
  restoreDefault('cfg-ts', CFG_DEFAULTS.ts);
  const updates = {
    context_size: parseInt($('cfg-ctx').value) || CFG_DEFAULTS.ctx,
    n_gpu_layers: parseInt($('cfg-ngl').value) ?? CFG_DEFAULTS.ngl,
    threads: parseInt($('cfg-threads').value) || 0,
    threads_batch: parseInt($('cfg-tb').value) || 0,
    batch_size: parseInt($('cfg-batch').value) || 0,
    ubatch_size: parseInt($('cfg-ubatch').value) || 0,
    n_slots: parseInt($('cfg-np').value) || 0,
    keepalive_interval_s: parseInt($('cfg-kai').value) || CFG_DEFAULTS.kai,
    tensor_split: $('cfg-ts').value.trim() || CFG_DEFAULTS.ts,
    split_mode: $('cfg-sm').value,
    flash_attn: $('cfg-fa').value,
    kv_cache_type: $('cfg-ct').value,
    mtp_enabled: $('cfg-mtp') && $('cfg-mtp').checked,
    mtp_draft_n_max: parseInt($('mtp-nmax') && $('mtp-nmax').value) || 3,
    vision_capable: $('cfg-vision') && $('cfg-vision').checked,
  };
  const wasLoaded = !!(curStatus && curStatus.pid);
  toast('Saving config…');
  try {
    // persist only - no restart; user unloads/loads to apply.
    // Always target the dropdown-selected model.
    const sel = $('profile');
    const target = (sel && sel.value) ? sel.value : (curStatus && curStatus.model);
    let url = '/control/config';
    if (target) url += '?model=' + encodeURIComponent(target);
    const r = await fetch(url, {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ updates, persist: true, restart: false }),
    });
    const j = await r.json().catch(() => ({}));
    if (!r.ok) throw new Error(j.error || ('HTTP ' + r.status));
    // reflect server-side clamping (e.g. context 2M -> 1048576 max) in the form
    if (j.config) fillConfigForm(j.config);
  } catch (e) {
    toast('Config save failed: ' + e.message, true);
    return;
  }
  if (wasLoaded) {
    toast('Config saved ✓ Unload the model (■), then Load (▶) again to apply');
  } else {
    toast('Config saved ✓ It will be used on next load');
  }
};

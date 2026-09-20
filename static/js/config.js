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
        const isCloud = String(m.id).startsWith('cloud:');
        o.textContent = m.display + (isCloud ? '' : (m.currently_loaded ? ' (loaded)' : ' (not loaded)'));
        o.dataset.kind = isCloud ? 'cloud' : 'local';
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
      String(a.name || '').localeCompare(String(b.name || '')));
    locals.forEach(m => {
      const o = document.createElement('option');
      o.value = m.path;
      const name = (m.name || '').trim();
      const shortName = name.length > 20 ? (name.slice(0, 20) + '...') : name;
      const size = m.size_gb != null ? ` · ${m.size_gb}GB` : '';
      const mtp = m.mtp_available ? ' ⚡MTP' : '';
      o.textContent = `${shortName}${size}${mtp}`;
      o.title = `${m.name}${size}${mtp}`;
      o.dataset.mtp = m.mtp_available ? '1' : '';
      o.dataset.mtpPath = m.mtp_draft_path || '';
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
  } catch (e) {}
}

$('profile').onchange = e => {
  e.target._user = true;
  try { localStorage.setItem('app_model', e.target.value); } catch (err) {}
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

function renderModelPicker() {
  const sel = $('profile');
  const btn = $('model-picker-btn');
  const dd = $('model-picker-dropdown');
  if (!sel || !btn || !dd) return;   // settings.html has no custom picker

  const icon = $('model-picker-icon');
  const label = $('model-picker-label');

  dd.innerHTML = '';
  [...sel.options].forEach(o => {
    const isCloud = o.dataset.kind === 'cloud';
    const isLoaded = /\(loaded\)/.test(o.textContent);
    const raw = _mpCleanLabel(o.textContent);
    const item = document.createElement('button');
    item.type = 'button';
    item.className = 'model-picker-item' + (o.value === sel.value ? ' active' : '');
    item.title = raw;
    item.innerHTML = `<span class="mpi-icon">${isCloud ? '☁️' : '🖥'}</span>` +
      `<span class="mpi-label">${_mpShorten(raw, 34)}</span>` +
      (isLoaded ? '<span class="mpi-badge">loaded</span>' : '');
    item.onclick = () => {
      if (sel.value !== o.value) {
        sel.value = o.value;
        sel._user = true;
        sel.dispatchEvent(new Event('change'));
      }
      dd.classList.remove('open');
    };
    dd.appendChild(item);
  });

  const cur = sel.options[sel.selectedIndex];
  if (cur) {
    const isCloud = cur.dataset.kind === 'cloud';
    icon.textContent = isCloud ? '☁️' : '🖥';
    const raw = _mpCleanLabel(cur.textContent);
    label.textContent = _mpShorten(raw, 22);
    label.title = raw;
  } else {
    icon.textContent = '🖥';
    label.textContent = 'No model';
    label.title = '';
  }
}

(function initModelPickerToggle() {
  const btn = document.getElementById('model-picker-btn');
  const dd = document.getElementById('model-picker-dropdown');
  if (!btn || !dd) return;

  const position = () => {
    const r = btn.getBoundingClientRect();
    dd.style.top = (r.bottom + 6) + 'px';
    dd.style.left = r.left + 'px';
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
})();

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
    if (c.cloud) { applyCloudConfigUI(c); return; }
    clearCloudConfigUI();
    fillConfigForm(c);
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
      mtpRow.style.display = mtpAvail ? '' : 'none';
      $('mtp-file').textContent = mtpAvail && c.mtp_draft_path ? c.mtp_draft_path.split('\\').pop().split('/').pop() : '';
    }
    if ($('cfg-mtp')) $('cfg-mtp').checked = !!c.mtp_enabled;
    if ($('mtp-nmax')) $('mtp-nmax').value = c.mtp_draft_n_max ?? 3;
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

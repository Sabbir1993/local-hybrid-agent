/* dynamic model & profile list */
const CFG_DEFAULTS = {
  ctx: 32768, ngl: 999, threads: 0, tb: 0, batch: 2048, ubatch: 512,
  np: 1, kai: 25, ts: '9,11', sm: 'layer', fa: 'auto', ct: 'f16',
};

async function loadProfiles() {
  try {
    const d = await (await fetch('/control/profiles')).json();
    const sel = $('profile');
    sel.innerHTML = '';

    // Group GGUF models by family (text before first '-')
    const groups = {};
    (d.models || []).forEach(m => {
      const fam = (m.family || 'Other').trim() || 'Other';
      (groups[fam] = groups[fam] || []).push(m);
    });
    const famNames = Object.keys(groups).sort();

    famNames.forEach(fam => {
      const g = document.createElement('optgroup');
      g.label = '❒ ' + fam;
      groups[fam].forEach(m => {
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
        g.appendChild(o);
      });
      sel.appendChild(g);
    });

    if (!sel.options.length) {
      const o = document.createElement('option');
      o.value = '';
      o.textContent = 'No models found';
      sel.appendChild(o);
    }

    // restore last selection across page refreshes
    try {
      const saved = localStorage.getItem('app_model');
      if (saved && [...sel.options].some(o => o.value === saved)) {
        sel.value = saved;
      }
    } catch (e) {}
    loadConfig();   // dropdown is ready — fetch its saved config
  } catch (e) {}
}

$('profile').onchange = e => {
  e.target._user = true;
  try { localStorage.setItem('app_model', e.target.value); } catch (err) {}
  loadConfig();   // show the newly selected model's saved config
};

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
    fillConfigForm(c);
  } catch (e) {}
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

$('btn-apply').onclick = async () => {
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
    // reflect server-side clamping (e.g. context 200K -> 262144 max) in the form
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

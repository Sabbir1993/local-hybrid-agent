/* ---------------- sampling.js ----------------
 * System prompt & sampling parameters live on the Settings page.
 * Saved to localStorage (with cross-frame sync) and persisted to the server.
 */
const SAMPLING_DEFAULTS = {
  sysprompt: '', temp: 0.6, topp: 0.95, minp: 0.0, rep: 1.0,
  presence: 0.0, topk: 20, maxtok: -1,
  rlast: 64, freq: 0.0, seed: -1,
  drym: 0.0, dryb: 1.75, dryl: 2, dryn: 4096,
  dynr: 0.0, dyne: 1.0,
};

/* Extra llama.cpp controls: [key, kind, min, max, label decimals].
 * drym / dynr = 0 means the feature is off. seed < 0 means random. */
const SAMPLING_EXTRA = [
  ['rlast', 'int', -1, 8192], ['freq', 'float', -2, 2, 2], ['seed', 'int', -1, 2147483647],
  ['drym', 'float', 0, 5, 2], ['dryb', 'float', 1, 4, 2], ['dryl', 'int', 0, 64], ['dryn', 'int', -1, 32768],
  ['dynr', 'float', 0, 2, 2], ['dyne', 'float', 0.1, 5, 2],
];

function sanitizeSamplingConfig(raw) {
  const src = (raw && typeof raw === 'object') ? raw : {};
  const num = (v, def, min, max) => {
    const n = parseFloat(v);
    if (isNaN(n)) return def;
    if (min !== undefined && n < min) return min;
    if (max !== undefined && n > max) return max;
    return n;
  };
  const intNum = (v, def, min, max) => {
    const n = parseInt(v, 10);
    if (isNaN(n)) return def;
    if (min !== undefined && n < min) return min;
    if (max !== undefined && n > max) return max;
    return n;
  };

  return {
    sysprompt: typeof src.sysprompt === 'string' ? src.sysprompt : SAMPLING_DEFAULTS.sysprompt,
    temp: num(src.temp, SAMPLING_DEFAULTS.temp, 0, 2),
    topp: num(src.topp, SAMPLING_DEFAULTS.topp, 0, 1),
    minp: num(src.minp, SAMPLING_DEFAULTS.minp, 0, 1),
    rep: num(src.rep, SAMPLING_DEFAULTS.rep, 0, 3),
    presence: num(src.presence, SAMPLING_DEFAULTS.presence, -2, 2),
    topk: intNum(src.topk, SAMPLING_DEFAULTS.topk, 0, 1000),
    maxtok: intNum(src.maxtok, SAMPLING_DEFAULTS.maxtok, -1, 1048576),
    ...Object.fromEntries(SAMPLING_EXTRA.map(([k, kind, lo, hi]) =>
      [k, (kind === 'int' ? intNum : num)(src[k], SAMPLING_DEFAULTS[k], lo, hi)])),
  };
}

/* Request-body fields for the extra llama.cpp controls (server applies the off/random rules). */
function samplingExtraBody(cfg) {
  const c = cfg || getSamplingConfig();
  return {
    repeat_last_n: c.rlast, frequency_penalty: c.freq, seed: c.seed,
    dry_multiplier: c.drym, dry_base: c.dryb, dry_allowed_length: c.dryl, dry_penalty_last_n: c.dryn,
    dynatemp_range: c.dynr, dynatemp_exponent: c.dyne,
  };
}

function loadSamplingConfig() {
  try {
    const raw = localStorage.getItem('sampling_config');
    if (raw) return sanitizeSamplingConfig(JSON.parse(raw));
  } catch (e) {}
  return Object.assign({}, SAMPLING_DEFAULTS);
}

function saveSamplingConfig(cfg) {
  const clean = sanitizeSamplingConfig(cfg);
  try {
    localStorage.setItem('sampling_config', JSON.stringify(clean));
  } catch (e) {}
  try {
    if (window.parent && window.parent !== window && window.parent.localStorage) {
      window.parent.localStorage.setItem('sampling_config', JSON.stringify(clean));
    }
  } catch (e) {}
  try {
    if (window.parent && window.parent !== window) {
      window.parent.postMessage({ type: 'sampling-config-changed', config: clean }, '*');
    }
  } catch (e) {}
  // Persist to server in background
  try {
    fetch('/control/sampling', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(clean),
    }).catch(() => {});
  } catch (e) {}
  return clean;
}

function getSamplingConfig() {
  return loadSamplingConfig();
}

window.addEventListener('message', (ev) => {
  if (ev.data && ev.data.type === 'sampling-config-changed' && ev.data.config) {
    try {
      localStorage.setItem('sampling_config', JSON.stringify(ev.data.config));
    } catch (_) {}
  }
});

/* Settings page: wire the Sampling card inputs to load/save/persist. */
function initSamplingPanel() {
  if (!document.getElementById('sysprompt')) return;   // not on this page
  let cfg = loadSamplingConfig();
  const $$ = (id) => document.getElementById(id);
  const setLabel = (id, val, fmt) => { const lb = $$(id + 'v'); if (lb) lb.textContent = fmt(val); };

  const applyToUI = (c) => {
    if ($$('sysprompt')) $$('sysprompt').value = c.sysprompt || '';
    if ($$('temp')) { $$('temp').value = c.temp; setLabel('temp', c.temp, v => parseFloat(v).toFixed(2)); }
    if ($$('topp')) { $$('topp').value = c.topp; setLabel('topp', c.topp, v => parseFloat(v).toFixed(2)); }
    if ($$('minp')) { $$('minp').value = c.minp; setLabel('minp', c.minp, v => parseFloat(v).toFixed(3)); }
    if ($$('rep')) { $$('rep').value = c.rep; setLabel('rep', c.rep, v => parseFloat(v).toFixed(2)); }
    if ($$('presence')) { $$('presence').value = c.presence; setLabel('presence', c.presence, v => parseFloat(v).toFixed(2)); }
    if ($$('topk')) $$('topk').value = c.topk;
    if ($$('maxtok')) $$('maxtok').value = c.maxtok;
    SAMPLING_EXTRA.forEach(([k, kind, , , dec]) => {
      if (!$$(k)) return;
      $$(k).value = c[k];
      if (kind === 'float') setLabel(k, c[k], v => parseFloat(v).toFixed(dec));
    });
  };

  applyToUI(cfg);

  // Sync from server if available (e.g. fresh browser or cleared storage)
  fetch('/control/sampling').then(r => r.json()).then(serverCfg => {
    if (serverCfg && typeof serverCfg === 'object' && !serverCfg.error) {
      const raw = localStorage.getItem('sampling_config');
      if (!raw) {
        cfg = sanitizeSamplingConfig(serverCfg);
        applyToUI(cfg);
        try { localStorage.setItem('sampling_config', JSON.stringify(cfg)); } catch (_) {}
      }
    }
  }).catch(() => {});

  const readFromUI = () => sanitizeSamplingConfig({
    sysprompt: $$('sysprompt') ? $$('sysprompt').value : '',
    temp: $$('temp') ? $$('temp').value : SAMPLING_DEFAULTS.temp,
    topp: $$('topp') ? $$('topp').value : SAMPLING_DEFAULTS.topp,
    minp: $$('minp') ? $$('minp').value : SAMPLING_DEFAULTS.minp,
    rep: $$('rep') ? $$('rep').value : SAMPLING_DEFAULTS.rep,
    presence: $$('presence') ? $$('presence').value : SAMPLING_DEFAULTS.presence,
    topk: $$('topk') ? $$('topk').value : SAMPLING_DEFAULTS.topk,
    maxtok: $$('maxtok') ? $$('maxtok').value : SAMPLING_DEFAULTS.maxtok,
    ...Object.fromEntries(SAMPLING_EXTRA.map(([k]) => [k, $$(k) ? $$(k).value : SAMPLING_DEFAULTS[k]])),
  });

  const showStatus = (msg = '✓ Saved') => {
    const el = $$('sampling-save-status');
    if (el) {
      el.textContent = msg;
      el.style.display = 'inline';
      clearTimeout(el._tid);
      el._tid = setTimeout(() => { el.style.display = 'none'; }, 2500);
    }
    if (typeof toast === 'function') {
      toast(msg.replace(/^✓\s*/, ''));
    }
  };

  const persist = (notify = false) => {
    saveSamplingConfig(readFromUI());
    if (notify) showStatus('✓ Saved');
  };

  // Immediate input events so typing and slider drags persist without needing blur
  ['input', 'change', 'blur'].forEach(ev => {
    if ($$('sysprompt')) $$('sysprompt').addEventListener(ev, () => persist(false));
    if ($$('topk')) $$('topk').addEventListener(ev, () => persist(false));
    if ($$('maxtok')) $$('maxtok').addEventListener(ev, () => persist(false));
  });

  if ($$('temp')) $$('temp').addEventListener('input', () => { setLabel('temp', $$('temp').value, v => parseFloat(v).toFixed(2)); persist(false); });
  if ($$('topp')) $$('topp').addEventListener('input', () => { setLabel('topp', $$('topp').value, v => parseFloat(v).toFixed(2)); persist(false); });
  if ($$('minp')) $$('minp').addEventListener('input', () => { setLabel('minp', $$('minp').value, v => parseFloat(v).toFixed(3)); persist(false); });
  if ($$('rep')) $$('rep').addEventListener('input', () => { setLabel('rep', $$('rep').value, v => parseFloat(v).toFixed(2)); persist(false); });
  if ($$('presence')) $$('presence').addEventListener('input', () => { setLabel('presence', $$('presence').value, v => parseFloat(v).toFixed(2)); persist(false); });

  SAMPLING_EXTRA.forEach(([k, kind, , , dec]) => {
    const el = $$(k);
    if (!el) return;
    if (kind === 'float') {
      el.addEventListener('input', () => { setLabel(k, el.value, v => parseFloat(v).toFixed(dec)); persist(false); });
    } else {
      ['input', 'change', 'blur'].forEach(ev => el.addEventListener(ev, () => persist(false)));
    }
  });

  // Explicit Save button
  const saveBtn = $$('btn-save-sampling');
  if (saveBtn) {
    saveBtn.onclick = (e) => {
      e.preventDefault();
      persist(true);
    };
  }

  // Explicit Reset button
  const resetBtn = $$('btn-reset-sampling');
  if (resetBtn) {
    resetBtn.onclick = (e) => {
      e.preventDefault();
      applyToUI(SAMPLING_DEFAULTS);
      saveSamplingConfig(SAMPLING_DEFAULTS);
      showStatus('↺ Reset to defaults');
    };
  }
}

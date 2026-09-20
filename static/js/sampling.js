/* ---------------- sampling.js ----------------
 * System prompt & sampling parameters now live on the Settings page (global
 * defaults) instead of a per-page drawer -- ui.html no longer hosts these
 * input elements directly, so chat/agent request builders read the last
 * saved values from localStorage instead of the DOM.
 */
const SAMPLING_DEFAULTS = {
  sysprompt: '', temp: 1.0, topp: 0.95, minp: 0, rep: 1.0,
  presence: 0, topk: 20, maxtok: -1,
};

function loadSamplingConfig() {
  try {
    const raw = localStorage.getItem('sampling_config');
    if (raw) return Object.assign({}, SAMPLING_DEFAULTS, JSON.parse(raw));
  } catch (e) {}
  return Object.assign({}, SAMPLING_DEFAULTS);
}

function saveSamplingConfig(cfg) {
  try { localStorage.setItem('sampling_config', JSON.stringify(cfg)); } catch (e) {}
}

function getSamplingConfig() {
  return loadSamplingConfig();
}

/* Settings page only: wire the Sampling card's inputs to load/save/persist. */
function initSamplingPanel() {
  if (!document.getElementById('sysprompt')) return;   // not on this page
  const cfg = loadSamplingConfig();
  const $$ = (id) => document.getElementById(id);
  const setLabel = (id, val, fmt) => { const lb = $$(id + 'v'); if (lb) lb.textContent = fmt(val); };

  $$('sysprompt').value = cfg.sysprompt;
  $$('temp').value = cfg.temp; setLabel('temp', cfg.temp, v => parseFloat(v).toFixed(2));
  $$('topp').value = cfg.topp; setLabel('topp', cfg.topp, v => parseFloat(v).toFixed(2));
  $$('minp').value = cfg.minp; setLabel('minp', cfg.minp, v => parseFloat(v).toFixed(3));
  $$('rep').value = cfg.rep; setLabel('rep', cfg.rep, v => parseFloat(v).toFixed(2));
  $$('presence').value = cfg.presence; setLabel('presence', cfg.presence, v => parseFloat(v).toFixed(2));
  $$('topk').value = cfg.topk;
  $$('maxtok').value = cfg.maxtok;

  const persist = () => saveSamplingConfig({
    sysprompt: $$('sysprompt').value,
    temp: parseFloat($$('temp').value),
    topp: parseFloat($$('topp').value),
    minp: parseFloat($$('minp').value),
    rep: parseFloat($$('rep').value),
    presence: parseFloat($$('presence').value),
    topk: parseInt($$('topk').value),
    maxtok: parseInt($$('maxtok').value),
  });

  $$('sysprompt').addEventListener('change', persist);
  $$('temp').addEventListener('input', () => { setLabel('temp', $$('temp').value, v => parseFloat(v).toFixed(2)); persist(); });
  $$('topp').addEventListener('input', () => { setLabel('topp', $$('topp').value, v => parseFloat(v).toFixed(2)); persist(); });
  $$('minp').addEventListener('input', () => { setLabel('minp', $$('minp').value, v => parseFloat(v).toFixed(3)); persist(); });
  $$('rep').addEventListener('input', () => { setLabel('rep', $$('rep').value, v => parseFloat(v).toFixed(2)); persist(); });
  $$('presence').addEventListener('input', () => { setLabel('presence', $$('presence').value, v => parseFloat(v).toFixed(2)); persist(); });
  $$('topk').addEventListener('change', persist);
  $$('maxtok').addEventListener('change', persist);
}

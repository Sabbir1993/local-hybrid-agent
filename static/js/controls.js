/* ---------------- server controls ---------------- */
async function warmup() {
  try {
    toast('Warming up shaders (first request compile takes ~10s)…');
    await fetch('/v1/chat/completions', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ messages: [{ role: 'user', content: 'hi' }], max_tokens: 1, temperature: 0 }),
    });
  } catch (e) { /* non-fatal */ }
}

async function loadSelectedModel() {
  const sel = $('profile');
  const target = sel.value;
  if (!target) { toast('Please select a model from the dropdown first', true); return; }

  // Cloud model: nothing to load. Selecting it binds the MAIN lane (who answers
  // you) and leaves the local llama-server / VRAM untouched.
  if (typeof isCloudValue === 'function' && isCloudValue(target)) {
    try {
      const r = await fetch('/control/switch', {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ target: target })
      });
      const j = await r.json().catch(() => ({}));
      if (!r.ok) throw new Error(j.error || ('HTTP ' + r.status));
      toast('\u2601 Main lane \u2192 ' + (j.display || target) + ' (' + (j.provider || '') + ') — no VRAM used');
      pollStatus(); loadConfig();
      if (typeof loadCloudCard === 'function') loadCloudCard();
    } catch (e) {
      toast('Cloud switch failed: ' + e.message, true);
    }
    return;
  }

  if (curStatus && curStatus.pid) {  // loaded -> unload
    if (ctrl) ctrl.abort();
    try { await fetch('/control/stop', { method: 'POST' }); } catch (e) {}
    pollStatus(); pollGpu();
    if (typeof loadProfiles === 'function') loadProfiles();
    toast('Model unloaded — VRAM freed on both A770s');
    return;
  }
  setPill('loading');
  toast('Loading model into GPU VRAM (~10–60s)…');
  // Preflight VRAM check: show suggestions before the manager refuses to load
  try {
    const pr = await fetch('/control/preflight?target=' + encodeURIComponent(target));
    if (pr.ok) {
      const plan = await pr.json();
      if (plan.status === 'nofit') {
        const sugs = (plan.suggestions || []).slice(0, 3).map(s => s.desc).join(' | ');
        setPill('off');
        toast('VRAM check: it won\u2019t fit. ' + (sugs || plan.message || 'Reduce GPU offload.'), true);
        return;
      }
      if (plan.status === 'tight') {
        toast('VRAM check: tight fit — loading anyway (watch the GPU panel).');
      }
    }
  } catch (e) { /* preflight is advisory only */ }
  try {
    const r = await fetch('/control/switch', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ target: target })
    });
    const j = await r.json().catch(() => ({}));
    if (!r.ok) throw new Error(j.error || ('HTTP ' + r.status));
  } catch (e) {
    toast('Load failed: ' + e.message, true);
    pollStatus();
    return;
  }
  await warmup();
  pollStatus();
  loadConfig();
  toast('Model loaded ✅');
}

if ($('btn-load-header')) $('btn-load-header').onclick = loadSelectedModel;

function setLoadBtn(mode) {
  const b = $('btn-load-header');
  if (!b) return;
  b.classList.remove('unload', 'loading');
  if (mode === 'cloud') {
    b.innerHTML = '<span>\u2601</span>';
    b.title = 'Cloud model — nothing to load into VRAM. Pick a local GGUF to use the GPUs again.';
  } else if (mode === 'on') {
    b.innerHTML = '<span>■</span>';
    b.classList.add('unload');
    b.title = 'Unload model — terminate llama-server and free VRAM/DRAM';
  } else if (mode === 'loading') {
    b.innerHTML = '<span class="spinner"></span>';
    b.classList.add('loading');
    b.title = 'Loading model into VRAM…';
  } else {
    b.innerHTML = '<span>▶</span>';
    b.title = 'Load the selected model into GPU VRAM';
  }
}

/* clear everything */
if ($('m-cancel')) $('m-cancel').onclick = () => { $('modal-bg').hidden = true; };
if ($('modal-bg')) $('modal-bg').onclick = e => { if (e.target.id === 'modal-bg') $('modal-bg').hidden = true; };
if ($('m-ok')) $('m-ok').onclick = async () => {
  $('modal-bg').hidden = true;
  if (ctrl) ctrl.abort();
  try { await fetch('/control/stop', { method: 'POST' }); } catch (e) {}
  messages = [];
  renderAll();
  pollStatus(); pollGpu();
  toast('Cleared — model unloaded, VRAM freed, chat reset');
};

/* keepalive toggle (Runtime card) */
if ($('ka')) $('ka').onchange = async e => {
  const on = e.target.checked;
  e.target._user = true;
  try {
    const r = await fetch('/control/keepalive', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ enabled: on }),
    });
    const j = await r.json();
    toast('Keepalive ' + (j.keepalive ? 'ON — VRAM stays resident at full speed' : 'OFF'));
  } catch (err) {
    toast('Keepalive toggle failed', true);
  }
};

/* chat clear (chat only) */
if ($('btn-chatclear')) $('btn-chatclear').onclick = () => { messages = []; renderAll(); };

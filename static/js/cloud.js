/* ---------------- cloud providers & lane routing (Settings → Cloud Models) ---------------- */
let cloudSnap = null;

async function loadCloudCard() {
  const box = $('cloud-content');
  if (!box) return;
  try {
    const d = await (await fetch('/control/cloud')).json();
    if (d.error) { box.innerHTML = '<div class="mon-empty">' + esc(d.error) + '</div>'; return; }
    cloudSnap = d;
    box.innerHTML = renderCloudCard(d);
    wireCloudCard();
  } catch (e) {
    box.innerHTML = '<div class="mon-empty">Failed: ' + esc(e.message) + '</div>';
  }
}

/* Quick starts for the "Add provider" form: only a name and a public base URL, never a key. */
const CLOUD_PRESETS = [
  { provider: 'openrouter', name: 'OpenRouter', url: 'https://openrouter.ai/api/v1', key: 'sk-or-v1-…' },
  { provider: 'openai', name: 'OpenAI', url: 'https://api.openai.com/v1', key: 'sk-…' },
];

function cloudHost(url) {
  try { return new URL(url).host; } catch (_) { return url || ''; }
}

function cloudHue(text) {
  let h = 0;
  for (const c of String(text)) h = (h * 31 + c.charCodeAt(0)) % 360;
  return h;
}

function cloudCtx(n) {
  if (!n) return '';
  return n >= 1000 ? Math.round(n / 1000) + 'K context' : n + ' context';
}

function cloudModelRow(p, m) {
  const prov = esc(p.provider), mid = esc(m.id);
  return `<div class="cp-model">
    <div class="cp-model-main"><b>${esc(m.name)}</b><code title="${mid}">${mid}</code></div>
    ${m.ctx ? `<span class="cp-chip">${esc(cloudCtx(m.ctx))}</span>` : ''}
    <div class="cp-model-actions">
      <button type="button" class="cp-toggle cloud-vis-model${m.vision ? ' on' : ''}" data-prov="${prov}" data-mid="${mid}" data-on="${m.vision ? 1 : 0}"
        title="${m.vision ? 'This model can read images. Click to turn off.' : 'Click if this model can read images.'}">Reads images</button>
      <button type="button" class="cp-mini cloud-test-model" data-prov="${prov}" data-mid="${mid}" title="Send a short test message to ${mid}">Test</button>
      <button type="button" class="cp-mini cp-danger cloud-del-model" data-prov="${prov}" data-mid="${mid}" title="Remove ${mid} from this provider">Remove</button>
    </div>
  </div>`;
}

function renderCloudCard(d) {
  const provs = d.providers || [];
  const models = d.models || [];
  const plural = (n, one, many) => `<b>${n}</b> ${n === 1 ? one : many}`;

  let h = `<div class="cp-bar">
    <span>${plural(provs.length, 'provider', 'providers')} · ${plural(models.length, 'cloud model', 'cloud models')}</span>
    <button type="button" class="btn accent" id="cloud-add-toggle">＋ Add provider</button>
  </div>`;

  if (!provs.length) {
    h += `<div class="cp-empty">
      <div class="cp-empty-icon">☁</div>
      <b>Connect a cloud provider</b>
      <p>Add any OpenAI-compatible service with its web address and an API key, then pick which of its models to use. Your key stays in your own private file and is never shown to other people.</p>
    </div>`;
  } else {
    provs.forEach(p => {
      const hue = cloudHue(p.provider);
      h += `<section class="cp-card">
        <header class="cp-head">
          <span class="cp-avatar" style="--h:${hue}" aria-hidden="true">${esc((p.name || p.provider).charAt(0).toUpperCase())}</span>
          <div class="cp-title"><b>${esc(p.name)}</b><span class="cp-host" title="${esc(p.base_url || '')}">${esc(cloudHost(p.base_url) || 'no web address')}</span></div>
          <div class="cp-actions">
            <button type="button" class="cp-mini cloud-test" data-prov="${esc(p.provider)}" title="Send a short test message with this provider's first model">Test</button>
            <button type="button" class="cp-mini cloud-add-model" data-prov="${esc(p.provider)}" title="Add another model to this provider">＋ Model</button>
            <button type="button" class="cp-mini cloud-edit" data-prov="${esc(p.provider)}" title="Change the name, web address or key">Edit</button>
            <button type="button" class="cp-mini cp-danger cloud-del" data-prov="${esc(p.provider)}" title="Remove this provider and its key">Remove</button>
          </div>
        </header>
        <div class="cp-key">
          <span class="cp-label">API key</span>
          <code class="cloud-key" data-prov="${esc(p.provider)}" data-masked="${esc(p.key_masked || '(none)')}">${esc(p.key_masked || '(none)')}</code>
          ${p.has_key ? `<button type="button" class="cp-mini cloud-reveal" data-prov="${esc(p.provider)}" title="Show the key for a few seconds">Show</button>` : '<span class="cp-chip cp-warn">No key saved</span>'}
        </div>
        <div class="cp-models">
          ${(p.models || []).map(m => cloudModelRow(p, m)).join('') || '<div class="cp-none">No models yet. Use “＋ Model” to add one.</div>'}
        </div>
      </section>`;
    });
  }

  // Which model does each job (main / helper / image reader, local fallback) is
  // set in Settings -> Models & Jobs (static/js/lanes.js) - one place only.
  h += `<div class="cp-foot">
    <span>Choose which model does each job in <b>Models &amp; Jobs</b>.</span>
    <button type="button" class="btn ghost" id="cl-goto-lanes">Open Models &amp; Jobs</button>
  </div>
  <div id="cl-result" class="cp-result" role="status"></div>`;

  // ---- add / edit provider form ----
  const field = (id, label, input, hint) => `<div class="cp-field"><label for="${id}">${label}</label>${input}${hint ? `<div class="cp-hint">${hint}</div>` : ''}</div>`;
  h += `<div id="cloud-form" class="cp-form" hidden>
    <div class="cp-form-head"><b id="cf-title">Add a provider</b>
      <div class="cp-presets" id="cf-presets"><span class="cp-label">Quick start</span>
        ${CLOUD_PRESETS.map((x, i) => `<button type="button" class="cp-chip cp-preset" data-i="${i}">${esc(x.name)}</button>`).join('')}
      </div>
    </div>
    <fieldset class="cp-set"><legend>Connection</legend>
      <div class="cp-grid">
        ${field('cf-name', 'Name', '<input type="text" id="cf-name" placeholder="OpenRouter">', 'Shown in lists.')}
        ${field('cf-prov', 'Short id', '<input type="text" id="cf-prov" placeholder="openrouter" autocapitalize="off" spellcheck="false">', 'Lowercase, no spaces. It cannot change later.')}
        <div class="cp-span">${field('cf-url', 'Web address (base URL)', '<input type="text" id="cf-url" placeholder="https://openrouter.ai/api/v1" spellcheck="false">', 'The address your provider gives for its OpenAI-compatible API.')}</div>
        <div class="cp-span">${field('cf-key', 'API key', '<input type="password" id="cf-key" placeholder="paste your key" autocomplete="off" spellcheck="false">', 'Stored only in your private provider file. Leave empty to keep the saved key.')}</div>
      </div>
    </fieldset>
    <fieldset class="cp-set"><legend>Models</legend>
      <div class="cp-grid">
        ${field('cf-mid', 'Model id(s)', '<input type="text" id="cf-mid" placeholder="vendor/model-name" spellcheck="false">', 'Copy it from the provider. Separate several with commas; existing models are kept.')}
        ${field('cf-mlabel', 'Display name(s)', '<input type="text" id="cf-mlabel" placeholder="Model name">', 'Optional, in the same order.')}
        ${field('cf-mctx', 'Context size (tokens)', '<input type="number" id="cf-mctx" placeholder="262144">', '')}
        <label class="cp-check"><input type="checkbox" id="cf-mvision"> <span><b>Can read images</b><br><span class="cp-hint">“Reading images” can then use this model directly instead of starting a separate image reader.</span></span></label>
      </div>
    </fieldset>
    <details class="cp-adv"><summary>Advanced</summary>
      <div class="cp-grid">
        <div class="cp-span">${field('cf-headers', 'Extra request headers', '<input type="text" id="cf-headers" placeholder="HTTP-Referer=https://myapp.com, X-Title=My App" spellcheck="false">', 'Optional name=value pairs, separated by commas, sent with every request. A default HTTP-Referer and X-Title are sent for you.')}</div>
        <div class="cp-span">${field('cf-npm', 'npm package', '<input type="text" id="cf-npm" placeholder="@ai-sdk/openai-compatible" spellcheck="false">', 'For reference only; not used while running.')}</div>
      </div>
    </details>
    <div class="cp-form-foot">
      <button type="button" class="btn ghost" id="cf-cancel">Cancel</button>
      <button type="button" class="btn accent" id="cf-save">Save provider</button>
    </div>
  </div>`;

  return h;
}

function wireCloudCard() {
  const box = $('cloud-content');
  if (!box) return;
  const form = box.querySelector('#cloud-form');
  const g = id => box.querySelector(id);

  const showForm = (pre, mode) => {
    form.hidden = false;
    const editing = !!pre && mode !== 'new';
    const title = g('#cf-title');
    if (title) title.textContent = mode === 'model' ? 'Add a model to ' + (pre.name || pre.provider) : editing ? 'Edit ' + (pre.name || pre.provider) : 'Add a provider';
    const presets = g('#cf-presets');
    if (presets) presets.hidden = !!pre;
    g('#cf-prov').readOnly = !!pre;      // the id names the saved file entry: it cannot change once created
    g('#cf-prov').value = (pre && pre.provider) || '';
    g('#cf-name').value = (pre && pre.name) || '';
    g('#cf-url').value = (pre && pre.base_url) || '';
    g('#cf-key').value = '';
    const m0 = (pre && (pre.models || [])[0]) || null;
    g('#cf-mid').value = (pre && pre.models ? pre.models.map(x => x.id).join(', ') : '') || (m0 ? m0.id : '');
    g('#cf-mlabel').value = m0 ? m0.name : '';
    g('#cf-mctx').value = m0 && m0.ctx ? m0.ctx : 262144;   // 256K default for new models
    g('#cf-mvision').checked = !!(m0 && m0.vision);
    g('#cf-npm').value = (pre && pre.npm) || '';
    // Reconstruct headers string from the saved provider info
    const savedProv = pre ? (cloudSnap.providers || []).find(x => x.provider === pre.provider) : null;
    const savedHeaders = (savedProv && savedProv.extra_headers) || (pre && pre._opts && pre._opts.extra_headers) || {};
    g('#cf-headers').value = Object.entries(savedHeaders).map(([k, v]) => `${k}=${v}`).join(', ');
    g('#cf-key').placeholder = pre && pre.has_key ? 'leave empty to keep the saved key' : 'paste your key';
    form.scrollIntoView({ block: 'nearest', behavior: 'smooth' });
  };

  box.querySelectorAll('.cp-preset').forEach(b => {
    b.onclick = () => {
      const x = CLOUD_PRESETS[parseInt(b.dataset.i, 10)];
      if (!x) return;
      g('#cf-prov').value = x.provider; g('#cf-name').value = x.name; g('#cf-url').value = x.url; g('#cf-key').placeholder = x.key;
      g('#cf-key').focus();
    };
  });

  const tgl = g('#cloud-add-toggle');
  if (tgl) tgl.onclick = () => { if (form.hidden) showForm(null); else form.hidden = true; };
  const cancel = g('#cf-cancel');
  if (cancel) cancel.onclick = () => { form.hidden = true; };

  const save = g('#cf-save');
  if (save) save.onclick = async () => {
    const prov = g('#cf-prov').value.trim();
    const url = g('#cf-url').value.trim();
    const midRaw = g('#cf-mid').value.trim();
    const labelRaw = g('#cf-mlabel').value.trim();
    if (!prov || !url || !midRaw) { toast('Provider id, base URL and model id are required', true); return; }
    // Support multiple model ids in one field (comma-separated)
    const idList = midRaw.split(',').map(s => s.trim()).filter(s => s);
    const labelList = labelRaw ? labelRaw.split(',').map(s => s.trim()) : [];
    const models = idList.map((id, i) => {
      const m = { id, name: labelList[i] || id };
      const ctx = parseInt(g('#cf-mctx').value);
      if (ctx > 0 && i === 0) m.ctx = ctx;   // shared ctx for now
      const was = (((cloudSnap.providers || []).find(x => x.provider === prov) || {}).models || []).find(x => x.id === id);
      if (g('#cf-mvision').checked) m.vision = true;
      else if (was && was.vision) m.vision = false;   // unticked: clear an existing marker
      return m;
    });
    // Parse extra headers: "Key=Value, Key2=Value2"
    const headersRaw = g('#cf-headers').value.trim();
    let extra_headers = {};
    if (headersRaw) {
      headersRaw.split(',').forEach(pair => {
        const [k, ...rest] = pair.split('=');
        const key = k.trim();
        const val = rest.join('=').trim();
        if (key && val) extra_headers[key] = val;
      });
    }
    const body = {
      provider: prov,
      name: g('#cf-name').value.trim() || prov,
      base_url: url,
      api_key: g('#cf-key').value.trim() || undefined,
      npm: g('#cf-npm').value.trim() || undefined,
      models,
    };
    if (Object.keys(extra_headers).length) body.extra_headers = extra_headers;
    save.disabled = true;
    try {
      const r = await fetch('/control/cloud/provider', {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(body),
      });
      const j = await r.json().catch(() => ({}));
      if (!r.ok) throw new Error(j.error || ('HTTP ' + r.status));
      toast('\u2601 Saved provider ' + prov + ' — press Test to verify it');
      await loadCloudCard();
      if (typeof loadProfiles === 'function') loadProfiles();
    } catch (e) {
      toast('Save failed: ' + e.message, true);
    } finally {
      save.disabled = false;
    }
  };

  box.querySelectorAll('.cloud-edit').forEach(b => {
    b.onclick = () => {
      const p = (cloudSnap.providers || []).find(x => x.provider === b.dataset.prov);
      showForm(p || null);
    };
  });

  // "+ model": same provider, but leave the model fields blank so saving only
  // adds a new model instead of re-showing (and risking a typo dropping) the
  // existing comma-joined list.
  box.querySelectorAll('.cloud-add-model').forEach(b => {
    b.onclick = () => {
      const p = (cloudSnap.providers || []).find(x => x.provider === b.dataset.prov);
      showForm(p ? { ...p, models: [] } : null, 'model');
      const midEl = g('#cf-mid');
      if (midEl) midEl.focus();
      toast('Provider fields kept — just add the new model id below and Save', false);
    };
  });

  box.querySelectorAll('.cloud-del').forEach(b => {
    b.onclick = async () => {
      const prov = b.dataset.prov;
      if (!confirm('Remove provider "' + prov + '"?\nLanes bound to it go back to local, and its key is deleted from your provider config.')) return;
      try {
        const r = await fetch('/control/cloud/provider?name=' + encodeURIComponent(prov), { method: 'DELETE' });
        const j = await r.json().catch(() => ({}));
        if (!r.ok) throw new Error(j.error || ('HTTP ' + r.status));
        toast('Removed provider ' + prov);
        await loadCloudCard();
        if (typeof loadProfiles === 'function') loadProfiles();
      } catch (e) { toast('Delete failed: ' + e.message, true); }
    };
  });

  box.querySelectorAll('.cloud-reveal').forEach(b => {
    let timer = null;
    b.onclick = async () => {
      const code = box.querySelector('.cloud-key[data-prov="' + b.dataset.prov + '"]');
      if (!code) return;
      const hide = () => { clearTimeout(timer); code.textContent = code.dataset.masked; b.textContent = 'Show'; };
      if (b.textContent === 'Hide') { hide(); return; }
      try {
        const d = await (await fetch('/control/cloud/key?provider=' + encodeURIComponent(b.dataset.prov))).json();
        code.textContent = d.api_key || '(none)';
        b.textContent = 'Hide';
        timer = setTimeout(hide, 10000);          // a revealed key does not stay on screen
      } catch (e) { toast('Could not show the key: ' + e.message, true); }
    };
  });

  const runProbe = async (provName, mid) => {
    const out = g('#cl-result');
    if (out) out.textContent = 'Testing ' + provName + '/' + mid + '…';
    try {
      const r = await fetch('/control/cloud/test', {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ key: provName + '/' + mid }),
      });
      const j = await r.json().catch(() => ({}));
      if (!r.ok) throw new Error(j.error || ('HTTP ' + r.status));
      const msg = j.ok
        ? '\u2713 ' + j.model + ' replied in ' + j.ms + ' ms' + (j.sample ? ' — "' + j.sample.slice(0, 40) + '"' : '')
        : '\u2717 ' + (j.error || 'failed') + ' (' + j.ms + ' ms)';
      if (out) out.textContent = msg;
      toast(j.ok ? msg : 'Cloud test failed: ' + (j.error || ''), !j.ok);
    } catch (e) {
      if (out) out.textContent = '\u2717 ' + e.message;
      toast('Cloud test failed: ' + e.message, true);
    }
  };

  box.querySelectorAll('.cloud-test').forEach(b => {
    b.onclick = async () => {
      const prov = (cloudSnap.providers || []).find(x => x.provider === b.dataset.prov);
      const mid = prov && (prov.models || [])[0] ? prov.models[0].id : null;
      if (!mid) { toast('Add a model to this provider first', true); return; }
      await runProbe(prov.provider, mid);
    };
  });

  // Per-model 👁 links: mark / unmark "can read images" without touching anything else.
  box.querySelectorAll('.cloud-vis-model').forEach(a => {
    a.onclick = async (e) => {
      e.preventDefault(); e.stopPropagation();
      const on = a.dataset.on !== '1';
      try {
        const r = await fetch('/control/cloud/provider', {
          method: 'POST', headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ provider: a.dataset.prov, models: [{ id: a.dataset.mid, vision: on }] }),
        });
        const j = await r.json().catch(() => ({}));
        if (!r.ok) throw new Error(j.error || ('HTTP ' + r.status));
        toast(on ? a.dataset.mid + ' can now read images' : a.dataset.mid + ' is no longer marked as reading images');
        await loadCloudCard();
        if (typeof lanesLoad === 'function') lanesLoad();
      } catch (err) { toast('Save failed: ' + err.message, true); }
    };
  });

  // Per-model ✓ links next to each model badge: test exactly that model.
  box.querySelectorAll('.cloud-test-model').forEach(a => {
    a.onclick = async (e) => {
      e.preventDefault(); e.stopPropagation();
      await runProbe(a.dataset.prov, a.dataset.mid);
    };
  });

  // Per-model ✕ links: remove just that model from its provider.
  box.querySelectorAll('.cloud-del-model').forEach(a => {
    a.onclick = async (e) => {
      e.preventDefault(); e.stopPropagation();
      const prov = a.dataset.prov, mid = a.dataset.mid;
      if (!confirm('Remove model "' + mid + '" from "' + prov + '"?\nAny lane bound to it goes back to local.')) return;
      try {
        const r = await fetch('/control/cloud/model?provider=' + encodeURIComponent(prov)
          + '&model=' + encodeURIComponent(mid), { method: 'DELETE' });
        const j = await r.json().catch(() => ({}));
        if (!r.ok) throw new Error(j.error || ('HTTP ' + r.status));
        toast('Removed model ' + mid);
        await loadCloudCard();
        if (typeof loadProfiles === 'function') loadProfiles();
      } catch (e) { toast('Delete failed: ' + e.message, true); }
    };
  });

  const gotoLanes = g('#cl-goto-lanes');
  if (gotoLanes) gotoLanes.onclick = () => {
    if (typeof switchSettingsTab === 'function') switchSettingsTab('sec-lanes');
    else location.href = '/settings#sec-lanes';
  };
}
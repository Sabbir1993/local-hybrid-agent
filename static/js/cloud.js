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

function renderCloudCard(d) {
  const provs = d.providers || [];
  const models = d.models || [];

  let h = `<div class="rep-bar" style="display:flex; align-items:center; justify-content:space-between; gap:8px;">
    <span><b>${provs.length}</b> provider(s) · <b>${models.length}</b> cloud model(s)</span>
    <button class="btn ghost" id="cloud-add-toggle" style="width:auto; margin:0; padding:4px 10px; font-size:11px; white-space:nowrap;">\uFF0B Add provider</button>
  </div>`;

  if (!provs.length) {
    h += `<div class="cap-item dim">No cloud providers yet — press <b>\uFF0B Add provider</b>. You can also paste a
      <code>provider</code> block into <code>config.json</code> (same shape as other agents) and it will appear here.</div>`;
  } else {
    provs.forEach(p => {
      h += `<div class="cap-group" style="margin-top:8px;">
        <div class="cap-head">
          <span>\u2601 ${esc(p.name)}</span>
          <span style="display:flex; gap:5px; align-items:center;">
            <button class="btn ghost cloud-test" data-prov="${esc(p.provider)}" style="width:auto; margin:0; padding:2px 8px; font-size:10px;">Test</button>
            <button class="btn ghost cloud-add-model" data-prov="${esc(p.provider)}" style="width:auto; margin:0; padding:2px 8px; font-size:10px;" title="Add another model to this same provider">+ model</button>
            <button class="btn ghost cloud-edit" data-prov="${esc(p.provider)}" style="width:auto; margin:0; padding:2px 8px; font-size:10px;">Edit</button>
            <button class="btn ghost cloud-del" data-prov="${esc(p.provider)}" style="width:auto; margin:0; padding:2px 8px; font-size:10px; color:var(--red);">\u2715</button>
          </span>
        </div>
        <div class="cap-body">
          <div class="dim" style="font-size:10.5px; font-family:monospace; word-break:break-all;">${esc(p.base_url || '(no base URL)')}</div>
          <div style="font-size:10.5px; margin-top:3px; display:flex; align-items:center; gap:6px;">
            <span>Key:</span>
            <code class="cloud-key" data-prov="${esc(p.provider)}">${esc(p.key_masked || '(none)')}</code>
            ${p.has_key ? `<button class="btn ghost cloud-reveal" data-prov="${esc(p.provider)}" style="width:auto; margin:0; padding:1px 6px; font-size:10px;" title="Reveal API key">\uD83D\uDC41</button>` : ''}
          </div>
          <div style="display:flex; gap:5px; flex-wrap:wrap; margin-top:5px;">
            ${(p.models || []).map(m => `<span class="badge-main" title="${esc(m.id)}${m.ctx ? ' · ' + m.ctx + ' ctx' : ''}">${esc(m.name)}<a href="#" class="cloud-test-model" data-prov="${esc(p.provider)}" data-mid="${esc(m.id)}" title="Test ${esc(m.id)}" style="margin-left:4px; text-decoration:none; opacity:0.7;">✓</a><a href="#" class="cloud-del-model" data-prov="${esc(p.provider)}" data-mid="${esc(m.id)}" title="Remove ${esc(m.id)}" style="margin-left:3px; text-decoration:none; opacity:0.7; color:var(--red);">✕</a></span>`).join('') || '<span class="dim" style="font-size:10px;">no models yet</span>'}
          </div>
        </div>
      </div>`;
    });
  }

  // Which model does each job (main / helper / image reader, local fallback) is
  // set in Settings -> Models & Jobs (static/js/lanes.js) - one place only.
  h += `<div style="margin-top:12px; border-top:1px solid var(--border); padding-top:10px; display:flex; align-items:center; justify-content:space-between; gap:8px;">
    <span class="dim" style="font-size:11px;">Which model does each job is set in <b>Models &amp; Jobs</b>.</span>
    <button class="btn ghost" id="cl-goto-lanes" style="width:auto; margin:0; padding:4px 12px;">Open Models &amp; Jobs</button>
  </div>
  <div id="cl-result" class="dim" style="font-size:10px; margin-top:6px; min-height:12px;"></div>`;

    // ---- add / edit provider form ----
  h += `<div id="cloud-form" hidden style="margin-top:12px; border-top:1px solid var(--border); padding-top:10px;">
    <div style="display:grid; grid-template-columns:1fr 1fr; gap:8px;">
      <div><label style="font-size:10px; color:var(--dim);">Provider id *</label>
        <input type="text" id="cf-prov" placeholder="open router" style="width:100%;"></div>
      <div><label style="font-size:10px; color:var(--dim);">Display name</label>
        <input type="text" id="cf-name" placeholder="open router" style="width:100%;"></div>
      <div style="grid-column:span 2;"><label style="font-size:10px; color:var(--dim);">Base URL *</label>
        <input type="text" id="cf-url" placeholder="https://openrouter.ai/api/v1" style="width:100%; font-family:monospace;"></div>
      <div style="grid-column:span 2;"><label style="font-size:10px; color:var(--dim);">API key</label>
        <input type="password" id="cf-key" placeholder="sk-or-v1-..." style="width:100%; font-family:monospace;"></div>
      <div style="grid-column:span 2;"><label style="font-size:10px; color:var(--dim);">npm package (whole provider — stored for reference, not used at runtime)</label>
        <input type="text" id="cf-npm" placeholder="@ai-sdk/openai-compatible" style="width:100%; font-family:monospace;"></div>
      <div style="grid-column:span 2; border-top:1px dashed var(--border); padding-top:8px; font-size:10px; color:var(--dim); font-weight:700; text-transform:uppercase; letter-spacing:0.5px;">
        Model(s) for this provider
      </div>
      <div><label style="font-size:10px; color:var(--dim);">Model id(s) *</label>
        <input type="text" id="cf-mid" placeholder="stealth/union-alpha" style="width:100%; font-family:monospace;"></div>
      <div><label style="font-size:10px; color:var(--dim);">Model label(s)</label>
        <input type="text" id="cf-mlabel" placeholder="union-alpha" style="width:100%;"></div>
      <div><label style="font-size:10px; color:var(--dim);">Context (tokens)</label>
        <input type="number" id="cf-mctx" placeholder="262144" style="width:100%;"></div>
      <div style="grid-column:span 2; font-size:10px; color:var(--dim);">
        Add more models to this same provider by separating ids (and labels) with commas,
        e.g. <code>stealth/union-alpha, stealth/vision</code> — existing models are kept, these are merged in.
      </div>
      <div style="grid-column:span 2;">
        <label style="font-size:10px; color:var(--dim);">Extra request headers (optional)</label>
        <input type="text" id="cf-headers" placeholder='HTTP-Referer=https://myapp.com, X-Title=My App' style="width:100%; font-family:monospace; font-size:10.5px;">
        <div style="font-size:8.5px; color:var(--dim); margin-top:2px;">Comma-separated key=value pairs sent with every request. A default HTTP-Referer/X-Title is sent automatically (many OpenAI-compatible gateways reject requests without one) — override it here if needed.</div>
      </div>
    </div>
    <div style="display:flex; justify-content:flex-end; gap:8px; margin-top:9px;">
      <button class="btn ghost" id="cf-cancel" style="width:auto; margin:0; padding:5px 14px;">Cancel</button>
      <button class="btn blue" id="cf-save" style="width:auto; margin:0; padding:5px 16px; font-weight:700;">Save provider</button>
    </div>
  </div>`;

  return h;
}

function wireCloudCard() {
  const box = $('cloud-content');
  if (!box) return;
  const form = box.querySelector('#cloud-form');
  const g = id => box.querySelector(id);

  const showForm = (pre) => {
    form.hidden = false;
    g('#cf-prov').value = (pre && pre.provider) || '';
    g('#cf-name').value = (pre && pre.name) || '';
    g('#cf-url').value = (pre && pre.base_url) || '';
    g('#cf-key').value = '';
    const m0 = (pre && (pre.models || [])[0]) || null;
    g('#cf-mid').value = (pre && pre.models ? pre.models.map(x => x.id).join(', ') : '') || (m0 ? m0.id : '');
    g('#cf-mlabel').value = m0 ? m0.name : '';
    g('#cf-mctx').value = m0 && m0.ctx ? m0.ctx : 262144;   // 256K default for new models
    g('#cf-npm').value = (pre && pre.npm) || '';
    // Reconstruct headers string from the saved provider info
    const savedProv = pre ? (cloudSnap.providers || []).find(x => x.provider === pre.provider) : null;
    const savedHeaders = (savedProv && savedProv.extra_headers) || (pre && pre._opts && pre._opts.extra_headers) || {};
    g('#cf-headers').value = Object.entries(savedHeaders).map(([k, v]) => `${k}=${v}`).join(', ');
    g('#cf-key').placeholder = pre && pre.has_key ? 'leave blank to keep the saved key' : 'sk-or-v1-...';
  };

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
      showForm(p ? { ...p, models: [] } : null);
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
    b.onclick = async () => {
      const code = box.querySelector('.cloud-key[data-prov="' + b.dataset.prov + '"]');
      if (!code) return;
      try {
        const d = await (await fetch('/control/cloud/key?provider=' + encodeURIComponent(b.dataset.prov))).json();
        code.textContent = d.api_key || '(none)';
      } catch (e) { toast('Reveal failed: ' + e.message, true); }
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
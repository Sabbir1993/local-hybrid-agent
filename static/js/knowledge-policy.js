/* Knowledge Base -> "What cloud models may read": categories (per source) and sensitive-content rules
 * (per chunk). Everything defaults to local-only; rules can only hold content back, never release it. */

async function kbPolicyCall(url, method, body) {
  const r = await fetch(url, {
    method, headers: { 'Content-Type': 'application/json' },
    body: body === undefined ? undefined : JSON.stringify(body),
  });
  const j = await r.json().catch(() => ({}));
  if (!r.ok || j.ok === false) throw new Error(j.error || j.detail || ('HTTP ' + r.status));
  return j;
}

function kbPolicyHtml(p) {
  const allowAll = p.cloud_policy === 'allow';
  const cats = (p.categories || []).map(c =>
    '<div class="kbp-row" data-cat="' + esc(c.name) + '" style="display:flex; align-items:center; gap:10px; padding:6px 10px; background:var(--bg-input); border:1px solid var(--border-subtle); border-radius:6px; margin-bottom:4px;">' +
      '<span style="flex:1; min-width:0; overflow:hidden; text-overflow:ellipsis; display:flex; align-items:center; gap:6px;"><b>' + esc(c.name) + '</b> ' +
        '<span class="dim" style="font-size:10.5px;">(' + c.sources + ' source' + (c.sources === 1 ? '' : 's') + ')</span></span>' +
      '<label class="dim" style="font-size:11px; display:flex; align-items:center; gap:5px; cursor:pointer;">' +
        '<input type="checkbox" class="kbp-cat-cloud" data-name="' + esc(c.name) + '" ' + (c.cloud_ok || allowAll ? 'checked' : '') +
        (allowAll ? ' disabled' : '') + ' style="accent-color:var(--accent); margin:0;"> ☁ cloud may read</label>' +
      '<button class="btn ghost kbp-cat-del" data-name="' + esc(c.name) + '" style="width:auto; margin:0; padding:2px 8px; font-size:11px; color:var(--red);">✕</button>' +
    '</div>').join('') || '<div class="dim" style="font-size:11px; padding:6px 0;">No categories defined yet.</div>';

  const rules = (p.rules || []).map(r =>
    '<div class="kbp-row" style="display:flex; align-items:flex-start; gap:10px; padding:6px 10px; background:var(--bg-input); border:1px solid var(--border-subtle); border-radius:6px; margin-bottom:4px;">' +
      '<input type="checkbox" class="kbp-rule-on" data-id="' + r.id + '" ' + (r.enabled ? 'checked' : '') +
        ' title="Keep matching content on local models" style="accent-color:var(--accent); margin:4px 0 0;">' +
      '<span style="flex:1; min-width:0;"><b style="font-size:12px;">' + esc(r.name) + '</b>' + (r.builtin ? ' <span class="cap-pill-badge" style="font-size:9px; background:var(--panel2);">built-in</span>' : '') +
        '<div class="dim" style="font-size:10.5px; word-break:break-all; margin-top:2px; font-family:monospace;">' + esc(r.kind) + ': ' + esc(r.pattern.length > 140 ? r.pattern.slice(0, 140) + '…' : r.pattern) + '</div></span>' +
      (r.builtin ? '' : '<button class="btn ghost kbp-rule-del" data-id="' + r.id + '" style="width:auto; margin:0; padding:2px 8px; font-size:11px; color:var(--red);">✕</button>') +
    '</div>').join('') || '<div class="dim" style="font-size:11px; padding:6px 0;">No sensitive-content rules defined yet.</div>';

  return `
    <div class="cap-pane-card" style="margin-bottom:12px; padding:12px 16px; background:var(--panel2); border-radius:8px; border:1px solid var(--border);">
      <div style="font-weight:600; font-size:14px; color:var(--text); display:flex; align-items:center; gap:8px;">
        <span>☁️ Cloud Model Data Residency &amp; Content Rules</span>
      </div>
      <div class="dim" style="font-size:11.5px; margin-top:4px; line-height:1.5;">${allowAll
        ? 'Policy is set to <b>allow</b> in config/app.json: every source is open to cloud models.'
        : 'Local models can read everything permitted by user roles. Cloud models only read sources in categories marked ☁, minus content held back by rules below.'}</div>
    </div>

    <!-- 1. Categories Card -->
    <div class="cap-item" style="margin-bottom:12px; padding:12px 14px; background:var(--panel2); border-radius:8px; border:1px solid var(--border);">
      <div style="font-size:12px; font-weight:700; margin-bottom:8px; display:flex; align-items:center; justify-content:space-between;">
        <span>📁 1. Categories</span>
        <span class="dim" style="font-weight:400; font-size:11px;">File each knowledge source under a category</span>
      </div>
      <div id="kbp-cats-list" style="margin-bottom:10px;">${cats}</div>
      <div style="display:flex; gap:8px; align-items:center; flex-wrap:wrap;">
        <input type="text" id="kbp-cat-name" placeholder="New category name (e.g. Product Docs)" style="flex:1; min-width:140px; height:32px; box-sizing:border-box; padding:0 8px; font-size:11.5px; background:var(--bg-input); border:1px solid var(--border); border-radius:6px; color:var(--text);">
        <label class="dim" style="font-size:11px; display:flex; align-items:center; gap:5px; cursor:pointer;"><input type="checkbox" id="kbp-cat-new-cloud" style="accent-color:var(--accent); margin:0;"> ☁ cloud may read</label>
        <button class="btn accent" id="kbp-cat-add" style="width:auto; margin:0; padding:4px 12px; font-size:11.5px; height:32px;">+ Add Category</button>
      </div>
    </div>

    <!-- 2. Local Model Rules Card -->
    <div class="cap-item" style="margin-bottom:12px; padding:12px 14px; background:var(--panel2); border-radius:8px; border:1px solid var(--border);">
      <div style="font-size:12px; font-weight:700; margin-bottom:8px; display:flex; align-items:center; justify-content:space-between;">
        <span>🛡️ 2. Keep on Local Models</span>
        <span class="dim" style="font-weight:400; font-size:11px;">Applies even inside ☁ cloud categories</span>
      </div>
      <div id="kbp-rules-list" style="margin-bottom:10px;">${rules}</div>
      <div style="display:flex; gap:8px; flex-wrap:wrap; align-items:center;">
        <input type="text" id="kbp-rule-name" placeholder="Rule name" style="flex:0 0 130px; height:32px; box-sizing:border-box; padding:0 8px; font-size:11.5px; background:var(--bg-input); border:1px solid var(--border); border-radius:6px; color:var(--text);">
        <select id="kbp-rule-kind" style="flex:0 0 100px; height:32px; box-sizing:border-box; padding:0 6px; font-size:11.5px; background:var(--bg-input); border:1px solid var(--border); border-radius:6px; color:var(--text);"><option value="keywords">Keywords</option><option value="regex">Regex</option></select>
        <input type="text" id="kbp-rule-pattern" placeholder="Keywords (comma-separated) or regex pattern" style="flex:1; min-width:160px; height:32px; box-sizing:border-box; padding:0 8px; font-size:11.5px; background:var(--bg-input); border:1px solid var(--border); border-radius:6px; color:var(--text);">
        <button class="btn accent" id="kbp-rule-add" style="width:auto; margin:0; padding:4px 12px; font-size:11.5px; height:32px;">+ Add Rule</button>
      </div>
    </div>

    ${kbWebHtml(p.web)}

    <!-- 4. Test Snippet Against Rules Card -->
    <details class="cap-item" style="padding:10px 14px; background:var(--panel2); border-radius:8px; border:1px solid var(--border);">
      <summary class="dim" style="font-size:11.5px; font-weight:600; cursor:pointer;">🔬 Test a snippet against the redaction rules</summary>
      <textarea id="kbp-test-text" rows="3" placeholder="Paste sample text here. Nothing is stored." style="width:100%; box-sizing:border-box; margin-top:8px; padding:8px; font-size:11.5px; background:var(--bg-input); border:1px solid var(--border); border-radius:6px; color:var(--text);"></textarea>
      <div style="display:flex; gap:10px; align-items:center; margin-top:6px;">
        <button class="btn ghost" id="kbp-test-run" style="width:auto; margin:0; padding:4px 12px; font-size:11px;">Check Snippet</button>
        <span id="kbp-test-out" class="dim" style="font-size:11px;"></span>
      </div>
    </details>`;
}

const KB_WEB_ROWS = [
  ['full', 'KB covers the question', 'Only Deep mode double-checks on the web'],
  ['partial', 'KB covers part of it', 'The web fills the missing part'],
  ['open', 'Nothing in the KB, or you ask for the web', 'Words like search, latest, or a link'],
];

/* "Web next to the knowledge base": offer the web only for what the KB cannot answer; calls per thinking level */
function kbWebHtml(w) {
  if (!w) return '';
  const tiers = w.tiers || ['low', 'medium', 'high', 'deep'];
  const head = '<tr><th></th>' + tiers.map(t => '<th class="dim" style="font-weight:600; padding:0 4px; font-size:11px;">' + esc(t) + '</th>').join('') + '</tr>';
  const rows = KB_WEB_ROWS.map(r => '<tr><td style="padding:3px 10px 3px 0; font-size:11.5px;" title="' + esc(r[2]) + '">' + esc(r[1]) + '</td>' +
    tiers.map(t => '<td><input type="number" min="0" max="100" class="kbp-wb" data-row="' + r[0] + '" data-tier="' + t +
      '" value="' + esc((w.budget[r[0]] || {})[t] ?? 0) + '" style="width:56px; height:28px; box-sizing:border-box; padding:0 6px; font-size:11.5px; background:var(--bg-input); border:1px solid var(--border); border-radius:6px; color:var(--text);"></td>').join('') + '</tr>').join('');
  return '<div class="cap-item" style="margin-bottom:12px; padding:12px 14px; background:var(--panel2); border-radius:8px; border:1px solid var(--border);">' +
    '<div style="font-size:12px; font-weight:700; margin-bottom:6px;"><span>🌐 3. Web next to the knowledge base</span></div>' +
    '<label class="dim" style="font-size:11.5px; display:flex; align-items:flex-start; gap:6px; cursor:pointer; line-height:1.4;">' +
      '<input type="checkbox" id="kbp-gap-fill" ' + (w.gap_fill ? 'checked' : '') + ' style="accent-color:var(--accent); margin:2px 0 0;"> ' +
      '<span>Offer the web only for what the knowledge base cannot answer. This saves tokens; when off, the web is offered only if the question asks for it.</span></label>' +
    '<div class="dim" style="font-size:11px; margin:8px 0 4px;">Web calls allowed per run, by how much the model is set to think (composer effort level; Deep research = deep):</div>' +
    '<table style="border-collapse:collapse;">' + head + rows + '</table>' +
    '<div style="display:flex; gap:10px; align-items:center; margin-top:8px; flex-wrap:wrap;">' +
      '<label class="dim" style="font-size:11px; display:flex; align-items:center; gap:6px;" title="A top match at or above this score counts as fully covered">full-match score ' +
        '<input type="number" id="kbp-full-cos" min="0.5" max="0.95" step="0.01" value="' + esc(w.full_cos) + '" style="width:64px; height:28px; box-sizing:border-box; padding:0 6px; font-size:11.5px; background:var(--bg-input); border:1px solid var(--border); border-radius:6px; color:var(--text);"></label>' +
      '<button class="btn accent" id="kbp-web-save" style="width:auto; margin:0; padding:4px 12px; font-size:11.5px; height:30px;">Save</button></div></div>';
}

/* box: element to fill; p: the /knowledge/policy payload; reload(): re-render the whole panel */
function renderKbPolicy(box, p, reload) {
  if (!box) return;
  box.innerHTML = kbPolicyHtml(p);
  const act = async (fn, okMsg) => {
    try { await fn(); if (okMsg) toast(okMsg); reload(); } catch (e) { toast('Failed: ' + e.message, true); reload(); }
  };
  box.querySelectorAll('.kbp-cat-cloud').forEach(cb => cb.onchange = () => {
    if (cb.checked && !confirm('Cloud model providers will be able to read every source filed under "' + cb.dataset.name + '" (except content a rule below holds back). Continue?')) { cb.checked = false; return; }
    act(() => kbPolicyCall('/knowledge/categories', 'PUT', { name: cb.dataset.name, cloud_ok: cb.checked }), 'Category updated');
  });
  box.querySelectorAll('.kbp-cat-del').forEach(b => b.onclick = () => {
    if (!confirm('Delete category "' + b.dataset.name + '"? Its sources become uncategorised (local-only unless switched on individually).')) return;
    act(() => kbPolicyCall('/knowledge/categories/' + encodeURIComponent(b.dataset.name), 'DELETE'), 'Category deleted');
  });
  $('kbp-cat-add').onclick = () => {
    const name = ($('kbp-cat-name').value || '').trim();
    if (!name) { toast('Give the category a name', true); return; }
    const cloud = $('kbp-cat-new-cloud').checked;
    if (cloud && !confirm('Allow cloud models to read sources in "' + name + '"?')) return;
    act(() => kbPolicyCall('/knowledge/categories', 'PUT', { name, cloud_ok: cloud }), 'Category saved');
  };
  box.querySelectorAll('.kbp-rule-on').forEach(cb => cb.onchange = () =>
    act(() => kbPolicyCall('/knowledge/rules/' + cb.dataset.id, 'PUT', { enabled: cb.checked }), cb.checked ? 'Rule on' : 'Rule off'));
  box.querySelectorAll('.kbp-rule-del').forEach(b => b.onclick = () => {
    if (!confirm('Delete this rule?')) return;
    act(() => kbPolicyCall('/knowledge/rules/' + b.dataset.id, 'DELETE'), 'Rule deleted');
  });
  $('kbp-rule-add').onclick = () => act(() => kbPolicyCall('/knowledge/rules', 'POST', {
    name: $('kbp-rule-name').value, kind: $('kbp-rule-kind').value, pattern: $('kbp-rule-pattern').value,
  }), 'Rule added');
  if ($('kbp-web-save')) $('kbp-web-save').onclick = () => {
    const budget = {};
    box.querySelectorAll('.kbp-wb').forEach(i => { (budget[i.dataset.row] = budget[i.dataset.row] || {})[i.dataset.tier] = parseInt(i.value, 10); });
    act(() => kbPolicyCall('/knowledge/web-policy', 'PUT', {
      gap_fill: $('kbp-gap-fill').checked, full_cos: parseFloat($('kbp-full-cos').value), budget,
    }), 'Saved');
  };
  $('kbp-test-run').onclick = async () => {
    const out = $('kbp-test-out');
    try {
      const j = await kbPolicyCall('/knowledge/rules/test', 'POST', { text: $('kbp-test-text').value });
      out.textContent = j.matches.length ? 'Held back on local models: ' + j.matches.join(', ') : 'No rule matches - cloud models could read this (if its source is cleared).';
    } catch (e) { out.textContent = 'Failed: ' + e.message; }
  };
}

/* the per-source line: category picker, and the old on/off switch only while it has no category */
function kbSourceCloudHtml(s, categories, policy) {
  if (policy === 'allow') return '<div class="dim" style="font-size:11px;">☁ Cloud models may read this (policy: allow)</div>';
  const opts = ['<option value="">(no category)</option>'].concat((categories || []).map(c =>
    '<option value="' + esc(c.name) + '"' + (s.category === c.name ? ' selected' : '') + '>' + esc(c.name) + (c.cloud_ok ? ' - ☁' : ' - local only') + '</option>')).join('');
  const own = !s.category
    ? '<label class="dim kb-cloud-label" style="display:inline-flex; align-items:center; gap:5px; font-size:11px; cursor:pointer; white-space:nowrap; user-select:none; margin:0;" title="Used only while the source has no category">' +
        '<input type="checkbox" class="kb-cloud-ok" data-id="' + s.id + '" ' + (s.cloud_ok ? 'checked' : '') + ' style="accent-color:var(--accent); margin:0; cursor:pointer; width:14px; height:14px;"> ☁ cloud may read</label>'
    : '<span class="dim" style="font-size:11px; white-space:nowrap;">' + (s.cloud_effective ? '☁ cloud may read' : '🔒 local only') + ' (from category)</span>';
  return '<div class="kb-cloud-wrap" style="display:inline-flex; align-items:center; gap:8px; flex-wrap:nowrap;">' +
    '<select class="kb-category" data-id="' + s.id + '">' + opts + '</select>' + own + '</div>';
}

if (typeof module !== 'undefined') module.exports = { kbSourceCloudHtml, kbPolicyHtml, kbWebHtml };

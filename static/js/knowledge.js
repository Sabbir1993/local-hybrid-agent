/* ---------------- organizational knowledge base panel (settings.html only) ---------------- */
/* Card numbers in an uploaded document are masked to their last 4 digits before the text is
   chunked and embedded (server-side, core/pan.py). Say so out loud: a redaction the admin
   cannot see is indistinguishable from a silent one. */
function panNote(j) {
  return j && j.pans_masked ? `, ${j.pans_masked} card number(s) redacted` : '';
}
/* ---- reusable role tag-picker: chips + "+ role" dropdown, backed by an array ---- */
function initTagPicker(container, allRoles, initialSelected) {
  let selected = [...(initialSelected || [])].filter(r => allRoles.includes(r));
  const closeDropdown = () => {
    const dd = container.querySelector('.tagpicker-dropdown');
    if (dd) dd.classList.remove('open');
  };
  const render = () => {
    const remaining = allRoles.filter(r => !selected.includes(r));
    container.innerHTML = `<div class="tagpicker-chips">
      ${selected.map(r => `<span class="tagpicker-chip">${esc(r)}<button type="button" class="tagpicker-x" data-role="${esc(r)}" title="Remove">×</button></span>`).join('')}
      <div class="tagpicker-add-wrap">
        <button type="button" class="tagpicker-add-btn">+ role</button>
        <div class="tagpicker-dropdown">
          ${remaining.length ? remaining.map(r => `<button type="button" class="tagpicker-opt" data-role="${esc(r)}">${esc(r)}</button>`).join('')
                             : '<div class="dim" style="font-size:10.5px; padding:5px 8px;">No more roles</div>'}
        </div>
      </div>
    </div>`;
    container.querySelectorAll('.tagpicker-x').forEach(b => {
      b.onclick = (e) => { e.stopPropagation(); selected = selected.filter(r => r !== b.dataset.role); render(); };
    });
    const addBtn = container.querySelector('.tagpicker-add-btn');
    const dd = container.querySelector('.tagpicker-dropdown');
    addBtn.onclick = (e) => { e.stopPropagation(); dd.classList.toggle('open'); };
    container.querySelectorAll('.tagpicker-opt').forEach(b => {
      b.onclick = (e) => { e.stopPropagation(); selected.push(b.dataset.role); closeDropdown(); render(); };
    });
  };
  render();
  container._getSelected = () => [...selected];
  document.addEventListener('click', (e) => { if (!container.contains(e.target)) closeDropdown(); });
}

let _kbAllRoles = [];

async function loadKnowledgePanel() {
  const box = $('knowledge-content');
  if (!box) return;
  box.innerHTML = '<div class="mon-empty">Loading…</div>';
  try {
    const [sourcesRes, rolesRes, polRes] = await Promise.all([fetch('/knowledge'), fetch('/admin/role_names'), fetch('/knowledge/policy')]);
    if (!sourcesRes.ok) { box.innerHTML = '<div class="mon-empty">You do not have access to the knowledge base.</div>'; return; }
    const kbJson = await sourcesRes.json();
    const sources = kbJson.sources || [];
    _kbCloudPolicy = kbJson.cloud_policy || 'local_only';
    _kbPolicy = polRes.ok ? await polRes.json() : { cloud_policy: _kbCloudPolicy, categories: [], rules: [] };
    _kbAllRoles = rolesRes.ok ? ((await rolesRes.json()).roles || []) : [];
    renderKnowledgePanel(box, sources);
  } catch (e) {
    box.innerHTML = '<div class="mon-empty">Failed to load: ' + esc(e.message) + '</div>';
  }
}

const KB_CHUNK_SIZE = 5 * 1024 * 1024; // 5 MB per chunk (safely bypasses 15MB proxy limits)

async function uploadFileChunked(file, title, onProgress) {
  const totalChunks = Math.max(1, Math.ceil(file.size / KB_CHUNK_SIZE));
  const uploadId = 'up_' + Date.now() + '_' + Math.random().toString(36).substring(2, 9);

  for (let i = 0; i < totalChunks; i++) {
    const start = i * KB_CHUNK_SIZE;
    const end = Math.min(file.size, start + KB_CHUNK_SIZE);
    const chunkBlob = file.slice(start, end);

    if (onProgress) {
      const pct = Math.round((i / totalChunks) * 100);
      const mbUploaded = (start / (1024 * 1024)).toFixed(1);
      const mbTotal = (file.size / (1024 * 1024)).toFixed(1);
      onProgress({
        phase: 'uploading',
        percent: pct,
        statusText: `Uploading: chunk ${i + 1}/${totalChunks} (${mbUploaded} / ${mbTotal} MB · ${pct}%)`
      });
    }

    const fd = new FormData();
    fd.append('upload_id', uploadId);
    fd.append('chunk_index', i);
    fd.append('total_chunks', totalChunks);
    fd.append('file', chunkBlob, file.name);

    const chunkRes = await fetch('/knowledge/upload/chunk', {
      method: 'POST',
      body: fd,
    });
    const chunkJson = await chunkRes.json().catch(() => ({}));
    if (!chunkRes.ok || chunkJson.ok === false) {
      throw new Error(chunkJson.error || `Chunk ${i + 1}/${totalChunks} upload failed`);
    }
  }

  if (onProgress) {
    onProgress({
      phase: 'processing',
      percent: 100,
      statusText: 'Processing & indexing knowledge document... (extracting text & generating embeddings)'
    });
  }

  const completeRes = await fetch('/knowledge/upload/complete', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({
      upload_id: uploadId,
      filename: file.name,
      total_chunks: totalChunks,
      title: title || file.name,
    }),
  });
  const completeJson = await completeRes.json().catch(() => ({}));
  if (!completeRes.ok || completeJson.ok === false) {
    throw new Error(completeJson.error || completeJson.detail || 'Finalizing upload failed');
  }
  return completeJson;
}

let _kbCloudPolicy = 'local_only';
let _kbPolicy = { cloud_policy: 'local_only', categories: [], rules: [] };
let _kbActiveTab = 'sources';

function renderKnowledgePanel(box, sources) {
  const categories = _kbPolicy.categories || [];
  const catOptions = categories.map(c => `<option value="${esc(c.name)}">${esc(c.name)}</option>`).join('');

  box.innerHTML = `
    <div class="kb-redesign-wrap">
      <div role="tablist" class="kb-tabs-bar">
        <button type="button" role="tab" class="kb-tab kb-tab-pill on" data-tab="sources">
          <span>📚 Sources</span>
          <span class="kb-tab-badge">${sources.length}</span>
        </button>
        <button type="button" role="tab" class="kb-tab kb-tab-pill" data-tab="add">
          <span>➕ Add Source</span>
        </button>
        <button type="button" role="tab" class="kb-tab kb-tab-pill" data-tab="policy">
          <span>☁️ Cloud Policy &amp; Rules</span>
        </button>
      </div>

      <!-- PANE 1: Sources -->
      <div class="ru-pane kb-pane" data-pane="sources">
        <div class="kb-filter-bar">
          <input type="text" id="kb-search-input" placeholder="🔍 Search sources by title...">
          <select id="kb-cat-filter">
            <option value="">All Categories</option>
            ${catOptions}
          </select>
          <select id="kb-kind-filter">
            <option value="">All Types</option>
            <option value="file">📄 File</option>
            <option value="url">🔗 URL</option>
            <option value="text">📝 Pasted Text</option>
          </select>
        </div>

        ${sources.length ? `
        <div id="kb-toolbar" class="kb-toolbar">
          <div class="kb-toolbar-left">
            <label class="kb-select-all">
              <input type="checkbox" id="kb-select-all">
              <span>Select all</span>
            </label>
            <span id="kb-selected-count" class="dim" style="font-size:11px;">(0 of ${sources.length} selected)</span>
          </div>
          <div class="kb-toolbar-right">
            <button type="button" class="btn red" id="kb-bulk-delete" disabled style="width:auto; margin:0; padding:5px 12px; font-size:11.5px; opacity:0.5; cursor:not-allowed; display:inline-flex; align-items:center; gap:5px;">
              🗑️ Delete Selected
            </button>
          </div>
        </div>` : ''}

        <div id="kb-list" class="kb-list">
          ${sources.map(s => sourceRow(s)).join('') || '<div class="kb-empty">No knowledge sources yet. Click <b>➕ Add Source</b> to ingest documents.</div>'}
        </div>
      </div>

      <!-- PANE 2: Add Source (Image 3) -->
      <div class="ru-pane kb-pane" data-pane="add" style="display:none;">
        <div class="kb-add-card">
          <div class="kb-card-title">Add New Knowledge Source</div>
          <div class="kb-card-desc">Ingest PDF, Office docs, web pages, or pasted text into the company knowledge base.</div>
          
          <div class="kb-form-row">
            <select id="kb-add-kind" class="kb-kind-select">
              <option value="file">📄 Document File (PDF/DOCX/XLSX)</option>
              <option value="url">🔗 Web URL</option>
              <option value="text">📝 Pasted Text</option>
            </select>
            <input type="text" id="kb-add-title" class="kb-title-input" placeholder="Source Title (e.g. Employee Handbook 2025)">
          </div>

          <div id="kb-add-body" class="kb-body-area"></div>

          <div class="kb-roles-section">
            <div class="kb-roles-label">Roles allowed to query</div>
            <div style="display:flex; gap:6px; align-items:flex-start;">
              <div id="kb-add-roles" class="tagpicker" style="flex:1;"></div>
            </div>
          </div>

          <div id="kb-upload-progress" style="display:none; margin-bottom:14px; padding:10px 14px; background:#0d0f12; border-radius:8px; border:1px solid rgba(255,255,255,0.1);">
            <div style="display:flex; justify-content:space-between; align-items:center; font-size:11.5px; margin-bottom:6px;">
              <span id="kb-progress-status" style="font-weight:600; color:#e5e7eb;">Uploading...</span>
              <span id="kb-progress-pct" class="dim" style="font-family:monospace; font-size:11px;">0%</span>
            </div>
            <div style="width:100%; height:6px; background:rgba(255,255,255,0.08); border-radius:3px; overflow:hidden;">
              <div id="kb-progress-bar" style="width:0%; height:100%; background:#38bdf8; transition:width 0.2s;"></div>
            </div>
          </div>

          <div class="kb-footer-actions">
            <button type="button" class="kb-btn-ingest" id="kb-add-submit">+ Ingest Source</button>
            <span class="kb-chunk-hint">Files uploaded in 5 MB chunks safely bypass server limits.</span>
          </div>
        </div>
      </div>

      <!-- PANE 3: Cloud Policy & Rules -->
      <div class="ru-pane kb-pane" data-pane="policy" style="display:none;">
        <div id="kb-policy"></div>
      </div>
    </div>`;

  renderKbPolicy($('kb-policy'), _kbPolicy, loadKnowledgePanel);

  const _kbShowTab = (tab) => {
    _kbActiveTab = tab;
    box.querySelectorAll('.kb-tab').forEach(b => {
      const on = b.dataset.tab === tab;
      b.classList.toggle('on', on);
      b.setAttribute('aria-selected', on ? 'true' : 'false');
    });
    box.querySelectorAll('.kb-pane').forEach(p => {
      p.style.display = p.dataset.pane === tab ? '' : 'none';
    });
  };
  box.querySelectorAll('.kb-tab').forEach(b => {
    b.onclick = () => _kbShowTab(b.dataset.tab);
  });
  _kbShowTab(_kbActiveTab);

  // Search & filter wiring
  const searchInput = box.querySelector('#kb-search-input');
  const catFilter = box.querySelector('#kb-cat-filter');
  const kindFilter = box.querySelector('#kb-kind-filter');
  const applyFilter = () => {
    const q = (searchInput ? searchInput.value : '').toLowerCase().trim();
    const cat = catFilter ? catFilter.value : '';
    const kind = kindFilter ? kindFilter.value : '';

    box.querySelectorAll('.kb-item').forEach(el => {
      const title = (el.dataset.title || '').toLowerCase();
      const itemCat = el.dataset.category || '';
      const itemKind = el.dataset.kind || '';

      const matchQ = !q || title.includes(q);
      const matchCat = !cat || itemCat === cat;
      const matchKind = !kind || itemKind === kind;

      el.style.display = (matchQ && matchCat && matchKind) ? '' : 'none';
    });
  };
  if (searchInput) searchInput.oninput = applyFilter;
  if (catFilter) catFilter.onchange = applyFilter;
  if (kindFilter) kindFilter.onchange = applyFilter;

  const kindSel = $('kb-add-kind');
  const bodyBox = $('kb-add-body');
  const renderBody = () => {
    if (kindSel.value === 'text') {
      bodyBox.innerHTML = '<textarea id="kb-add-text" rows="4" placeholder="Paste text…" style="width:100%; box-sizing:border-box;"></textarea>';
    } else if (kindSel.value === 'url') {
      bodyBox.innerHTML = '<input type="text" id="kb-add-url" placeholder="https://…" style="width:100%; box-sizing:border-box;">';
    } else {
      bodyBox.innerHTML = `
        <div class="kb-file-custom-row">
          <button type="button" class="kb-file-btn" id="kb-file-trigger">Choose File</button>
          <span class="kb-file-label" id="kb-file-name-label">No file chosen</span>
          <input type="file" id="kb-add-file" accept=".pdf,.docx,.pptx,.xlsx,.xls,.csv" style="display:none;">
        </div>
      `;
      const fileInput = $('kb-add-file');
      const fileBtn = $('kb-file-trigger');
      const fileLabel = $('kb-file-name-label');
      if (fileBtn && fileInput) {
        fileBtn.onclick = () => fileInput.click();
      }
      if (fileInput && fileLabel) {
        fileInput.onchange = () => {
          if (fileInput.files && fileInput.files.length > 0) {
            fileLabel.textContent = fileInput.files[0].name;
            fileLabel.style.color = '#e5e7eb';
          } else {
            fileLabel.textContent = 'No file chosen';
            fileLabel.style.color = '#9ca3af';
          }
        };
      }
    }
  };
  renderBody();
  kindSel.onchange = renderBody;
  initTagPicker($('kb-add-roles'), _kbAllRoles, []);

  const submitBtn = $('kb-add-submit');
  const progressBox = $('kb-upload-progress');
  const progressStatus = $('kb-progress-status');
  const progressPct = $('kb-progress-pct');
  const progressBar = $('kb-progress-bar');

  const updateProgress = ({ percent, statusText }) => {
    if (!progressBox) return;
    progressBox.style.display = 'block';
    if (progressStatus && statusText) progressStatus.textContent = statusText;
    if (progressPct) progressPct.textContent = `${percent}%`;
    if (progressBar) progressBar.style.width = `${percent}%`;
  };

  const hideProgress = () => {
    if (progressBox) progressBox.style.display = 'none';
    if (progressBar) progressBar.style.width = '0%';
    if (progressPct) progressPct.textContent = '0%';
  };

  submitBtn.onclick = async () => {
    const title = $('kb-add-title').value.trim();
    const roles = $('kb-add-roles')._getSelected ? $('kb-add-roles')._getSelected() : [];
    if (!title) { toast('Title required', true); return; }

    submitBtn.disabled = true;
    submitBtn.textContent = 'Processing...';

    try {
      let j = {};
      if (kindSel.value === 'text') {
        const text = $('kb-add-text').value;
        if (!text.trim()) { toast('Paste some text first', true); return; }
        updateProgress({ percent: 100, statusText: 'Processing & indexing knowledge...' });
        const res = await fetch('/knowledge/text', {
          method: 'POST', headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ title, text }),
        });
        j = await res.json().catch(() => ({}));
        if (!res.ok || j.ok === false) throw new Error(j.error || j.detail || ('HTTP ' + res.status));
      } else if (kindSel.value === 'url') {
        const url = $('kb-add-url').value.trim();
        if (!url) { toast('URL required', true); return; }
        updateProgress({ percent: 100, statusText: 'Fetching URL & indexing knowledge...' });
        const res = await fetch('/knowledge/url', {
          method: 'POST', headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ title, url }),
        });
        j = await res.json().catch(() => ({}));
        if (!res.ok || j.ok === false) throw new Error(j.error || j.detail || ('HTTP ' + res.status));
      } else {
        const f = $('kb-add-file').files[0];
        if (!f) { toast('Choose a file first', true); return; }
        j = await uploadFileChunked(f, title, updateProgress);
      }

      const source = j.source;
      if (roles.length && source) {
        await fetch(`/knowledge/${source.id}/access`, {
          method: 'PUT', headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ roles }),
        });
      }
      toast(`'${title}' indexed ✓ (${j.chunks || 0} chunks${panNote(j)})`);
      loadKnowledgePanel();
    } catch (e) {
      toast('Add failed: ' + e.message, true);
    } finally {
      submitBtn.disabled = false;
      submitBtn.textContent = '+ Add';
      hideProgress();
    }
  };

  const bulkBtn = box.querySelector('#kb-bulk-delete');
  const selectAll = box.querySelector('#kb-select-all');
  const selectedCount = box.querySelector('#kb-selected-count');
  const rowCheckboxes = box.querySelectorAll('.kb-row-select');

  const getSelectedIds = () => {
    return Array.from(box.querySelectorAll('.kb-row-select:checked')).map(cb => parseInt(cb.dataset.id, 10));
  };

  const updateBulkState = () => {
    const selected = getSelectedIds();
    const count = selected.length;
    const total = rowCheckboxes.length;
    if (selectedCount) {
      selectedCount.textContent = `(${count} of ${total} selected)`;
    }
    if (selectAll) {
      selectAll.checked = total > 0 && count === total;
      selectAll.indeterminate = count > 0 && count < total;
    }
    if (bulkBtn) {
      bulkBtn.disabled = count === 0;
      bulkBtn.style.opacity = count > 0 ? '1' : '0.5';
      bulkBtn.style.cursor = count > 0 ? 'pointer' : 'not-allowed';
      bulkBtn.innerHTML = count > 0 ? `🗑️ Delete Selected (${count})` : '🗑️ Delete Selected';
    }
  };

  if (selectAll) {
    selectAll.onchange = () => {
      rowCheckboxes.forEach(cb => { cb.checked = selectAll.checked; });
      updateBulkState();
    };
  }

  rowCheckboxes.forEach(cb => {
    cb.onchange = () => updateBulkState();
  });

  if (bulkBtn) {
    bulkBtn.onclick = async () => {
      const ids = getSelectedIds();
      if (!ids.length) return;
      const count = ids.length;
      if (!confirm(`Permanently delete ${count} selected knowledge source(s)? This removes them from the index.`)) return;

      bulkBtn.disabled = true;
      bulkBtn.textContent = 'Deleting...';

      try {
        const res = await fetch('/knowledge/bulk-delete', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ ids }),
        });
        if (res.ok) {
          toast(`Deleted ${count} knowledge source(s) ✓`);
        } else {
          let done = 0;
          await Promise.all(ids.map(async (id) => {
            const r = await fetch(`/knowledge/${id}`, { method: 'DELETE' });
            if (r.ok) done++;
          }));
          toast(`Deleted ${done} knowledge source(s) ✓`);
        }
        loadKnowledgePanel();
      } catch (e) {
        toast('Bulk delete failed: ' + e.message, true);
        loadKnowledgePanel();
      }
    };
  }

  box.querySelectorAll('.kb-delete').forEach(btn => {
    btn.onclick = async () => {
      if (!confirm('Delete this knowledge source? This removes it from the index permanently.')) return;
      try {
        const r = await fetch(`/knowledge/${btn.dataset.id}`, { method: 'DELETE' });
        if (!r.ok) throw new Error('HTTP ' + r.status);
        loadKnowledgePanel();
      } catch (e) { toast('Delete failed: ' + e.message, true); }
    };
  });

  box.querySelectorAll('.kb-reindex').forEach(btn => {
    btn.onclick = async () => {
      try {
        const r = await fetch(`/knowledge/${btn.dataset.id}/reindex`, { method: 'POST' });
        const j = await r.json().catch(() => ({}));
        if (!r.ok || j.ok === false) throw new Error(j.error || ('HTTP ' + r.status));
        toast(`Reindexed ✓ (${j.chunks || 0} chunks${panNote(j)})`);
        loadKnowledgePanel();
      } catch (e) { toast('Reindex failed: ' + e.message, true); }
    };
  });

  box.querySelectorAll('.kb-category').forEach(sel => {
    sel.onchange = async () => {
      try {
        const cat = sel.value || null;
        const c = (_kbPolicy.categories || []).find(x => x.name === cat);
        if (c && c.cloud_ok && !confirm('"' + cat + '" is readable by cloud models. Filing this source under it sends its text (minus content held back by rules) to cloud providers. Continue?')) { loadKnowledgePanel(); return; }
        await kbPolicyCall(`/knowledge/${sel.dataset.id}/category`, 'PUT', { category: cat });
        toast(cat ? `Filed under ${cat}` : 'Category removed');
        loadKnowledgePanel();
      } catch (e) { toast('Update failed: ' + e.message, true); loadKnowledgePanel(); }
    };
  });

  box.querySelectorAll('.kb-cloud-ok').forEach(cb => {
    cb.onchange = async () => {
      const want = cb.checked;
      if (want && !confirm('Allow cloud model providers to read this source? Its text will be sent to them when a chat or agent run uses a cloud model.')) { cb.checked = false; return; }
      try {
        const r = await fetch(`/knowledge/${cb.dataset.id}/cloud`, {
          method: 'PUT', headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ allowed: want }),
        });
        const j = await r.json().catch(() => ({}));
        if (!r.ok || j.ok === false) throw new Error(j.error || j.detail || ('HTTP ' + r.status));
        toast(want ? 'Cloud models may now read this source' : 'This source is local-only again');
      } catch (e) { cb.checked = !want; toast('Update failed: ' + e.message, true); }
    };
  });

  box.querySelectorAll('.kb-roles-picker').forEach(el => {
    let initial = [];
    try { initial = JSON.parse(el.dataset.roles || '[]'); } catch (e) {}
    initTagPicker(el, _kbAllRoles, initial);
  });

  box.querySelectorAll('.kb-roles-save').forEach(btn => {
    btn.onclick = async () => {
      const id = btn.dataset.id;
      const picker = box.querySelector(`.kb-roles-picker[data-id="${id}"]`);
      const roles = picker && picker._getSelected ? picker._getSelected() : [];
      const title = (sources.find(s => String(s.id) === String(id)) || {}).title || `source #${id}`;
      try {
        const r = await fetch(`/knowledge/${id}/access`, {
          method: 'PUT', headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ roles }),
        });
        const j = await r.json().catch(() => ({}));
        if (!r.ok || j.ok === false) throw new Error(j.error || j.detail || ('HTTP ' + r.status));
        // Re-sync the picker's chips from what the server actually persisted, rather than
        // trusting the client's in-memory selection -- makes a row-mismatch click visible
        // immediately instead of silently saving to the wrong source.
        const savedRoles = (j.source && j.source.roles) || [];
        if (picker) {
          picker.dataset.roles = JSON.stringify(savedRoles);
          initTagPicker(picker, _kbAllRoles, savedRoles);
        }
        toast(`Access updated for '${title}' ✓ (${savedRoles.join(', ') || 'all users'})`);
      } catch (e) { toast(`Update failed for '${title}': ` + e.message, true); }
    };
  });
}

function sourceRow(s) {
  const isReady = s.status === 'ready';
  const isErr = s.status === 'error';
  const isProc = s.status === 'processing';
  const statusCls = isReady ? 'ready' : (isErr ? 'err' : (isProc ? 'proc' : 'dim'));
  const kindIco = s.kind === 'file' ? '📄' : (s.kind === 'url' ? '🔗' : '📝');

  return `<div class="kb-item kb-source-card" data-id="${s.id}" data-title="${esc(s.title)}" data-category="${esc(s.category || '')}" data-kind="${esc(s.kind || '')}">
    <div class="kb-item-head">
      <div class="kb-item-title">
        <input type="checkbox" class="kb-row-select" data-id="${s.id}" aria-label="Select ${esc(s.title)}">
        <span class="kb-kind">${kindIco} ${esc(s.kind)}</span>
        <b title="${esc(s.title)}">${esc(s.title)}</b>
        <span class="kb-status-pill ${statusCls}">● ${esc(s.status)}</span>
      </div>
      <div class="kb-item-actions">
        ${s.kind === 'file' ? `<a class="btn ghost" href="/knowledge/${s.id}/file" target="_blank" rel="noopener">👁 View</a>` : ''}
        <button class="btn ghost kb-reindex" data-id="${s.id}">⟳ Reindex</button>
        <button class="btn ghost kb-delete" data-id="${s.id}">🗑️ Delete</button>
      </div>
    </div>
    ${s.error ? `<div class="chat-alert-box error" style="margin:8px 0 0; font-size:11px; padding:6px 10px;">⚠ ${esc(s.error)}</div>` : ''}
    <div class="kb-item-foot">
      <div class="kb-item-foot-left">
        ${kbSourceCloudHtml(s, _kbPolicy.categories, _kbCloudPolicy)}
      </div>
      <div class="kb-item-foot-right">
        <span class="dim" style="font-size:10.5px;">Role access:</span>
        <div class="kb-roles-picker tagpicker" data-id="${s.id}" data-roles="${esc(JSON.stringify(s.roles || []))}"></div>
        <button class="btn ghost kb-roles-save" data-id="${s.id}">Save</button>
      </div>
    </div>
  </div>`;
}

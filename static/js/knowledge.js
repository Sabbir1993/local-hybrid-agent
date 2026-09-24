/* ---------------- organizational knowledge base panel (settings.html only) ---------------- */
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
    const [sourcesRes, rolesRes] = await Promise.all([fetch('/knowledge'), fetch('/admin/role_names')]);
    if (!sourcesRes.ok) { box.innerHTML = '<div class="mon-empty">You do not have access to the knowledge base.</div>'; return; }
    const sources = (await sourcesRes.json()).sources || [];
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

function renderKnowledgePanel(box, sources) {
  box.innerHTML = `
    <div class="cap-item" style="margin-bottom:10px;">
      <b>Add source</b>
      <div style="display:flex; gap:6px; flex-wrap:wrap; margin-top:6px;">
        <select id="kb-add-kind" style="background:var(--panel2); color:var(--text); border:1px solid var(--border); border-radius:5px; padding:5px 8px; font-size:11.5px;">
          <option value="text">📝 Pasted text</option>
          <option value="url">🔗 URL</option>
          <option value="file">📄 File (PDF/DOCX/XLSX)</option>
        </select>
        <input type="text" id="kb-add-title" placeholder="Title" style="flex:1; min-width:120px; background:var(--panel2); color:var(--text); border:1px solid var(--border); border-radius:5px; padding:5px 8px; font-size:11.5px;">
      </div>
      <div id="kb-add-body" style="margin-top:6px;"></div>
      <div style="margin-top:8px;">
        <label class="dim" style="font-size:10.5px; display:block; margin-bottom:4px;">Roles allowed to query</label>
        <div style="display:flex; gap:6px; align-items:flex-start;">
          <div id="kb-add-roles" class="tagpicker" style="flex:1;"></div>
          <button class="btn accent" id="kb-add-submit" style="width:auto; margin:0; padding:5px 12px; font-size:11.5px;">+ Add</button>
        </div>
      </div>
      <div id="kb-upload-progress" style="display:none; margin-top:8px; padding:8px 10px; background:var(--panel2); border-radius:6px; border:1px solid var(--border);">
        <div style="display:flex; justify-content:space-between; align-items:center; font-size:11px; margin-bottom:6px;">
          <span id="kb-progress-status" style="font-weight:500; color:var(--text);">Uploading...</span>
          <span id="kb-progress-pct" class="dim" style="font-family:monospace; font-size:11px;">0%</span>
        </div>
        <div style="width:100%; height:6px; background:var(--border); border-radius:3px; overflow:hidden;">
          <div id="kb-progress-bar" style="width:0%; height:100%; background:var(--accent); transition:width 0.2s;"></div>
        </div>
      </div>
      <div class="cfg-note" style="margin-top:6px;">Large files are uploaded in 5 MB chunks to safely bypass server size limits. Ingestion is not blocked.</div>
    </div>
    <div id="kb-list" style="display:flex; flex-direction:column; gap:4px;">
      ${sources.map(s => sourceRow(s)).join('') || '<div class="dim" style="font-size:11px;">No knowledge sources yet.</div>'}
    </div>`;

  const kindSel = $('kb-add-kind');
  const bodyBox = $('kb-add-body');
  const renderBody = () => {
    if (kindSel.value === 'text') {
      bodyBox.innerHTML = '<textarea id="kb-add-text" rows="4" placeholder="Paste text…" style="width:100%; box-sizing:border-box; background:var(--panel2); color:var(--text); border:1px solid var(--border); border-radius:5px; padding:6px 8px; font-size:11.5px;"></textarea>';
    } else if (kindSel.value === 'url') {
      bodyBox.innerHTML = '<input type="text" id="kb-add-url" placeholder="https://…" style="width:100%; box-sizing:border-box; background:var(--panel2); color:var(--text); border:1px solid var(--border); border-radius:5px; padding:6px 8px; font-size:11.5px;">';
    } else {
      bodyBox.innerHTML = '<input type="file" id="kb-add-file" accept=".pdf,.docx,.pptx,.xlsx,.xls,.csv" style="font-size:11.5px;">';
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
        const res = await fetch('/knowledge/text?' + new URLSearchParams({ title, text }), { method: 'POST' });
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
      toast(`'${title}' indexed ✓ (${j.chunks || 0} chunks)`);
      loadKnowledgePanel();
    } catch (e) {
      toast('Add failed: ' + e.message, true);
    } finally {
      submitBtn.disabled = false;
      submitBtn.textContent = '+ Add';
      hideProgress();
    }
  };

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
        toast(`Reindexed ✓ (${j.chunks || 0} chunks)`);
        loadKnowledgePanel();
      } catch (e) { toast('Reindex failed: ' + e.message, true); }
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
  const statusColor = s.status === 'ready' ? 'var(--green)' : (s.status === 'error' ? 'var(--red)' : (s.status === 'processing' ? 'var(--accent)' : 'var(--dim)'));
  return `<div class="cap-item">
    <div style="display:flex; align-items:center; justify-content:space-between; gap:8px; flex-wrap:wrap;">
      <span><b>${esc(s.title)}</b> <span class="dim">(${esc(s.kind)})</span>
        <span style="color:${statusColor};"> · ${esc(s.status)}</span></span>
      <div style="display:flex; gap:4px;">
        ${s.kind === 'file' ? `<a class="btn ghost" href="/knowledge/${s.id}/file" target="_blank" rel="noopener" style="width:auto; margin:0; padding:2px 8px; font-size:10.5px; text-decoration:none;">👁 View</a>` : ''}
        <button class="btn ghost kb-reindex" data-id="${s.id}" style="width:auto; margin:0; padding:2px 8px; font-size:10.5px;">⟳ Reindex</button>
        <button class="btn ghost kb-delete" data-id="${s.id}" style="width:auto; margin:0; padding:2px 8px; font-size:10.5px; color:var(--red);">Delete</button>
      </div>
    </div>
    ${s.error ? `<div class="dim" style="font-size:10px; color:var(--red); margin-top:3px;">${esc(s.error)}</div>` : ''}
    <div style="display:flex; gap:6px; margin-top:5px; align-items:flex-start;">
      <div class="kb-roles-picker tagpicker" data-id="${s.id}" data-roles="${esc(JSON.stringify(s.roles || []))}" style="flex:1;"></div>
      <button class="btn ghost kb-roles-save" data-id="${s.id}" style="width:auto; margin:0; padding:2px 8px; font-size:10.5px;">Save</button>
    </div>
  </div>`;
}

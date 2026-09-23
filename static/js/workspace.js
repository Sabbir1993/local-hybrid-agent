/* ---------------- agent workspace panel (right side) ---------------- */
let wsPanelOpen = false;
const wsExpandedDirs = new Set();   // remember expanded folders while panel is used

/* per-extension colored file glyphs (Claude Code style) */
function wsFileIcon(name) {
  const e = name.split('.').pop().toLowerCase();
  const map = {
    py: '🐍', js: '⚡', mjs: '⚡', jsx: '⚡', ts: '⚡', tsx: '⚡',
    html: '🌐', htm: '🌐', css: '🎨', scss: '🎨', json: '🧩',
    md: '📝', txt: '📝', sh: '⚙️', ps1: '⚙️', bat: '⚙️',
    php: '🐘', sql: '🗄️', yml: '🔧', yaml: '🔧', toml: '🔧',
    png: '🖼️', jpg: '🖼️', jpeg: '🖼️', webp: '🖼️', svg: '🖼️',
    gguf: '🧠', db: '🗄️',
    // Document types
    xlsx: '📊', xls: '📊', csv: '📋',
    pdf: '📕', pptx: '📊', ppt: '📊',
    docx: '📄', doc: '📄',
  };
  return map[e] || '📄';
}

function fmtSize(bytes) {
  if (bytes == null) return '';
  if (bytes < 1024) return bytes + ' B';
  if (bytes < 1024 * 1024) return (bytes / 1024).toFixed(1) + ' KB';
  return (bytes / (1024 * 1024)).toFixed(2) + ' MB';
}

/* Session Changes pinned group (created/modified this session) */
function renderWsChanges(changes) {
  const box = $('ws-changes');
  if (!box) return;
  const list = $('ws-changes-list');
  const cnt = $('ws-changes-count');
  if (!changes || !changes.length) {
    box.style.display = 'none';
    return;
  }
  box.style.display = 'block';
  if (cnt) cnt.textContent = changes.length;
  if (list) {
    list.innerHTML = changes.map(c => `
      <div class="ws-node ws-chg" data-path="${esc(c.path.toLowerCase())}" title="${esc(c.path)} — ${c.status} this session. Click to open diff.">
        <span class="caret" style="visibility:hidden;">▶</span>
        <span>${c.status === 'created' ? '✚' : '●'}</span>
        <span class="nm" style="${c.status === 'created' ? 'color:var(--green);' : ''}">${esc(c.path)}</span>
      </div>`).join('');
    list.querySelectorAll('.ws-chg').forEach(row => {
      row.onclick = () => wsShowFile(row.dataset.path);
    });
  }
}

function getActiveProject() {
  try {
    if (typeof curProject !== 'undefined' && curProject) return curProject;
    if (window.curProject) return window.curProject;
  } catch (_) {}
  return null;
}

function hasValidActiveProject() {
  const p = getActiveProject();
  return Boolean(
    p &&
    p.id &&
    p.id !== 0 &&
    p.name &&
    p.name !== 'scratch' &&
    p.name !== 'default'
  );
}

async function wsLoadTree(dirPath, targetEl, indent) {
  try {
    if (!hasValidActiveProject()) {
      targetEl.innerHTML = '<div class="ws-empty" style="padding:12px; text-align:center;">Please select a project to view workspace files.</div>';
      return;
    }
    const hdrs = (typeof getDeviceHeaders === 'function') ? getDeviceHeaders() : {};
    const r = await fetch('/agent/ws/tree?path=' + encodeURIComponent(dirPath || ''), { headers: hdrs });
    const d = await r.json().catch(() => ({ error: `Could not load project files (server error ${r.status})` }));
    if (!r.ok || d.error || !d.root) { targetEl.innerHTML = `<div class="ws-empty">${esc(d.error || 'No project workspace active')}</div>`; return; }
    if (!indent) {
      const rp = $('ws-root-path');
      if (rp) rp.textContent = '📁 ' + (d.root || '');
      const tn = $('ws-title-name');
      if (tn) tn.textContent = d.project || 'Workspace';
      if (typeof renderWsChanges === 'function') {
        try { renderWsChanges(d.changes || []); } catch (err) { console.warn(err); }
      }
    }
    if (!d.nodes || !d.nodes.length) {
      targetEl.innerHTML = '<div class="ws-empty" style="padding:6px 0 0 14px;">(empty)</div>';
      return;
    }
    const wrap = document.createElement('div');
    wrap.className = 'ws-children';
    d.nodes.forEach(n => {
      const row = document.createElement('div');
      row.className = 'ws-node' + (n.dir ? ' dir' : '') + (n.changed ? ' changed' : '');
      row.dataset.path = (n.path || '').toLowerCase();
      const sizeStr = (!n.dir && n.size != null) ? fmtSize(n.size) : '';
      row.title = n.path + (sizeStr ? ` · ${sizeStr}` : '') + (n.changed ? ` · ${n.status || 'modified'} this session` : '');
      const caret = n.dir ? '<span class="caret">▶</span>' : '<span class="caret" style="visibility:hidden;">▶</span>';
      const sizeTag = sizeStr ? `<span class="ws-size">${sizeStr}</span>` : '';
      row.innerHTML = `${caret}<span>${n.dir ? '📁' : wsFileIcon(n.name)}</span><span class="nm">${esc(n.name)}</span>${sizeTag}`;
      let kids;
      if (n.dir) {
        kids = document.createElement('div');
        kids.className = 'ws-children';
        kids.style.display = 'none';
        row.onclick = () => {
          const open = kids.style.display === 'none';
          kids.style.display = open ? 'block' : 'none';
          row.classList.toggle('expanded', open);
          if (open) {
            if (!kids.childElementCount) wsLoadTree(n.path, kids, true);
            wsExpandedDirs.add(n.path);
          } else {
            wsExpandedDirs.delete(n.path);
          }
          wsApplyFilter();
        };
      } else {
        row.onclick = () => wsShowFile(n.path);
        row.draggable = true;
        row.addEventListener('dragstart', e => {
          e.dataTransfer.setData('application/x-agent-file', n.path);
          e.dataTransfer.setData('text/plain', '@' + n.path);
          e.dataTransfer.effectAllowed = 'copy';
        });
      }
      wrap.appendChild(row);
      if (n.dir) wrap.appendChild(kids);
      if (n.dir && wsExpandedDirs.has(n.path)) {
        row.click();
      }
    });
    targetEl.appendChild(wrap);
    wsApplyFilter();
  } catch (e) {
    targetEl.innerHTML = `<div class="ws-empty">${esc(e.message)}</div>`;
  }
}


/* client-side filter box */
let wsFilterT;
if ($('ws-filter')) {
  $('ws-filter').oninput = () => {
    clearTimeout(wsFilterT);
    wsFilterT = setTimeout(wsApplyFilter, 120);
  };
}
function wsApplyFilter() {
  const q = ($('ws-filter') && $('ws-filter').value || '').toLowerCase().trim();
  const tree = $('ws-tree');
  if (!tree) return;
  tree.querySelectorAll('.ws-node').forEach(row => {
    if (!q) { row.style.display = ''; return; }
    row.style.display = (row.dataset.path || '').includes(q) ? '' : 'none';
  });
  tree.querySelectorAll('.ws-children').forEach(grp => {
    if (!q) return;
    // keep a group visible if any child row matched
    const any = [...grp.querySelectorAll('.ws-node')].some(r => r.style.display !== 'none');
    const parentRow = grp.previousElementSibling;
    if (parentRow && parentRow.classList.contains('ws-node')) {
      parentRow.style.display = any ? '' : 'none';
    }
    grp.style.display = any ? grp.style.display : 'none';
  });
}

if ($('ws-changes-toggle')) {
  $('ws-changes-toggle').onclick = () => {
    const list = $('ws-changes-list');
    const t = $('ws-changes-toggle');
    if (!list || !t) return;
    const open = list.style.display !== 'none';
    list.style.display = open ? 'none' : 'block';
    const caret = t.querySelector('.caret');
    if (caret) caret.style.transform = open ? '' : 'rotate(90deg)';
  };
}

if ($('ws-refresh')) $('ws-refresh').onclick = () => wsRefreshTree();

let wsCurrentFile = null;   // { path, content } of the file currently shown in the viewer

async function wsShowFile(path) {
  try {
    const hdrs = (typeof getDeviceHeaders === 'function') ? getDeviceHeaders() : {};
    const r = await fetch('/agent/ws/file?path=' + encodeURIComponent(path), { headers: hdrs });
    const d = await r.json().catch(() => ({ error: `server error ${r.status}` }));
    if (!r.ok || d.error) { toast('File view failed: ' + (d.error || r.status), true); return; }
    wsCurrentFile = { path, content: d.content || '' };
    $('ws-tree-view').style.display = 'none';
    const fv = $('ws-file-view');
    fv.style.display = 'flex';
    $('ws-file-path').textContent = path;
    $('ws-file-path').title = path;

    const badge = $('ws-file-badge');
    if (d.changed) {
      badge.textContent = '● ' + (d.status === 'created' ? 'created this session' : 'modified this session');
      badge.className = d.status === 'created' ? 'created' : 'modified';
    } else {
      badge.textContent = 'unchanged';
      badge.className = 'unchanged';
    }

    const pre = $('ws-file-code');
    const lang = hlLangFor(path);
    if (d.changed && d.diff) {
      // highlighted unified diff: green +, red -, dim context
      const marker = { '+': 'wsdiff-add', '-': 'wsdiff-del', ' ': 'wsdiff-ctx' };
      pre.innerHTML = d.diff.map(l => {
        const sign = l.t === ' ' ? '  ' : l.t + ' ';
        return `<span class="${marker[l.t] || 'wsdiff-ctx'}">${sign}${hlCode(l.s, lang)}</span>`;
      }).join('');
    } else {
      pre.innerHTML = hlCode(d.content || '(empty file)', lang);
    }
    pre.scrollLeft = 0; pre.scrollTop = 0;
  } catch (e) {
    toast('File view failed: ' + e.message, true);
  }
}

/* shift+wheel -> horizontal scroll in file viewer */
(function initWsHScroll() {
  const pre = $('ws-file-code');
  if (!pre) return;
  pre.addEventListener('wheel', e => {
    if (e.shiftKey && e.deltaY) {
      e.preventDefault();
      pre.scrollLeft += e.deltaY;
    }
  }, { passive: false });
})();

/* drag a selected code snippet out of the file viewer into the chat composer */
(function initWsCodeDrag() {
  const pre = $('ws-file-code');
  if (!pre) return;
  pre.addEventListener('dragstart', e => {
    const sel = window.getSelection();
    const text = sel ? sel.toString() : '';
    if (!wsCurrentFile || !text || sel.isCollapsed) { e.preventDefault(); return; }
    const range = sel.getRangeAt(0);

    const lineOf = (node, offset) => {
      const r = document.createRange();
      r.selectNodeContents(pre);
      r.setEnd(node, offset);
      return (r.toString().match(/\n/g) || []).length + 1;
    };
    const startLine = lineOf(range.startContainer, range.startOffset);
    const endLine = lineOf(range.endContainer, range.endOffset);

    e.dataTransfer.setData('application/x-agent-code', JSON.stringify({
      path: wsCurrentFile.path, text, startLine, endLine,
    }));
    e.dataTransfer.setData('text/plain', text);
    e.dataTransfer.effectAllowed = 'copy';
  });
})();

function wsShowTree() {
  $('ws-file-view').style.display = 'none';
  $('ws-tree-view').style.display = 'block';
}

function wsRefreshTree() {
  const tree = $('ws-tree');
  if (!tree) return;
  tree.innerHTML = '';
  wsExpandedDirs.clear();
  wsLoadTree('', tree, false);
  // if a file is open, refresh its diff too
  if ($('ws-file-view').style.display !== 'none') {
    const p = $('ws-file-path').textContent;
    if (p) wsShowFile(p);
  }
}

function updateWsRail() {
  const rail = $('ws-rail');
  if (!rail) return;
  // workspace panel only makes sense with a real, active user project workspace
  const hasValidProject = hasValidActiveProject();
  rail.style.display = (agentMode && hasValidProject && !wsPanelOpen) ? 'block' : 'none';
  if ((!agentMode || !hasValidProject) && wsPanelOpen) {
    setWsPanel(false);
  }
  if (typeof updateGitIconVisibility === 'function') updateGitIconVisibility();
}

function setWsPanel(open) {
  const hasValidProject = hasValidActiveProject();
  if (open && (!agentMode || !hasValidProject)) {
    wsPanelOpen = false;
    const panel = $('ws-panel');
    if (panel) panel.classList.remove('open');
    updateWsRail();
    return;
  }
  wsPanelOpen = open;
  const panel = $('ws-panel');
  if (panel) {
    panel.style.right = '';
    panel.classList.toggle('open', open);
  }
  updateWsRail();
  if (open) wsRefreshTree();
}

$('ws-rail').onclick = () => setWsPanel(true);
$('ws-close').onclick = () => setWsPanel(false);
$('ws-back').onclick = wsShowTree;
window.addEventListener('keydown', e => {
  if (e.key === 'Escape' && wsPanelOpen) setWsPanel(false);
});

// Run on script load to guarantee rail is hidden if no project
updateWsRail();
document.addEventListener('DOMContentLoaded', updateWsRail);

/* ---------------- shell permission modal ---------------- */
function showPermModal(reqId, cmd) {
  const m = $('perm-modal');
  if (!m) return;
  m.dataset.reqId = reqId;
  $('perm-cmd').textContent = cmd;
  // suggest the leading command word as an allow pattern, * for everything
  const firstWord = (cmd.trim().split(/\s+/)[0] || '*') + ' *';
  const p = getActiveProject();
  const projName = p ? (p.name || 'this project') : 'active project';
  $('perm-note').innerHTML = `Permission scopes for <code>${esc(firstWord)}</code>:<br>` +
    `• <b>Just once:</b> Run this command now without saving.<br>` +
    `• <b>For this project:</b> Automatically permit in <i>${esc(projName)}</i>.<br>` +
    `• <b>Always allow for me:</b> Permit for your account, across all your projects.<br>` +
    `• <b>Always:</b> Allow globally for every user (admin-visible setting).`;
  m.dataset.pattern = firstWord;
  m.hidden = false;
}

async function answerPermission(decision) {
  const m = $('perm-modal');
  const reqId = m.dataset.reqId;
  m.hidden = true;
  if (!reqId) return;
  const pattern = (decision === 'always' || decision === 'project' || decision === 'user') ? m.dataset.pattern : null;
  const p = getActiveProject();
  const projectId = (p && p.id) ? p.id : null;
  try {
    await fetch('/agent/permission', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        req_id: reqId,
        decision,
        pattern,
        project_id: projectId
      }),
    });
  } catch (e) { /* stream may have ended */ }

  if (decision === 'always') {
    toast(`✓ Pattern "${m.dataset.pattern}" allow-listed globally`);
  } else if (decision === 'project') {
    const pName = p ? p.name : 'project';
    toast(`✓ Pattern "${m.dataset.pattern}" allowed for ${pName}`);
  } else if (decision === 'user') {
    toast(`✓ Pattern "${m.dataset.pattern}" always allowed for your account`);
  } else if (decision === 'allow') {
    toast('▶ Shell command approved once');
  } else if (decision === 'deny') {
    toast('✕ Shell command denied');
  }
}

if ($('perm-allow')) $('perm-allow').onclick = () => answerPermission('allow');
if ($('perm-project')) $('perm-project').onclick = () => answerPermission('project');
if ($('perm-user')) $('perm-user').onclick = () => answerPermission('user');
if ($('perm-always')) $('perm-always').onclick = () => answerPermission('always');
if ($('perm-deny')) $('perm-deny').onclick = () => answerPermission('deny');
if ($('perm-close-x')) $('perm-close-x').onclick = () => answerPermission('deny');

/* drag the left-edge grip to resize the workspace panel width */
(function initWsGrip() {
  const grip = $('ws-grip');
  const panel = $('ws-panel');
  if (!grip || !panel) return;

  // restore persisted width
  try {
    const saved = parseInt(localStorage.getItem('ws_panel_w'), 10);
    if (saved >= 220 && saved <= window.innerWidth * 0.95) {
      panel.style.width = saved + 'px';
      panel.style.maxWidth = 'none';
    }
  } catch (e) {}

  let isDragging = false;
  let activePointerId = null;

  function applyWidth(clientX) {
    const vw = window.innerWidth;
    const w = Math.min(Math.max(220, vw - clientX), Math.floor(vw * 0.92));
    panel.style.width = w + 'px';
    panel.style.maxWidth = 'none';
  }

  function onMove(e) {
    if (!isDragging) return;
    applyWidth(e.clientX);
  }

  function endDrag(e) {
    if (!isDragging) return;
    isDragging = false;
    grip.classList.remove('dragging');
    document.body.classList.remove('ws-resizing');
    panel.style.transition = '';

    if (activePointerId != null) {
      try {
        if (grip.hasPointerCapture && grip.hasPointerCapture(activePointerId)) {
          grip.releasePointerCapture(activePointerId);
        }
      } catch (_) {}
      activePointerId = null;
    }

    window.removeEventListener('pointermove', onMove);
    window.removeEventListener('pointerup', endDrag);
    window.removeEventListener('pointercancel', endDrag);
    window.removeEventListener('mousemove', onMove);
    window.removeEventListener('mouseup', endDrag);

    try {
      const finalW = parseInt(panel.style.width, 10);
      if (finalW >= 220) {
        localStorage.setItem('ws_panel_w', finalW);
      }
    } catch (e) {}
  }

  function startDrag(e) {
    if (e.button != null && e.button !== 0) return;
    e.preventDefault();
    isDragging = true;
    activePointerId = e.pointerId ?? null;

    grip.classList.add('dragging');
    document.body.classList.add('ws-resizing');
    panel.style.transition = 'none';

    if (activePointerId != null) {
      try { grip.setPointerCapture(activePointerId); } catch (_) {}
    }

    applyWidth(e.clientX);

    window.addEventListener('pointermove', onMove, { passive: true });
    window.addEventListener('pointerup', endDrag);
    window.addEventListener('pointercancel', endDrag);
    window.addEventListener('mousemove', onMove, { passive: true });
    window.addEventListener('mouseup', endDrag);
  }

  grip.addEventListener('pointerdown', startDrag);
  grip.addEventListener('mousedown', startDrag);
  grip.addEventListener('dragstart', e => e.preventDefault());
  grip.addEventListener('dblclick', () => {
    panel.style.width = '360px';
    try { localStorage.setItem('ws_panel_w', 360); } catch (e) {}
  });
})();

/* Plan/Build dropdown selector (default Build) */
(function initPlanSeg() {
  const planSel = $('agent-plan-sel');
  if (!planSel) return;
  planMode = localStorage.getItem('agent_plan') === 'plan';
  planSel.value = planMode ? 'plan' : 'build';
  function apply(silent) {
    planSel.value = planMode ? 'plan' : 'build';
    const input = $('input');
    if (input && agentMode) {
      input.placeholder = planMode
        ? 'Plan mode — agent explores read-only and proposes a plan…'
        : 'Describe a coding task (e.g. "Find and fix bug in main.py")…';
    }
    if (!silent) toast(planMode ? '📋 Plan — agent proposes, nothing is written' : '🔨 Build — agent executes changes');
  }
  planSel.onchange = () => {
    planMode = planSel.value === 'plan';
    try { localStorage.setItem('agent_plan', planMode ? 'plan' : 'build'); } catch (e) {}
    apply();
  };
  apply(true);
  window._setPlanMode = (on) => {
    planMode = on;
    try { localStorage.setItem('agent_plan', on ? 'plan' : 'build'); } catch (e) {}
    apply(true);
  };
})();

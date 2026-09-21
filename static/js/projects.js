/* ---------------- projects & sessions ---------------- */
let curProject = null;      // {id, name}
let curSession = null;      // {id, title}
try {
  Object.defineProperty(window, 'curProject', {
    get() { return curProject; },
    set(v) { curProject = v; },
    configurable: true
  });
  Object.defineProperty(window, 'curSession', {
    get() { return curSession; },
    set(v) { curSession = v; },
    configurable: true
  });
} catch (_) {}

/* ---------------- client device helper ---------------- */
function getClientDeviceId() {
  let devId = localStorage.getItem('antigravity_device_id');
  if (!devId) {
    devId = 'dev_' + Math.random().toString(36).substring(2, 10) + '_' + Date.now().toString(36);
    try { localStorage.setItem('antigravity_device_id', devId); } catch (_) {}
  }
  return devId;
}

function getClientDeviceName() {
  let devName = localStorage.getItem('antigravity_device_name');
  if (!devName) {
    const ua = navigator.userAgent || '';
    if (ua.includes('Windows')) devName = 'Windows PC';
    else if (ua.includes('Macintosh') || ua.includes('Mac OS')) devName = 'Mac';
    else if (ua.includes('Linux')) devName = 'Linux PC';
    else if (ua.includes('Android')) devName = 'Android';
    else if (ua.includes('iPhone') || ua.includes('iPad')) devName = 'iOS Device';
    else devName = 'My Device';
    try { localStorage.setItem('antigravity_device_name', devName); } catch (_) {}
  }
  return devName;
}

function getClientDeviceShortId() {
  // Returns a short readable suffix from the device ID (e.g. "ABC12DEF3456")
  const devId = getClientDeviceId();
  return devId.replace(/^dev_/, '').replace(/_/g, '').slice(0, 12).toUpperCase();
}

function getDeviceDisplayLabel() {
  // Returns "Windows PC-ABC12DEF3456" style label
  return `${getClientDeviceName()}-${getClientDeviceShortId()}`;
}

function setClientDeviceName(name) {
  if (!name || !name.trim()) return;
  try {
    localStorage.setItem('antigravity_device_name', name.trim());
  } catch (_) {}
  updateDeviceUI();
  loadProjects();
}

function getDeviceHeaders() {
  return {
    'X-Device-Id': getClientDeviceId(),
    'X-Device-Name': getClientDeviceName()
  };
}

function updateDeviceUI() {
  const label = $('device-label-text');
  if (label) {
    label.textContent = `💻 ${getDeviceDisplayLabel()}`;
    label.title = `Current device: ${getClientDeviceName()} (ID: ${getClientDeviceId()})`;
  }
}

async function renameClientDevice() {
  const cur = getClientDeviceName();
  // Prompt user to update only the name part (device ID suffix stays fixed)
  const next = prompt(`Rename this device:\n(Device ID suffix will remain: -${getClientDeviceShortId()})`, cur);
  if (!next || !next.trim() || next.trim() === cur) return;

  const newName = next.trim();
  try {
    localStorage.setItem('antigravity_device_name', newName);
  } catch (_) {}

  updateDeviceUI();

  try {
    const resp = await fetch('/control/device/rename', {
      method: 'POST',
      headers: {
        'Content-Type': 'application/json',
        ...getDeviceHeaders(),
      },
      body: JSON.stringify({
        old_name: cur,
        new_name: newName,
        device_id: getClientDeviceId(),
      }),
    });
    const data = await resp.json();
    if (!resp.ok || !data.ok) {
      console.warn('Backend rename failed:', data.error || resp.status);
    }
  } catch (e) {
    console.warn('Backend rename failed:', e);
  }

  toast(`Device renamed to "${newName}-${getClientDeviceShortId()}"`);
  await loadProjects();
}

// Wire up device rename handlers
document.addEventListener('DOMContentLoaded', () => {
  setupDeviceUIHandlers();
});
setTimeout(setupDeviceUIHandlers, 50);

function setupDeviceUIHandlers() {
  const btnRenameDev = $('btn-rename-device');
  if (btnRenameDev && !btnRenameDev._bound) {
    btnRenameDev._bound = true;
    btnRenameDev.onclick = renameClientDevice;
  }
  const label = $('device-label-text');
  if (label && !label._bound) {
    label._bound = true;
    label.onclick = renameClientDevice;
    label.style.cursor = 'pointer';
  }
}

async function promptFixProjectPath(pid, pname, oldPath) {
  const currentDev = getClientDeviceName();
  const newPath = prompt(
    `Update workspace directory for project "${pname}" on this device (${currentDev}):\n(Current path: ${oldPath})`,
    oldPath
  );
  if (!newPath || !newPath.trim() || newPath.trim() === oldPath) return;

  try {
    const r = await fetch(`/control/projects/${pid}/workspace`, {
      method: 'PATCH',
      headers: {
        'Content-Type': 'application/json',
        ...getDeviceHeaders(),
      },
      body: JSON.stringify({
        workspace_dir: newPath.trim(),
        device_id: getClientDeviceId(),
        device_name: currentDev,
      }),
    });
    const d = await r.json();
    if (!r.ok || !d.ok) throw new Error(d.error || ('HTTP ' + r.status));
    toast(`Workspace path updated for ${currentDev}`);
    await loadProjects();
  } catch (e) {
    toast('Failed to update workspace path: ' + e.message, true);
  }
}

async function loadProjects(autoRestoreSessions = false) {
  const list = $('projects-list');
  updateDeviceUI();
  try {
    const q = '?device=current';
    const d = await (await fetch('/control/projects' + q, {
      headers: { ...getDeviceHeaders() }
    })).json();
    const savedProjId = localStorage.getItem('active_project_id');
    const savedProjName = localStorage.getItem('active_project_name');

    if (d.active && d.active !== 'scratch' && d.active !== 'default') {
      const p = (d.projects || []).find(x => x.name === d.active);
      if (p) curProject = p;
    } else if (savedProjId || savedProjName) {
      const p = (d.projects || []).find(x => String(x.id) === String(savedProjId) || (savedProjName && x.name === savedProjName));
      if (p) {
        curProject = p;
        // Keep backend active project state in sync
        fetch(`/control/projects/${p.id}/activate`, {
          method: 'POST',
          headers: { ...getDeviceHeaders() },
        }).catch(() => {});
      } else {
        curProject = null;
      }
    } else {
      curProject = null;
    }

    if (curProject) {
      try {
        localStorage.setItem('active_project_id', String(curProject.id));
        localStorage.setItem('active_project_name', curProject.name);
      } catch (_) {}
    }

    if (list) {
      list.innerHTML = '';
      const userProjects = (d.projects || []).filter(p => p.id !== 0 && p.name !== 'scratch' && p.name !== 'default');

      if (!userProjects.length) {
        const emptyMsg = `No projects on this device (${getClientDeviceName()})`;
        list.innerHTML = `
          <div class="project-empty-state">
            <span style="font-size:11px; color:var(--dim);">${emptyMsg}</span>
            <button class="btn ghost" style="font-size:10.5px; padding:3px 8px; margin-top:4px;" onclick="$('btn-newproject').click()">+ Create Project</button>
          </div>`;
      } else {
        userProjects.forEach(p => {
          const ws = p.workspace_dir || `(server workspace)/${p.name}`;
          const isCur = curProject && curProject.id === p.id;
          const isMyDev = !!p.is_current_device;
          const pathValid = p.path_valid_on_device !== false;
          const devLabel = p.device_name || (p.device_id ? (p.device_id === getClientDeviceId() ? getClientDeviceName() : p.device_id.slice(0, 8)) : 'Default');

          const row = document.createElement('div');
          row.className = 'project-row' + (isCur ? ' cur' : '') + (!pathValid ? ' path-invalid' : '');
          row.title = `Project: ${p.name}\nDirectory: ${ws}\nDevice: ${devLabel}${!pathValid ? '\n[Warning: Path does not exist on this machine]' : ''}`;
          row.innerHTML = `
            <div class="proj-icon-wrapper">
              <svg class="proj-dir-icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round">
                <path d="M22 19a2 2 0 0 1-2 2H4a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h5l2 3h9a2 2 0 0 1 2 2z"></path>
              </svg>
            </div>
            <div class="proj-meta">
              <div style="display:flex; align-items:center; justify-content:space-between; gap:4px;">
                <div class="proj-name">${esc(p.name)}</div>
                <span class="proj-dev-badge ${isMyDev ? 'this-dev' : 'other-dev'}" title="Device: ${esc(devLabel)}">${esc(devLabel)}</span>
              </div>
              <div class="proj-path" title="${esc(ws)}">${esc(ws)}</div>
              ${!pathValid ? `
                <div class="proj-path-warn">
                  <span>⚠️ Path not found</span>
                  <button type="button" class="btn-fix-path" data-pid="${p.id}" data-pname="${esc(p.name)}" data-curpath="${esc(ws)}">Map Path</button>
                </div>` : ''}
            </div>
            <span class="p-del" title="Remove project registration (files preserved)">✕</span>
          `;
          row.onclick = e => {
            if (e.target.classList.contains('p-del')) {
              e.stopPropagation();
              deleteProject(p.id, p.name);
              return;
            }
            if (e.target.classList.contains('btn-fix-path')) {
              e.stopPropagation();
              promptFixProjectPath(p.id, p.name, ws);
              return;
            }
            activateProject(p.id);
          };
          list.appendChild(row);
        });
      }
    }

    // Populate registered projects in manage modal if open
    const mList = $('proj-manage-list');
    if (mList) {
      if (!d.projects || d.projects.length === 0) {
        mList.innerHTML = '<div style="color:var(--dim); font-size:11px; padding:6px; text-align:center;">No projects registered.</div>';
      } else {
        mList.innerHTML = '';
        d.projects.forEach(p => {
          const ws = p.workspace_dir || `(server workspace)/${p.name}`;
          const isMyDev = !!p.is_current_device;
          const devLabel = p.device_name || (p.device_id ? (p.device_id === getClientDeviceId() ? getClientDeviceName() : p.device_id.slice(0, 8)) : 'Default');
          const item = document.createElement('div');
          item.style.cssText = 'display:flex; align-items:center; justify-content:space-between; background:var(--bg); border:1px solid var(--border); border-radius:6px; padding:5px 8px; font-size:11.5px;';
          item.innerHTML = `
            <div style="min-width:0; flex:1; margin-right:8px;">
              <div style="display:flex; align-items:center; gap:6px;">
                <div style="font-weight:600; color:var(--text); overflow:hidden; text-overflow:ellipsis; white-space:nowrap;">📁 ${esc(p.name)}</div>
                <span class="proj-dev-badge ${isMyDev ? 'this-dev' : 'other-dev'}">${esc(devLabel)}</span>
              </div>
              <div style="font-size:9.5px; color:var(--dim); font-family:monospace; overflow:hidden; text-overflow:ellipsis; white-space:nowrap;" title="${esc(ws)}">${esc(ws)}</div>
            </div>
            <button class="btn ghost btn-del-proj-row" style="width:auto; margin:0; padding:2px 7px; font-size:10px; color:var(--red); border-color:rgba(239,68,68,0.3);" title="Remove project from agent (files on disk are NOT deleted)">🗑️ Remove</button>
          `;
          item.querySelector('.btn-del-proj-row').onclick = () => deleteProject(p.id, p.name);
          mList.appendChild(item);
        });
      }
    }

    updateWsRail();
    loadSessions(autoRestoreSessions);
  } catch (e) {}
}

async function deleteProject(pid, pname) {
  if (!confirm(`Remove project "${pname}" from agent?\n\nNOTE: Only the project registration and sessions are removed from the agent.\nWorkspace files on your disk will NOT be touched or deleted.`)) {
    return;
  }
  try {
    const r = await fetch(`/control/projects/${pid}`, {
      method: 'DELETE',
      headers: { ...getDeviceHeaders() }
    });
    const d = await r.json();
    if (!r.ok || !d.ok) throw new Error(d.error || ('HTTP ' + r.status));
    toast(`Project "${pname}" removed from agent (disk files preserved)`);
    if (curProject && curProject.id === pid) {
      curProject = null;
      curSession = null;
      try {
        localStorage.removeItem('active_project_id');
        localStorage.removeItem('active_project_name');
      } catch (_) {}
      messages = [];
      renderAll();
    }
    await loadProjects();
  } catch (e) {
    toast('Delete project failed: ' + e.message, true);
  }
}

async function loadSessions(autoRestore = false) {
  const list = $('session-list');
  const url = agentMode
    ? (curProject ? `/control/projects/${curProject.id}/sessions` : null)
    : `/control/projects/0/sessions`;

  if (agentMode && !curProject) {
    list.innerHTML = '<div class="dim" style="font-size:11.5px; padding:8px 4px;">No project selected.<br>Pick or create a project above for agent tasks.</div>';
    return;
  }
  try {
    const d = await (await fetch(url, { headers: { ...getDeviceHeaders() } })).json();
    if (!d.sessions || !d.sessions.length) {
      const emptyMsg = agentMode
        ? 'No task sessions in this project.<br>Type a prompt to start an agent task.'
        : 'No chats yet.<br>Type a message to start chatting.';
      list.innerHTML = `<div class="dim" style="font-size:11.5px; padding:8px 4px;">${emptyMsg}</div>`;
      return;
    }
    list.innerHTML = '';
    d.sessions.forEach(s => {
      const row = document.createElement('div');
      row.className = 'session-row' + (curSession && curSession.id === s.id ? ' cur' : '');
      row.setAttribute('data-sid', String(s.id));
      const icon = agentMode ? '🤖' : '💬';
      const isRunning = window.bgJobs && window.bgJobs.has(String(s.id));
      const bgBadge = isRunning ? '<span class="bg-session-indicator" title="Executing in background..."><span class="bg-pulse-dot"></span>⚡</span>' : '';
      row.innerHTML = `<span style="flex:1; overflow:hidden; text-overflow:ellipsis; white-space:nowrap;" title="${esc(s.title)}">${icon} ${esc(s.title)}</span>` +
        bgBadge +
        `<span class="s-menu-wrap"><span class="s-menu" title="Session options">⋮</span></span>`;
      row.onclick = e => {
        if (e.target.classList.contains('s-menu')) {
          e.stopPropagation();
          toggleSessionMenu(e.target, s);
          return;
        }
        openSession(s);
      };
      list.appendChild(row);
    });

    if (typeof updateBgIndicators === 'function') updateBgIndicators();

    // Auto-restore active session on initial load / refresh if saved
    if (autoRestore && !curSession && d.sessions && d.sessions.length) {
      const savedSid = localStorage.getItem(agentMode ? 'active_agent_session_id' : 'active_chat_session_id');
      const target = savedSid ? d.sessions.find(x => String(x.id) === String(savedSid)) : null;
      if (target) {
        openSession(target);
      }
    }
  } catch (e) {}
}

async function openSession(s) {
  try {
    if (curSession && String(curSession.id) === String(s.id)) return;

    // If target session is currently running in background, attach directly to live job state
    if (window.bgJobs && window.bgJobs.has(String(s.id))) {
      const job = window.bgJobs.get(String(s.id));
      curSession = s;
      messages = job.messages;
      try {
        localStorage.setItem(agentMode ? 'active_agent_session_id' : 'active_chat_session_id', String(s.id));
      } catch (e) {}
      renderAll();
      setGenUI(true);
      loadSessions();
      if (typeof updateBgIndicators === 'function') updateBgIndicators();
      return;
    }

    // Fetch messages and config in parallel so curCtxMax is ready before rendering.
    const sel = $('profile');
    const modelTarget = (sel && sel.value) ? sel.value : (curStatus && curStatus.model);
    const cfgUrl = '/control/config' + (modelTarget ? '?model=' + encodeURIComponent(modelTarget) : '');
    const [msgRes, cfgRes] = await Promise.allSettled([
      fetch(`/control/sessions/${s.id}/messages`).then(r => r.json()),
      fetch(cfgUrl).then(r => r.json()).catch(() => null),
    ]);

    const d = (msgRes.status === 'fulfilled' ? msgRes.value : null) || {};
    const cfg = cfgRes.status === 'fulfilled' ? cfgRes.value : null;

    // Immediately seed curCtxMax from the config response (no need to wait for loadProfiles)
    if (cfg && cfg.context_size > 0) curCtxMax = cfg.context_size;

    messages = (d.messages || []).map(m => {
      const meta = m.meta || {};
      const textLen = (m.content || '').length + ((meta.reasoning) || '').length;
      const tok = (typeof meta.ntok === 'number') ? meta.ntok : Math.max(1, Math.round(textLen / 3.5));
      return {
        role: m.role,
        content: m.content || '',
        displayContent: meta.displayContent || undefined,
        images: meta.images || undefined,
        files: meta.files || undefined,
        reasoning: meta.reasoning || '',
        acts: meta.acts || [],
        tps: meta.tps,
        ntok: tok,
        secs: meta.secs,
        compact: meta.compact || undefined,
        compactBefore: meta.before_tokens,
        compactAfter: meta.after_tokens,
        reductionPct: meta.reduction_pct,
        compactKept: meta.kept_messages,
        modelDisplay: meta.modelDisplay || undefined,
        modelSource: meta.modelSource || undefined,
        modelProvider: meta.modelProvider || undefined,
      };
    });
    curSession = s;
    try {
      localStorage.setItem(agentMode ? 'active_agent_session_id' : 'active_chat_session_id', String(s.id));
    } catch (e) {}
    setGenUI(false);
    renderAll();
    loadSessions();
    if (typeof updateBgIndicators === 'function') updateBgIndicators();
  } catch (e) { toast('Failed to load session', true); }
}

async function openSessionById(sid) {
  const sidStr = String(sid);
  // Check if session exists in current list
  const row = document.querySelector(`#session-list .session-row[data-sid="${sidStr}"]`);
  if (row) {
    row.click();
    return;
  }
  // Otherwise fetch session details
  try {
    const s = { id: sid, title: 'Session ' + sid };
    await openSession(s);
  } catch (e) {
    toast('Could not switch to session ' + sid, true);
  }
}
window.openSessionById = openSessionById;

function closeSessionMenu() {
  const open = document.querySelector('.s-menu-pop');
  if (open) open.remove();
  document.querySelectorAll('.s-menu.open').forEach(m => m.classList.remove('open'));
}
document.addEventListener('click', closeSessionMenu);

function toggleSessionMenu(btn, s) {
  const already = btn.classList.contains('open');
  closeSessionMenu();
  if (already) return;
  btn.classList.add('open');
  const pop = document.createElement('div');
  pop.className = 's-menu-pop';
  pop.innerHTML = `
    <button type="button" data-act="export"><span class="mi">📄</span>Export .md</button>
    <button type="button" data-act="delete" class="danger"><span class="mi">🗑</span>Delete</button>`;
  pop.onclick = e => {
    e.stopPropagation();
    const act = e.target.closest('button')?.dataset.act;
    if (act === 'export') {
      if (typeof exportSessionMarkdownById === 'function') exportSessionMarkdownById(s.id, s.title);
      closeSessionMenu();
    } else if (act === 'delete') {
      deleteSession(s.id);
      closeSessionMenu();
    }
  };
  document.body.appendChild(pop);
  const br = btn.getBoundingClientRect();
  const pr = pop.getBoundingClientRect();
  let left = br.right - pr.width;
  left = Math.max(6, Math.min(left, window.innerWidth - pr.width - 6));
  let top = br.bottom + 4;
  if (top + pr.height > window.innerHeight - 6) top = br.top - pr.height - 4;
  pop.style.left = left + 'px';
  pop.style.top = top + 'px';
}

async function deleteSession(sid) {
  try { await fetch(`/control/sessions/${sid}`, { method: 'DELETE', headers: { ...getDeviceHeaders() } }); } catch (e) {}
  if (curSession && curSession.id === sid) {
    curSession = null;
    messages = [];
    try {
      localStorage.removeItem(agentMode ? 'active_agent_session_id' : 'active_chat_session_id');
    } catch (e) {}
    renderAll();
  }
  loadSessions(false);
}

function persistMsgForSession(sid, role, content, meta) {
  if (!sid) return Promise.resolve();
  return fetch(`/control/sessions/${sid}/messages`, {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ role, content, meta }),
  }).catch(() => {});
}
window.persistMsgForSession = persistMsgForSession;

function persistMsg(role, content, meta) {
  if (!curSession) return;
  persistMsgForSession(curSession.id, role, content, meta);
}

function formatSessionTitleFromPrompt(text) {
  if (!text) return agentMode ? 'New agent task' : 'New chat';
  let clean = text.replace(/--- (?:FILE|IMAGE):[\s\S]*?--- END [^\n]+ ---/g, '').trim();
  clean = clean.replace(/\s+/g, ' ');
  if (!clean) return agentMode ? 'Task session' : 'Chat session';
  return clean.length > 50 ? clean.slice(0, 48) + '…' : clean;
}

function ensureSession(promptText) {
  const desiredTitle = formatSessionTitleFromPrompt(promptText);
  if (curSession) {
    // If curSession has a generic default title and we now have a real prompt, auto-rename it
    if ((curSession.title === 'New chat' || curSession.title === 'New agent task' || curSession.title === 'Chat session' || curSession.title === 'Agent Task') && promptText) {
      curSession.title = desiredTitle;
      fetch(`/control/sessions/${curSession.id}`, {
        method: 'PATCH',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ title: desiredTitle })
      }).then(() => loadSessions()).catch(() => {});
    }
    return Promise.resolve(curSession);
  }
  if (agentMode && (!curProject || !curProject.id)) {
    toast('Please select a project first', true);
    return Promise.resolve(null);
  }
  const pid = (agentMode && curProject) ? curProject.id : 0;
  return fetch(`/control/projects/${pid}/sessions`, {
    method: 'POST',
    headers: {
      'Content-Type': 'application/json',
      ...getDeviceHeaders(),
    },
    body: JSON.stringify({ title: desiredTitle }),
  })
    .then(r => r.json())
    .then(j => {
      curSession = j.session;
      if (curSession) {
        try {
          localStorage.setItem(agentMode ? 'active_agent_session_id' : 'active_chat_session_id', String(curSession.id));
        } catch (e) {}
      }
      loadSessions();
      return curSession;
    })
    .catch(() => null);
}

$('btn-newchat').onclick = () => {
  const isCurRunning = curSession && window.bgJobs && window.bgJobs.has(String(curSession.id));
  curSession = null;
  messages = [];
  try {
    localStorage.removeItem(agentMode ? 'active_agent_session_id' : 'active_chat_session_id');
  } catch (e) {}
  setGenUI(false);
  renderAll();
  loadSessions(false);
  if (typeof updateBgIndicators === 'function') updateBgIndicators();
  if ($('input')) {
    $('input').value = '';
    $('input').focus();
  }
  clearAttachments();
  if (isCurRunning) {
    toast('Previous task is continuing in background ⚡');
  } else {
    toast(agentMode ? 'New agent task started' : 'New chat started');
  }
};

const btnNewSession = $('btn-newsession');
if (btnNewSession) {
  btnNewSession.onclick = () => $('btn-newchat').click();
}

// Project modal handling
$('btn-newproject').onclick = () => {
  $('new-proj-name').value = '';
  $('new-proj-dir').value = '';
  $('proj-modal').hidden = false;
  setTimeout(() => $('new-proj-name').focus(), 50);
  refreshCompanionStatus();
};

async function refreshCompanionStatus() {
  const el = $('companion-status');
  if (window.electronAPI && window.electronAPI.isNativeApp) {
    if (el) {
      el.textContent = `🟢 Native App — browsing runs directly on your machine`;
      el.style.color = 'var(--green)';
    }
    updateAgentModeAvailability(true, 'Native App');
    return;
  }
  try {
    const d = await (await fetch('/control/companion/status', {
      headers: { ...getDeviceHeaders() }
    })).json();
    updateAgentModeAvailability(d.connected, d.hostname);
    if (!el) return;
    if (d.connected) {
      el.textContent = `🟢 Connected to ${d.hostname || 'your device'} — browsing runs on your machine`;
      el.style.color = 'var(--green)';
    } else {
      el.textContent = '⚪ No companion connected — folder browsing/writes run on the server';
      el.style.color = 'var(--dim)';
    }
  } catch (e) { if (el) el.textContent = ''; }
}

const closeProjModal = () => { $('proj-modal').hidden = true; };
$('btn-proj-cancel').onclick = closeProjModal;
$('proj-modal-close').onclick = closeProjModal;
if ($('btn-delproject')) $('btn-delproject').onclick = () => { if (curProject) deleteProject(curProject.id, curProject.name); };
if ($('btn-delproject-icon')) $('btn-delproject-icon').onclick = () => { if (curProject) deleteProject(curProject.id, curProject.name); };

async function submitNewProject() {
  const name = $('new-proj-name').value.trim();
  const workspace_dir = $('new-proj-dir').value.trim() || null;
  if (!name) { toast('Project name is required', true); return; }
  try {
    const r = await fetch('/control/projects', {
      method: 'POST',
      headers: {
        'Content-Type': 'application/json',
        ...getDeviceHeaders(),
      },
      body: JSON.stringify({
        name,
        workspace_dir,
        device_id: getClientDeviceId(),
        device_name: getClientDeviceName(),
      }),
    });
    const j = await r.json();
    if (!r.ok) throw new Error(j.error || ('HTTP ' + r.status));
    closeProjModal();
    if (!agentMode && typeof setAppMode === 'function') {
      setAppMode(true, true);
    }
    await loadProjects();
    await activateProject(j.project.id);
    toast(`Project "${j.project.name}" created & activated`);
  } catch (e) {
    toast('Project create failed: ' + e.message, true);
  }
}

$('btn-proj-submit').onclick = submitNewProject;
$('new-proj-name').onkeydown = e => { if (e.key === 'Enter') submitNewProject(); };
$('new-proj-dir').onkeydown = e => { if (e.key === 'Enter') submitNewProject(); };

// Workspace directory browsing & picker handling
$('btn-browse-dir').onclick = async () => {
  const btn = $('btn-browse-dir');
  const txt = $('btn-browse-dir-text');
  const icon = $('btn-browse-dir-icon');
  const oldText = 'Browse…';
  txt.textContent = 'Choosing…';
  icon.textContent = '⏳';
  btn.disabled = true;
  try {
    const curVal = $('new-proj-dir').value.trim();

    // 1. Direct native app dialog (Electron IPC) on installed machine
    if (window.electronAPI && typeof window.electronAPI.browseFolder === 'function') {
      const selected = await window.electronAPI.browseFolder(curVal);
      if (selected) {
        $('new-proj-dir').value = selected;
        const nameInput = $('new-proj-name');
        if (!nameInput.value.trim()) {
          const parts = selected.replace(/\\/g, '/').split('/').filter(Boolean);
          if (parts.length) nameInput.value = parts[parts.length - 1];
        }
        toast(`Selected: ${selected}`);
      }
      return;
    }

    // 2. Modern browser File System Access API (runs on user's machine)
    if (window.showDirectoryPicker && !window.electronAPI) {
      try {
        const dirHandle = await window.showDirectoryPicker();
        if (dirHandle && dirHandle.name) {
          const nameInput = $('new-proj-name');
          if (!nameInput.value.trim()) nameInput.value = dirHandle.name;
          // Prompt user to enter or confirm full path on their local OS
          const entered = prompt(`Selected folder: "${dirHandle.name}"\nEnter the full local path on this machine for agent tasks:`, curVal || '');
          if (entered && entered.trim()) {
            $('new-proj-dir').value = entered.trim();
            toast(`Selected: ${entered.trim()}`);
          } else {
            toast(`Project name set to "${dirHandle.name}"`);
          }
          return;
        }
      } catch (pickerErr) {
        if (pickerErr.name === 'AbortError') return; // User cancelled
      }
    }

    // 3. Server route (bridges to companion if connected)
    const r = await fetch('/control/browse_folder', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', ...getDeviceHeaders() },
      body: JSON.stringify({ initial_dir: curVal }),
    });
    const d = await r.json().catch(() => ({}));
    if (r.ok && d.ok && d.path) {
      $('new-proj-dir').value = d.path;
      const nameInput = $('new-proj-name');
      if (!nameInput.value.trim()) {
        const parts = d.path.replace(/\\/g, '/').split('/').filter(Boolean);
        if (parts.length) nameInput.value = parts[parts.length - 1];
      }
      toast(`Selected: ${d.path}`);
      return;
    }

    // 4. Remote client without companion: prompt for local path directly on user's device
    const entered = prompt('Enter your local workspace directory path on this machine (e.g. C:\\Users\\... or /home/...):', curVal || '');
    if (entered && entered.trim()) {
      $('new-proj-dir').value = entered.trim();
      const nameInput = $('new-proj-name');
      if (!nameInput.value.trim()) {
        const parts = entered.trim().replace(/\\/g, '/').split('/').filter(Boolean);
        if (parts.length) nameInput.value = parts[parts.length - 1];
      }
      toast(`Selected: ${entered.trim()}`);
    }
  } catch (e) {
    toast('Directory chooser error: ' + (e.message || String(e)), true);
  } finally {
    txt.textContent = oldText;
    icon.textContent = '📁';
    btn.disabled = false;
  }
};

document.querySelectorAll('.btn-quick-dir').forEach(b => {
  b.onclick = () => {
    const d = b.getAttribute('data-dir');
    $('new-proj-dir').value = d === 'default' ? '' : d;
    const nameInput = $('new-proj-name');
    if (!nameInput.value.trim() && d !== 'default') {
      const parts = d.replace(/\\/g, '/').split('/').filter(Boolean);
      if (parts.length) nameInput.value = parts[parts.length - 1];
    }
  };
});

// In-App Directory Picker Modal
let pickerCurrentPath = '';
let pickerSelectedPath = '';

const closeDirPicker = () => { $('dir-picker-modal').hidden = true; };
$('dir-picker-close').onclick = closeDirPicker;
$('btn-dir-picker-cancel').onclick = closeDirPicker;

async function navigateDirPicker(path) {
  const listEl = $('dir-picker-list');
  listEl.innerHTML = '<div style="color:var(--dim); font-size:11px; padding:12px; text-align:center;">Loading directories…</div>';
  try {
    let d = null;
    if (window.electronAPI && typeof window.electronAPI.browseDir === 'function') {
      const nativeRes = await window.electronAPI.browseDir(path);
      d = { ok: true, ...nativeRes };
    } else {
      const q = path ? `?path=${encodeURIComponent(path)}` : '';
      const r = await fetch(`/control/fs/browse${q}`, {
        headers: { ...getDeviceHeaders() }
      });
      try { d = await r.json(); } catch (_) {}
      if (!r.ok || !d || !d.ok) {
        let errStr = (d && (d.error || d.detail)) || `HTTP ${r.status}`;
        if (typeof errStr === 'object') errStr = JSON.stringify(errStr);
        if (r.status === 404) errStr += ' (Please restart server_manager.py to activate file browser)';
        throw new Error(errStr);
      }
    }
    pickerCurrentPath = d.current;
    pickerSelectedPath = d.current;
    $('dir-picker-path').textContent = d.current;
    $('btn-dir-picker-up').disabled = !d.parent;
    $('btn-dir-picker-up').onclick = () => { if (d.parent) navigateDirPicker(d.parent); };

    // Render drives
    const drivesEl = $('dir-picker-drives');
    drivesEl.innerHTML = '';
    (d.drives || []).forEach(drv => {
      const db = document.createElement('button');
      db.className = 'btn ' + (d.current.toUpperCase().startsWith(drv.toUpperCase()) ? 'blue' : 'ghost');
      db.style.cssText = 'width:auto; margin:0; padding:2px 8px; font-size:10.5px;';
      db.textContent = drv;
      db.onclick = () => navigateDirPicker(drv);
      drivesEl.appendChild(db);
    });

    // Render subdirectories
    listEl.innerHTML = '';
    if (!d.subdirs || d.subdirs.length === 0) {
      listEl.innerHTML = '<div style="color:var(--dim); font-size:11px; padding:14px; text-align:center;">(Empty or no accessible subdirectories)</div>';
      return;
    }

    d.subdirs.forEach(name => {
      const item = document.createElement('div');
      item.className = 'dir-item';
      item.innerHTML = `<span style="font-size:13px;">📁</span> <span style="flex:1; overflow:hidden; text-overflow:ellipsis; white-space:nowrap;">${name}</span>`;
      
      const getFullPath = () => {
        const sep = d.current.includes('\\') ? '\\' : '/';
        return d.current.endsWith(sep) ? (d.current + name) : (d.current + sep + name);
      };

      item.onclick = () => {
        document.querySelectorAll('#dir-picker-list .dir-item').forEach(el => el.classList.remove('selected'));
        item.classList.add('selected');
        pickerSelectedPath = getFullPath();
      };
      item.ondblclick = () => {
        navigateDirPicker(getFullPath());
      };
      listEl.appendChild(item);
    });
  } catch (e) {
    const errMsg = (e && e.message) ? e.message : String(e);
    listEl.innerHTML = `<div style="color:var(--red); font-size:11px; padding:10px;">Error: ${esc(errMsg)}</div>`;
  }
}

$('btn-open-dir-picker').onclick = () => {
  $('dir-picker-modal').hidden = false;
  const initial = $('new-proj-dir').value.trim();
  navigateDirPicker(initial);
};

$('btn-dir-picker-select').onclick = () => {
  if (pickerSelectedPath) {
    $('new-proj-dir').value = pickerSelectedPath;
    const nameInput = $('new-proj-name');
    if (!nameInput.value.trim()) {
      const parts = pickerSelectedPath.replace(/\\/g, '/').split('/').filter(Boolean);
      if (parts.length) nameInput.value = parts[parts.length - 1];
    }
  }
  closeDirPicker();
};

$('btn-dir-picker-mkdir').onclick = async () => {
  const name = $('dir-picker-new-name').value.trim();
  if (!name) return;
  try {
    let d = null;
    if (window.electronAPI && typeof window.electronAPI.mkdir === 'function') {
      const res = await window.electronAPI.mkdir(pickerCurrentPath, name);
      d = { ok: true, ...res };
    } else {
      const r = await fetch('/control/fs/mkdir', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json', ...getDeviceHeaders() },
        body: JSON.stringify({ path: pickerCurrentPath, name }),
      });
      d = await r.json();
    }
    if (d.ok) {
      $('dir-picker-new-name').value = '';
      await navigateDirPicker(pickerCurrentPath);
      toast(`Created folder "${name}"`);
    } else {
      toast('Failed to create folder: ' + (d.error || 'unknown'), true);
    }
  } catch (e) {
    toast('Create folder error: ' + e.message, true);
  }
};

async function activateProject(pid) {
  try {
    const r = await fetch(`/control/projects/${pid}/activate`, {
      method: 'POST',
      headers: { ...getDeviceHeaders() },
    });
    const d = await r.json();
    curProject = d.project || null;
    try {
      if (curProject) {
        localStorage.setItem('active_project_id', String(curProject.id));
        localStorage.setItem('active_project_name', curProject.name);
      } else {
        localStorage.removeItem('active_project_id');
        localStorage.removeItem('active_project_name');
      }
    } catch (_) {}
    curSession = null;
    messages = [];
    renderAll();
    await loadProjects();
    if (curProject) {
      const ws = curProject.workspace_dir || `E:\\AI\\workspace\\${curProject.name}`;
      toast(`Active project: ${curProject.name} (${ws})`);
    } else {
      toast('No active project');
    }
    updateWsRail();
    if (wsPanelOpen) wsRefreshTree();   // tree now shows the new project's files
  } catch (e) { toast('Activate failed', true); }
}

if ($('project-sel')) {
  $('project-sel').onchange = e => {
    const v = e.target.value;
    if (!v) { setNoProject(); return; }
    activateProject(parseInt(v));
  };
}

async function setNoProject() {
  try {
    await fetch('/control/projects/0/activate', {
      method: 'POST',
      headers: { ...getDeviceHeaders() },
    });
  } catch (e) {}
  try {
    localStorage.removeItem('active_project_id');
    localStorage.removeItem('active_project_name');
  } catch (_) {}
  curProject = null; curSession = null;
  messages = [];
  renderAll();
  loadProjects();
  if (wsPanelOpen) setWsPanel(false);   // no project -> no workspace to show
  updateWsRail();
}

/* Draw attention to the Projects card when agent mode needs a project
   (used by the /compact gate and the agent-run project gate). */
function flashProjectsCard() {
  const card = $('projects-card');
  if (card) {
    card.classList.remove('flash');
    void card.offsetWidth;              // restart the CSS animation
    card.classList.add('flash');
    setTimeout(() => card.classList.remove('flash'), 2200);
    card.scrollIntoView({ behavior: 'smooth', block: 'nearest' });
  }
  if (typeof toast === 'function') toast('Please select a project first — pick or create one above', true);
}
window.flashProjectsCard = flashProjectsCard;

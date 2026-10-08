/* ---------------- custom-agents.js ----------------
 * User-wise Custom Agents Controller:
 * - Load, create, update, delete, fork custom agents
 * - Agent switcher dropdown in composer
 * - Active agent chip & custom prompt placeholder
 * - Visual Custom Agent Builder modal
 */

(function () {
  let customAgentsList = [];
  let availableToolsList = [];
  let activeCustomAgent = null;
  let agentGridFilter = '';

  const $ = id => document.getElementById(id);
  const esc = s => String(s == null ? '' : s).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;');

  async function api(url, opts = {}) {
    const res = await fetch(url, opts);
    const data = await res.json().catch(() => ({}));
    if (!res.ok) throw new Error(data.error || data.detail || ('HTTP ' + res.status));
    return data;
  }

  /* ---------------- Data Loaders ---------------- */

  async function loadCustomAgents() {
    try {
      const data = await api('/custom-agents');
      customAgentsList = data.agents || [];
      renderSidebarAgentsList();
      renderPickerDropdown();
      renderSettingsAgentsGrid();
      restoreSavedAgent();
      if (typeof loadSessions === 'function') loadSessions(false);
    } catch (e) {
      console.warn('[custom-agents] failed to load agents:', e);
    }
  }

  async function loadAvailableTools() {
    try {
      const data = await api('/custom-agents/tools/available');
      availableToolsList = data.tools || [];
    } catch (e) {
      console.warn('[custom-agents] failed to load tools:', e);
    }
  }

  function getActiveCustomAgentId() {
    if (activeCustomAgent && !isAgentOnCurrentDevice(activeCustomAgent)) {
      clearActiveCustomAgent();
      return null;
    }
    return activeCustomAgent ? activeCustomAgent.id : null;
  }

  function getActiveCustomAgent() {
    if (activeCustomAgent && !isAgentOnCurrentDevice(activeCustomAgent)) {
      clearActiveCustomAgent();
      return null;
    }
    return activeCustomAgent;
  }

  /* ---------------- Active Agent State ---------------- */

  // composer temperature/effort at the moment the agent was picked: while they're
  // unchanged the request sends null so the agent's own values apply server-side
  let activationSnapshot = null;

  function _composerTemp() {
    try { return (typeof getSamplingConfig === 'function') ? getSamplingConfig().temp : undefined; } catch (_) { return undefined; }
  }

  function _composerEffort() {
    try { return (typeof getReasoningEffort === 'function') ? getReasoningEffort() : undefined; } catch (_) { return undefined; }
  }

  // {temperature, reasoning_effort} for request payloads
  function requestOverrides() {
    const temp = _composerTemp();
    const effort = _composerEffort();
    if (!activeCustomAgent || !activationSnapshot) return { temperature: temp, reasoning_effort: effort };
    return {
      temperature: temp === activationSnapshot.temp ? null : temp,
      reasoning_effort: effort === activationSnapshot.effort ? null : effort,
    };
  }

  function isAgentOnCurrentDevice(agent) {
    if (!agent) return true;
    if (agent.scope === 'template') return false;
    const myDevId = (typeof getClientDeviceId === 'function') ? getClientDeviceId() : '';
    const myDevLabel = (typeof getDeviceDisplayLabel === 'function') ? getDeviceDisplayLabel() : '';
    const myDevName = (typeof getClientDeviceName === 'function') ? getClientDeviceName() : '';

    if (agent.device_id && myDevId) {
      return String(agent.device_id).toLowerCase() === String(myDevId).toLowerCase();
    }
    if (agent.device_name && (myDevName || myDevLabel)) {
      const aName = String(agent.device_name).trim().toLowerCase();
      const devNameMatch = myDevName && aName === String(myDevName).trim().toLowerCase();
      const devLabelMatch = myDevLabel && aName === String(myDevLabel).trim().toLowerCase();
      return !!(devNameMatch || devLabelMatch);
    }
    if (agent.device_id || agent.device_name) {
      return false;
    }
    return true;
  }
  window.isAgentOnCurrentDevice = isAgentOnCurrentDevice;

  function setActiveCustomAgent(agent, fromParentSync = false, noBind = false) {
    if (agent && !isAgentOnCurrentDevice(agent)) {
      clearActiveCustomAgent();
      if (typeof toast === 'function') toast(`Bound to "${agent.device_name || 'another device'}". Fork it to use on this machine.`, true);
      openForkModal(agent);
      return;
    }
    activeCustomAgent = agent || null;
    window.activeCustomAgent = activeCustomAgent;
    activationSnapshot = activeCustomAgent ? { temp: _composerTemp(), effort: _composerEffort() } : null;

    // Settings runs in an iframe: tell the main window so its in-memory agent follows
    if (!fromParentSync && window.parent && window.parent !== window) {
      try { window.parent.postMessage({ type: 'custom-agent-selected', id: activeCustomAgent ? activeCustomAgent.id : null }, window.location.origin); } catch (_) {}
    }

    if (activeCustomAgent) {
      localStorage.setItem('active_custom_agent_id', String(activeCustomAgent.id));
    } else {
      localStorage.removeItem('active_custom_agent_id');
    }
    if (!noBind && typeof curSession !== 'undefined' && curSession && curSession.id != null) bindSession(curSession.id, activeCustomAgent);

    // Hide any legacy selectors if present in DOM
    const pill = $('active-agent-pill');
    if (pill) pill.style.display = 'none';
    const btn = $('btn-agent-picker');
    if (btn) btn.style.display = 'none';

    // Update Terminal CWD when custom agent changes
    if (window.RightDock && typeof window.RightDock.setTerminalCwd === 'function') {
      if (activeCustomAgent && activeCustomAgent.work_dir) {
        window.RightDock.setTerminalCwd(activeCustomAgent.work_dir, `${activeCustomAgent.name} (${activeCustomAgent.work_dir})`);
      } else if (activeCustomAgent) {
        const fallbackCwd = window.curProject?.workspace_dir || '';
        window.RightDock.setTerminalCwd(fallbackCwd, activeCustomAgent.name);
      } else if (window.curProject && window.curProject.workspace_dir) {
        window.RightDock.setTerminalCwd(window.curProject.workspace_dir, `${window.curProject.name} (${window.curProject.workspace_dir})`);
      }
    }

    // Update Right Dock tab visibility (show code & terminal on custom agent)
    if (window.RightDock && typeof window.RightDock.syncTabVisibility === 'function') {
      const curMode = (typeof window.appMode !== 'undefined' ? window.appMode : 'chat');
      window.RightDock.syncTabVisibility(curMode === 'agent' || curMode === 'code' || !!activeCustomAgent);
    }
    if (window.RightDock && typeof window.RightDock.updateTerminalStatus === 'function') {
      window.RightDock.updateTerminalStatus();
    }

    renderAgentChip();
    updateComposerPlaceholder();
    renderSidebarAgentsList();
    closePickerDropdown();
  }

  /* sessions remember their agent (browser-local), so reopening one restores it */
  const SESSION_MAP_KEY = 'custom_agent_sessions';

  function _sessionMap() {
    try { return JSON.parse(localStorage.getItem(SESSION_MAP_KEY) || '{}') || {}; } catch (_) { return {}; }
  }

  function bindSession(sid, agent) {
    const m = _sessionMap();
    if (agent) m[String(sid)] = agent.id; else delete m[String(sid)];
    const keys = Object.keys(m);
    if (keys.length > 500) keys.slice(0, keys.length - 500).forEach(k => delete m[k]);
    try { localStorage.setItem(SESSION_MAP_KEY, JSON.stringify(m)); } catch (_) {}
  }

  function restoreForSession(sid, sessionObj = null) {
    let aid = _sessionMap()[String(sid)];
    if (aid == null && sessionObj && sessionObj.agent_id) {
      aid = sessionObj.agent_id;
    }
    const a = aid != null ? customAgentsList.find(x => String(x.id) === String(aid)) : null;
    if (a) {
      bindSession(sid, a);
      if (!activeCustomAgent || activeCustomAgent.id !== a.id) setActiveCustomAgent(a, false, true);
    } else if (activeCustomAgent) {
      activeCustomAgent = null;          // not bound: plain session (keep the map as is)
      window.activeCustomAgent = null;
      localStorage.removeItem('active_custom_agent_id');
      activationSnapshot = null;
      updateComposerPlaceholder();
      renderSidebarAgentsList();
    }
  }

  function clearActiveCustomAgent() {
    setActiveCustomAgent(null);
  }

  function restoreSavedAgent() {
    const savedId = localStorage.getItem('active_custom_agent_id');
    if (savedId) {
      const found = customAgentsList.find(a => String(a.id) === String(savedId) && a.scope === 'mine');
      if (found && isAgentOnCurrentDevice(found)) {
        setActiveCustomAgent(found);
      } else {
        clearActiveCustomAgent();
      }
    }
  }

  function selectCustomAgent(agent) {
    if (!agent) return;
    if (!isAgentOnCurrentDevice(agent)) {
      clearActiveCustomAgent();
      if (typeof toast === 'function') toast(`Bound to "${agent.device_name || 'another device'}". Fork it to configure your local workspace path.`, true);
      openForkModal(agent);
      return;
    }
    setActiveCustomAgent(agent, false, true);

    if (window.RightDock && typeof window.RightDock.setTerminalCwd === 'function') {
      if (agent.work_dir) {
        window.RightDock.setTerminalCwd(agent.work_dir, `${agent.name} (${agent.work_dir})`);
      }
    }

    // Check if curSession belongs to this agent
    let curAgentId = null;
    if (curSession) {
      const map = _sessionMap();
      curAgentId = curSession.agent_id != null ? String(curSession.agent_id) : (map[String(curSession.id)] ? String(map[String(curSession.id)]) : null);
    }
    // If no session or session doesn't belong to this agent: clear to empty chat box
    if (!curSession || (curAgentId && curAgentId !== String(agent.id))) {
      curSession = null;
      messages = [];
      try {
        const sKey = typeof window.getActiveSessionKey === 'function' ? window.getActiveSessionKey() : 'active_agent_session_id';
        localStorage.removeItem(sKey);
      } catch (_) {}
      if (typeof setGenUI === 'function') setGenUI(false);
      if (typeof renderAll === 'function') renderAll();
    }

    if (typeof loadSessions === 'function') loadSessions(false);
    if (typeof toast === 'function') {
      toast(`Selected agent: ${agent.name} ${agent.icon || '🤖'}`);
    }
    const input = $('input');
    if (input) input.focus();
    renderSidebarAgentsList();
  }

  async function startAgentChatSession(agent) {
    if (agent && !isAgentOnCurrentDevice(agent)) {
      if (typeof toast === 'function') toast(`Bound to "${agent.device_name || 'another device'}". Fork it to configure your local workspace path.`, true);
      openForkModal(agent);
      return;
    }
    setActiveCustomAgent(agent, false, true);   // bound to the new session below

    const isCurRunning = curSession && window.bgJobs && window.bgJobs.has(String(curSession.id));
    curSession = null;
    messages = [];
    try {
      const sKey = typeof window.getActiveSessionKey === 'function' ? window.getActiveSessionKey() : 'active_agent_session_id';
      localStorage.removeItem(sKey);
    } catch (_) {}

    if (typeof setGenUI === 'function') setGenUI(false);
    if (typeof renderAll === 'function') renderAll();

    // Create a new session in projects.db with the agent's name
    const pid = (agentMode && curProject) ? curProject.id : 0;
    const title = `${agent.icon || '🤖'} ${agent.name}`;
    try {
      const devHeaders = (typeof getDeviceHeaders === 'function') ? getDeviceHeaders() : {};
      const res = await fetch(`/control/projects/${pid}/sessions`, {
        method: 'POST',
        headers: {
          'Content-Type': 'application/json',
          ...devHeaders,
        },
        body: JSON.stringify({ title, agent_id: agent.id }),
      });
      const data = await res.json();
      if (data && data.session) {
        curSession = data.session;
        bindSession(data.session.id, agent);
        try {
          const sKey = typeof window.getActiveSessionKey === 'function' ? window.getActiveSessionKey() : 'active_agent_session_id';
          localStorage.setItem(sKey, String(data.session.id));
        } catch (_) {}
      }
    } catch (e) {
      console.warn('[custom-agents] failed to create session:', e);
    }

    if (typeof loadSessions === 'function') loadSessions(false);
    if (typeof updateBgIndicators === 'function') updateBgIndicators();

    // input_template is applied server-side to the first message ({input} = what you type)
    const input = $('input');
    if (input) {
      input.value = '';
      input.focus();
    }
    if (typeof clearAttachments === 'function') clearAttachments();

    if (typeof toast === 'function') {
      if (isCurRunning) {
        toast(`Task in BG ⚡ · Started with ${agent.name}`);
      } else {
        toast(`Started session with ${agent.name} ${agent.icon || '🤖'}`);
      }
    }
    renderSidebarAgentsList();
  }

  function renderSidebarAgentsList() {
    const list = $('custom-agents-sidebar-list');
    if (!list) return;

    const card = $('custom-agents-card');
    const curMode = window.appMode || (localStorage.getItem('app_mode') || 'chat');
    if (card) {
      card.style.display = (curMode === 'agent') ? 'flex' : 'none';
    }

    const myAgents = customAgentsList.filter(a => a.scope === 'mine');

    const btnHeadNew = $('btn-new-custom-agent');
    if (btnHeadNew) {
      btnHeadNew.onclick = (e) => {
        e.stopPropagation();
        openAgentBuilder(null);
      };
    }

    if (!myAgents.length) {
      list.innerHTML = `
        <div class="sidebar-empty-card" style="display:flex; flex-direction:column; align-items:center; justify-content:center; text-align:center; padding:18px 12px; gap:8px; margin:4px; border-radius:10px; background:rgba(255,255,255,0.02); border:1px dashed var(--border);">
          <div style="width:32px; height:32px; border-radius:8px; background:rgba(56,189,248,0.08); border:1px solid rgba(56,189,248,0.22); display:flex; align-items:center; justify-content:center; color:var(--accent);">
            <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M12 5v14M5 12h14"/></svg>
          </div>
          <div style="font-size:11.5px; font-weight:600; color:var(--text);">No Custom Agents</div>
          <div style="font-size:10.5px; color:var(--dim); line-height:1.4; max-width:170px;">Click ＋ above to create one, or fork an agent from another device.</div>
        </div>
      `;
      return;
    }

    let html = '';
    const myDevLabel = (typeof getDeviceDisplayLabel === 'function') ? getDeviceDisplayLabel() : 'This Device';
    for (const a of myAgents) {
      const isCurDev = isAgentOnCurrentDevice(a);
      const isSel = isCurDev && activeCustomAgent && activeCustomAgent.id === a.id;
      const devLabel = a.device_name || (isCurDev ? myDevLabel : 'Other Device');
      html += `
        <div class="sidebar-agent-row ${isSel ? 'selected' : ''} ${!isCurDev ? 'other-device-barrier' : ''}" data-agent-id="${a.id}" title="${esc(a.name)}: ${esc(a.description)}${!isCurDev ? `\n(Bound to: ${devLabel}. Only Fork is available on this device)` : ''}">
          <span class="sidebar-agent-icon">${esc(a.icon || '🤖')}</span>
          <div class="sidebar-agent-info">
            <div class="sidebar-agent-device-tag" style="font-size:9px; color:${isCurDev ? 'var(--accent, #38bdf8)' : 'var(--amber, #f59e0b)'}; font-weight:700; display:flex; align-items:center; gap:4px; margin-bottom:1px; line-height:1.2; flex-wrap:nowrap; min-width:0; overflow:hidden;">
              <span style="flex-shrink:0;">${isCurDev ? '💻' : '🔒'}</span>
              <span style="overflow:hidden; text-overflow:ellipsis; white-space:nowrap; min-width:0; flex:1 1 auto;">${esc(devLabel)}</span>
              ${!isCurDev ? '<span style="font-size:7.5px; background:rgba(245,158,11,0.18); color:var(--amber, #f59e0b); padding:1px 5px; border-radius:3px; margin-left:auto; text-transform:uppercase; font-weight:700; white-space:nowrap; flex-shrink:0; display:inline-block; line-height:1.2;">Fork only</span>' : ''}
            </div>
            <div class="sidebar-agent-name">${esc(a.name)}</div>
            <div class="sidebar-agent-desc">
              ${a.work_dir ? `<span class="ca-workdir-pill" title="Workspace: ${esc(a.work_dir)}">📁 ${esc(a.work_dir.split(/[/\\]/).pop() || a.work_dir)}</span> · ` : ''}
              ${esc(a.description || a.preferred_lane || '')}${shareChip(a)}
            </div>
          </div>
          <div class="sidebar-agent-actions">
            ${!isCurDev ? `
              <button type="button" class="btn ghost fork-agent-btn" data-fork-id="${a.id}" title="Fork Agent to use on this device">🍴 Fork</button>
            ` : `
              <button type="button" class="btn ghost btn-icon-xs fork-agent-btn" data-fork-id="${a.id}" title="Fork Agent">🍴</button>
              <button type="button" class="btn ghost btn-icon-xs edit-agent-btn" data-edit-id="${a.id}" title="Edit Agent">✏️</button>
            `}
          </div>
        </div>
      `;
    }

    list.innerHTML = html;

    list.querySelectorAll('.sidebar-agent-row').forEach(row => {
      row.onclick = (e) => {
        if (e.target.closest('.edit-agent-btn') || e.target.closest('.fork-agent-btn')) return;
        const aid = row.getAttribute('data-agent-id');
        const agent = customAgentsList.find(a => String(a.id) === String(aid));
        if (agent) {
          if (!isAgentOnCurrentDevice(agent)) {
            if (typeof toast === 'function') toast(`"${agent.name}" is bound to ${agent.device_name || 'another device'}. Only Fork is available on this device.`, true);
            openForkModal(agent);
            return;
          }
          if (activeCustomAgent && String(activeCustomAgent.id) === String(aid)) {
            clearActiveCustomAgent();
            if (typeof toast === 'function') toast('Deselected agent');
            if (typeof loadSessions === 'function') loadSessions(false);
            return;
          }
          selectCustomAgent(agent);
        }
      };
    });

    list.querySelectorAll('.edit-agent-btn').forEach(btn => {
      btn.onclick = (e) => {
        e.stopPropagation();
        const id = btn.getAttribute('data-edit-id');
        const agent = customAgentsList.find(a => String(a.id) === String(id));
        if (agent) openAgentBuilder(agent);
      };
    });

    list.querySelectorAll('.fork-agent-btn').forEach(btn => {
      btn.onclick = (e) => {
        e.stopPropagation();
        const id = btn.getAttribute('data-fork-id');
        const agent = customAgentsList.find(a => String(a.id) === String(id));
        if (agent) openForkModal(agent);
      };
    });
  }

  /* where an agent stands with sharing: waiting for a reviewer, shared, or not approved */
  function shareChip(a) {
    const t = { pending: 'waiting for approval', approved: 'shared with everyone', rejected: 'sharing not approved' }[a.share_status];
    return t ? ` <span class="ca-share ca-share-${esc(a.share_status)}">${t}</span>` : '';
  }

  function renderAgentChip() {
    const bar = $('cmd-chip-bar');
    if (!bar) return;
    const existingAgentChip = bar.querySelector('.custom-agent-chip');
    if (existingAgentChip) existingAgentChip.remove();
    if (!bar.children.length) bar.style.display = 'none';
  }

  function updateComposerPlaceholder() {
    const input = $('input');
    if (!input || window.armedCmd) return;
    if (activeCustomAgent) {
      input.placeholder = `${activeCustomAgent.icon || '🤖'} ${activeCustomAgent.name}: ${activeCustomAgent.description || 'Describe your task for this agent…'}`;
      if (activeCustomAgent.work_dir && activeCustomAgent.scope === 'mine') input.placeholder += `  (📁 works in ${activeCustomAgent.work_dir})`;
    } else {
      if (typeof window.refreshInputPlaceholder === 'function') {
        window.refreshInputPlaceholder();
      }
    }
  }

  /* ---------------- Dropdown Picker ---------------- */

  function renderPickerDropdown() {
    const drop = $('agent-picker-dropdown');
    if (!drop) return;

    let html = `
      <div class="agent-dropdown-header">
        <span>Select Agent</span>
        <button type="button" class="btn ghost btn-xs" id="btn-create-agent-drop">＋ New</button>
      </div>
      <div class="agent-picker-item ${!activeCustomAgent ? 'selected' : ''}" data-agent-id="default">
        <span class="agent-item-icon">🤖</span>
        <div class="agent-item-text">
          <div class="agent-item-name">General Agent (Default)</div>
          <div class="agent-item-desc">Full autonomous coding and orchestrator capabilities</div>
        </div>
        ${!activeCustomAgent ? '<span class="agent-item-check">✓</span>' : ''}
      </div>
    `;

    // Group: User's Own Agents
    const myAgents = customAgentsList.filter(a => a.scope === 'mine');
    const starterAgents = [];

    if (myAgents.length) {
      const myDevLabel = (typeof getDeviceDisplayLabel === 'function') ? getDeviceDisplayLabel() : 'This Device';
      html += `<div class="agent-dropdown-section-label">My Custom Agents</div>`;
      for (const a of myAgents) {
        const isSel = activeCustomAgent && activeCustomAgent.id === a.id;
        const isCurDev = isAgentOnCurrentDevice(a);
        const devLabel = a.device_name || myDevLabel;
        html += `
          <div class="agent-picker-item ${isSel ? 'selected' : ''}" data-agent-id="${a.id}">
            <span class="agent-item-icon">${esc(a.icon || '🤖')}</span>
            <div class="agent-item-text">
              <div class="agent-item-name">${!isCurDev ? '<span style="color:var(--amber, #f59e0b); font-size:10px; margin-right:3px;">🔒</span>' : ''}${esc(a.name)} <span style="font-size:9px; color:${isCurDev ? 'var(--accent, #38bdf8)' : 'var(--amber, #f59e0b)'}; font-weight:normal;">(${esc(devLabel)})</span></div>
              <div class="agent-item-desc">${esc(a.description)}</div>
            </div>
            <div class="agent-item-actions">
              ${isCurDev ? `<button class="btn ghost btn-icon-xs edit-agent-btn" data-edit-id="${a.id}" title="Edit Agent">✏️</button>` : ''}
              <button class="btn ghost btn-icon-xs fork-agent-btn" data-fork-id="${a.id}" title="Fork for this device">🍴</button>
              ${isSel ? '<span class="agent-item-check">✓</span>' : ''}
            </div>
          </div>
        `;
      }
    }

    // Group: Starter Templates
    if (starterAgents.length) {
      html += `<div class="agent-dropdown-section-label">Shared &amp; Templates</div>`;
      for (const a of starterAgents) {
        const isSel = activeCustomAgent && activeCustomAgent.id === a.id;
        html += `
          <div class="agent-picker-item ${isSel ? 'selected' : ''}" data-agent-id="${a.id}">
            <span class="agent-item-icon">${esc(a.icon || '🤖')}</span>
            <div class="agent-item-text">
              <div class="agent-item-name">${esc(a.name)}</div>
              <div class="agent-item-desc">${esc(a.description)}</div>
            </div>
            <div class="agent-item-actions">
              <button class="btn ghost btn-icon-xs fork-agent-btn" data-fork-id="${a.id}" title="Fork / Customize this template">🍴</button>
              ${isSel ? '<span class="agent-item-check">✓</span>' : ''}
            </div>
          </div>
        `;
      }
    }

    drop.innerHTML = html;

    // Attach listeners
    const createBtn = drop.querySelector('#btn-create-agent-drop');
    if (createBtn) {
      createBtn.onclick = (e) => {
        e.stopPropagation();
        closePickerDropdown();
        openAgentBuilder();
      };
    }

    drop.querySelectorAll('.agent-picker-item').forEach(item => {
      item.onclick = (e) => {
        if (e.target.closest('.edit-agent-btn') || e.target.closest('.fork-agent-btn')) return;
        const id = item.dataset.agentId;
        if (id === 'default') {
          clearActiveCustomAgent();
        } else {
          const a = customAgentsList.find(x => String(x.id) === String(id));
          if (a) {
            if (!isAgentOnCurrentDevice(a)) {
              closePickerDropdown();
              if (typeof toast === 'function') toast(`Bound to "${a.device_name || 'another device'}". Fork it to configure your local workspace path.`, true);
              openForkModal(a);
              return;
            }
            setActiveCustomAgent(a);
          }
        }
      };
    });

    drop.querySelectorAll('.edit-agent-btn').forEach(btn => {
      btn.onclick = (e) => {
        e.stopPropagation();
        closePickerDropdown();
        const a = customAgentsList.find(x => String(x.id) === String(btn.dataset.editId));
        if (a) openAgentBuilder(a);
      };
    });

    drop.querySelectorAll('.fork-agent-btn').forEach(btn => {
      btn.onclick = (e) => {
        e.stopPropagation();
        closePickerDropdown();
        const a = customAgentsList.find(x => String(x.id) === String(btn.dataset.forkId));
        if (a) openForkModal(a);
      };
    });
  }

  function togglePickerDropdown() {
    const drop = $('agent-picker-dropdown');
    if (!drop) return;
    const isVisible = drop.style.display === 'block';
    if (isVisible) {
      closePickerDropdown();
    } else {
      drop.style.display = 'block';
      renderPickerDropdown();
    }
  }

  function closePickerDropdown() {
    const drop = $('agent-picker-dropdown');
    if (drop) drop.style.display = 'none';
  }

  /* ---------------- Custom Agent Builder Modal ---------------- */

  let editingAgentId = null;

  async function openAgentBuilder(agent = null) {
    // If inside an iframe (like Settings modal), delegate to parent window so builder
    // renders top-level with full viewport space and uninhibited keyboard focus
    if (window.parent && window.parent !== window && typeof window.parent.openAgentBuilder === 'function') {
      window.parent.openAgentBuilder(agent);
      return;
    }

    if (!availableToolsList.length) {
      await loadAvailableTools();
    }

    editingAgentId = agent ? agent.id : null;
    const modal = $('custom-agent-modal');
    if (!modal) return;

    $('ca-modal-title').textContent = agent ? `Edit Agent: ${agent.name}` : 'Create Custom Agent';
    const myDevLabel = (typeof getDeviceDisplayLabel === 'function') ? getDeviceDisplayLabel() : 'This Device';
    const devNameEl = $('ca-modal-device-name');
    if (devNameEl) {
      if (agent) {
        devNameEl.textContent = `Bound Device: ${agent.device_name || myDevLabel}`;
      } else {
        devNameEl.textContent = `Binding to Device: ${myDevLabel}`;
      }
    }
    $('ca-name').value = agent ? agent.name : '';
    $('ca-slug').value = agent ? agent.slug : '';
    $('ca-icon').value = agent ? (agent.icon || '🤖') : '🤖';
    $('ca-desc').value = agent ? agent.description : '';
    $('ca-prompt').value = agent ? agent.system_prompt : '';
    $('ca-template').value = agent ? (agent.input_template || '') : '';
    $('ca-lane').value = agent ? (agent.preferred_lane || 'auto') : 'auto';
    $('ca-effort').value = agent ? (agent.reasoning_effort || 'medium') : 'medium';
    $('ca-temp').value = agent ? String(agent.temperature != null ? agent.temperature : 0.4) : '0.4';
    $('ca-temp-val').textContent = $('ca-temp').value;
    $('ca-public').checked = agent ? !!agent.is_public : false;
    $('ca-workdir').value = agent ? (agent.work_dir || '') : '';

    // Render tool checkboxes
    renderToolCheckboxes(agent ? (agent.tool_allowlist || []) : []);

    const deleteBtn = $('ca-btn-delete');
    if (deleteBtn) {
      deleteBtn.style.display = (agent && agent.can_edit && agent.scope !== 'template') ? 'inline-block' : 'none';
    }

    modal.style.display = 'flex';
    window.focus();
    setTimeout(() => {
      const nameInput = $('ca-name');
      if (nameInput) {
        nameInput.focus();
        nameInput.select && nameInput.select();
      }
    }, 60);
  }

  function closeAgentBuilder() {
    const modal = $('custom-agent-modal');
    if (modal) modal.style.display = 'none';
    editingAgentId = null;
  }

  function renderToolCheckboxes(selectedTools = []) {
    const container = $('ca-tools-container');
    if (!container) return;

    // Group tools by category
    const groups = {};
    for (const t of availableToolsList) {
      const cat = t.category || 'General';
      if (!groups[cat]) groups[cat] = [];
      groups[cat].push(t);
    }

    const isAllSelected = !selectedTools || selectedTools.length === 0;

    let html = `
      <div class="ca-tool-preset-bar">
        <label><input type="radio" name="ca-tool-scope" value="all" ${isAllSelected ? 'checked' : ''}> All Available Tools</label>
        <label><input type="radio" name="ca-tool-scope" value="custom" ${!isAllSelected ? 'checked' : ''}> Restricted Toolset</label>
      </div>
      <input type="search" id="ca-tool-filter" class="ca-filter" placeholder="Filter tools, e.g. file, web, browser" autocomplete="off" style="display:${isAllSelected ? 'none' : 'block'};">
      <div id="ca-tools-list-wrap" style="display:${isAllSelected ? 'none' : 'grid'};">
    `;

    for (const [cat, tools] of Object.entries(groups)) {
      html += `<div class="ca-tool-group">
        <div class="ca-tool-group-title">${esc(cat)}</div>
        <div class="ca-tool-group-items">`;
      for (const t of tools) {
        const always = t.name === 'write_file' || t.name === 'edit_file';   // never withheld from an agent
        const checked = always || selectedTools.includes(t.name);
        html += `
          <label class="ca-tool-item" title="${esc(always ? 'Every agent can write files, so this stays on' : t.description)}">
            <input type="checkbox" class="ca-tool-cb" value="${esc(t.name)}" ${checked ? 'checked' : ''} ${always ? 'onclick="return false" tabindex="-1" aria-label="always on"' : ''}>
            <span class="ca-tool-label">${esc(t.label || t.name)}</span>${always ? '<span class="ca-tool-on">always on</span>' : ''}
          </label>
        `;
      }
      html += `</div></div>`;
    }
    html += `</div>`;

    container.innerHTML = html;

    container.querySelectorAll('input[name="ca-tool-scope"]').forEach(radio => {
      radio.onchange = () => {
        const wrap = $('ca-tools-list-wrap');
        if (wrap) wrap.style.display = radio.value === 'custom' ? 'grid' : 'none';
        const tf = $('ca-tool-filter');
        if (tf) tf.style.display = radio.value === 'custom' ? 'block' : 'none';
      };
    });
    const tf = $('ca-tool-filter');
    if (tf) tf.oninput = () => {
      const q = tf.value.trim().toLowerCase();
      container.querySelectorAll('.ca-tool-group').forEach(g => {
        let any = false;
        g.querySelectorAll('.ca-tool-item').forEach(it => {
          const hit = !q || (it.textContent + ' ' + (it.title || '') + ' ' + g.querySelector('.ca-tool-group-title').textContent).toLowerCase().includes(q);
          it.style.display = hit ? '' : 'none';
          any = any || hit;
        });
        g.style.display = any ? '' : 'none';
      });
    };
  }

  async function saveCustomAgent() {
    const name = $('ca-name').value.trim();
    if (!name) {
      if (typeof toast === 'function') toast('Agent name is required', true);
      return;
    }
    const slug = $('ca-slug').value.trim();
    const icon = $('ca-icon').value.trim() || '🤖';
    const description = $('ca-desc').value.trim();
    const system_prompt = $('ca-prompt').value.trim();
    if (!system_prompt) {
      if (typeof toast === 'function') toast('System prompt instructions are required', true);
      return;
    }

    const scopeRadio = document.querySelector('input[name="ca-tool-scope"]:checked');
    let tool_allowlist = [];
    if (scopeRadio && scopeRadio.value === 'custom') {
      document.querySelectorAll('.ca-tool-cb:checked').forEach(cb => {
        tool_allowlist.push(cb.value);
      });
      // an empty list means "all tools" server-side -- never send it for a restricted set
      if (!tool_allowlist.length) {
        if (typeof toast === 'function') toast('Pick at least one tool, or choose "All Available Tools"', true);
        return;
      }
    }
    const tempNum = parseFloat($('ca-temp').value);

    const myDevId = (typeof getClientDeviceId === 'function') ? getClientDeviceId() : '';
    const myDevName = (typeof getDeviceDisplayLabel === 'function') ? getDeviceDisplayLabel() : '';
    const curAgent = editingAgentId ? customAgentsList.find(x => String(x.id) === String(editingAgentId)) : null;

    const payload = {
      name,
      slug: slug || undefined,
      icon,
      description,
      system_prompt,
      tool_allowlist,
      input_template: $('ca-template').value.trim(),
      preferred_lane: $('ca-lane').value,
      reasoning_effort: $('ca-effort').value,
      temperature: Number.isFinite(tempNum) ? tempNum : 0.4,
      is_public: $('ca-public').checked,
      work_dir: $('ca-workdir').value.trim(),
      device_id: (curAgent && curAgent.device_id) ? curAgent.device_id : myDevId,
      device_name: (curAgent && curAgent.device_name) ? curAgent.device_name : myDevName,
    };

    try {
      if (editingAgentId) {
        await api(`/custom-agents/${editingAgentId}`, {
          method: 'PUT',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify(payload),
        });
        if (typeof toast === 'function') toast(payload.is_public ? `Updated "${name}". Sharing needs approval before others see it.` : `Updated agent "${name}"`);
      } else {
        const created = await api('/custom-agents', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify(payload),
        });
        if (typeof toast === 'function') toast(`Created agent "${name}"`);
        setActiveCustomAgent(created);
      }
      closeAgentBuilder();
      await refreshAllCustomAgentViews();
    } catch (e) {
      if (typeof toast === 'function') toast(`Error saving agent: ${e.message}`, true);
    }
  }

  async function refreshAllCustomAgentViews() {
    await loadCustomAgents();
    if (window.parent && window.parent !== window && typeof window.parent.loadCustomAgents === 'function') {
      window.parent.loadCustomAgents();
    }
    const frame = document.getElementById('settings-frame');
    if (frame && frame.contentWindow && typeof frame.contentWindow.loadCustomAgents === 'function') {
      frame.contentWindow.loadCustomAgents();
    }
  }

  async function deleteCurrentAgent() {
    if (!editingAgentId) return;
    if (!confirm('Are you sure you want to delete this custom agent?')) return;
    try {
      await api(`/custom-agents/${editingAgentId}`, { method: 'DELETE' });
      if (activeCustomAgent && activeCustomAgent.id === editingAgentId) {
        clearActiveCustomAgent();
      }
      closeAgentBuilder();
      await refreshAllCustomAgentViews();
      if (typeof toast === 'function') toast('Agent deleted');
    } catch (e) {
      if (typeof toast === 'function') toast(`Delete failed: ${e.message}`, true);
    }
  }

  let forkingAgent = null;

  function openForkModal(agent) {
    if (!agent) return;
    forkingAgent = agent;
    const modal = $('ca-fork-modal');
    if (!modal) return;

    const myDevLabel = (typeof getDeviceDisplayLabel === 'function') ? getDeviceDisplayLabel() : 'This Device';
    const isCurDev = isAgentOnCurrentDevice(agent);

    const titleEl = $('ca-fork-modal-title');
    if (titleEl) titleEl.textContent = `Fork Agent: ${agent.name}`;

    const targetDevEl = $('ca-fork-target-device');
    if (targetDevEl) targetDevEl.textContent = `Target Device: ${myDevLabel}`;

    const sourceText = $('ca-fork-source-text');
    if (sourceText) sourceText.textContent = `${agent.icon || '🤖'} ${agent.name}`;

    const sourceReason = $('ca-fork-source-reason');
    if (sourceReason) {
      if (!isCurDev) {
        sourceReason.textContent = `🔒 Barrier Active: This agent is bound to "${agent.device_name || 'another device'}". To use it on this machine ("${myDevLabel}"), fork it and choose a local workspace directory below.`;
      } else {
        sourceReason.textContent = `Forking creates a distinct clone bound to this machine, allowing you to configure a different workspace directory or prompt.`;
      }
    }

    const nameInput = $('ca-fork-name');
    if (nameInput) nameInput.value = `${agent.name} (Fork)`;

    const workdirInput = $('ca-fork-workdir');
    if (workdirInput) workdirInput.value = '';

    modal.style.display = 'flex';
    window.focus();
    setTimeout(() => {
      if (workdirInput) workdirInput.focus();
    }, 60);
  }

  function closeForkModal() {
    const modal = $('ca-fork-modal');
    if (modal) modal.style.display = 'none';
    forkingAgent = null;
  }

  async function submitForkAgent() {
    if (!forkingAgent) return;
    const name = ($('ca-fork-name') ? $('ca-fork-name').value : '').trim() || `${forkingAgent.name} (Fork)`;
    const workDir = ($('ca-fork-workdir') ? $('ca-fork-workdir').value : '').trim();
    if (!workDir) {
      if (typeof toast === 'function') toast('Please specify a workspace path on this device', true);
      if ($('ca-fork-workdir')) $('ca-fork-workdir').focus();
      return;
    }
    const myDevId = (typeof getClientDeviceId === 'function') ? getClientDeviceId() : '';
    const myDevName = (typeof getDeviceDisplayLabel === 'function') ? getDeviceDisplayLabel() : '';
    try {
      const forked = await api(`/custom-agents/${forkingAgent.id}/fork`, {
        method: 'POST',
        headers: {
          'Content-Type': 'application/json',
          ...(typeof getDeviceHeaders === 'function' ? getDeviceHeaders() : {})
        },
        body: JSON.stringify({
          name,
          work_dir: workDir,
          device_id: myDevId,
          device_name: myDevName,
        })
      });
      closeForkModal();
      if (typeof toast === 'function') toast(`Forked & bound to this device as "${forked.name}" ⚡`);
      await refreshAllCustomAgentViews();
      selectCustomAgent(forked);
    } catch (e) {
      if (typeof toast === 'function') toast(`Fork failed: ${e.message}`, true);
    }
  }

  async function forkCustomAgent(agentId) {
    const a = customAgentsList.find(x => String(x.id) === String(agentId));
    if (a) {
      openForkModal(a);
    }
  }

  /* pick a folder with the desktop app's own dialog, else through the companion; '' when cancelled */
  async function pickFolder(cur) {
    try {
      if (window.electronAPI && typeof window.electronAPI.browseFolder === 'function') {
        return (await window.electronAPI.browseFolder(cur || '')) || '';
      }
      const r = await fetch('/control/browse_folder', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json', ...(typeof getDeviceHeaders === 'function' ? getDeviceHeaders() : {}) },
        body: JSON.stringify({ initial_dir: cur || '' }),
      });
      const d = await r.json().catch(() => ({}));
      if (r.ok && d.ok && d.path) return d.path;
      if (!r.ok) throw new Error(d.error || d.detail || ('HTTP ' + r.status));
      return '';
    } catch (e) {
      if (typeof toast === 'function') toast('Could not open the folder picker. Type the full path instead.', true);
      return '';
    }
  }
  window.pickFolder = pickFolder;

  async function browseWorkDir() {
    const field = $('ca-workdir');
    const picked = await pickFolder(field.value.trim());
    if (picked) field.value = picked;
  }

  function initUI() {
    const btn = $('btn-agent-picker');
    if (btn) {
      btn.onclick = (e) => {
        e.stopPropagation();
        togglePickerDropdown();
      };
    }

    const clearActiveBtn = $('btn-clear-active-agent');
    if (clearActiveBtn) {
      clearActiveBtn.onclick = (e) => {
        e.stopPropagation();
        clearActiveCustomAgent();
        if (typeof toast === 'function') toast('Returned to standard chat');
      };
    }

    const btnNewAgent = $('btn-new-custom-agent');
    if (btnNewAgent) {
      btnNewAgent.onclick = (e) => {
        e.stopPropagation();
        openAgentBuilder(null);
      };
    }

    // Auto-generate slug as user types name
    const nameInput = $('ca-name');
    const slugInput = $('ca-slug');
    if (nameInput && slugInput) {
      nameInput.oninput = () => {
        if (!editingAgentId) {
          slugInput.value = nameInput.value.toLowerCase().replace(/[^a-z0-9_-]/g, '-').replace(/-+/g, '-').replace(/^-|-$/g, '');
        }
      };
    }

    // Temperature slider readout
    const tempInput = $('ca-temp');
    const tempVal = $('ca-temp-val');
    if (tempInput && tempVal) {
      tempInput.oninput = () => {
        tempVal.textContent = tempInput.value;
      };
    }

    // Preset icon selector buttons
    document.querySelectorAll('.ca-icon-preset').forEach(preset => {
      preset.onclick = () => {
        const iconField = $('ca-icon');
        if (iconField) iconField.value = preset.textContent.trim();
      };
    });

    const browseBtn = $('ca-workdir-browse');
    if (browseBtn) browseBtn.onclick = browseWorkDir;

    const saveBtn = $('ca-btn-save');
    if (saveBtn) saveBtn.onclick = saveCustomAgent;

    const cancelBtn = $('ca-btn-cancel');
    if (cancelBtn) cancelBtn.onclick = closeAgentBuilder;

    const deleteBtn = $('ca-btn-delete');
    if (deleteBtn) deleteBtn.onclick = deleteCurrentAgent;

    const modalClose = $('ca-modal-close');
    if (modalClose) modalClose.onclick = closeAgentBuilder;

    const forkModalClose = $('ca-fork-modal-close');
    if (forkModalClose) forkModalClose.onclick = closeForkModal;

    const forkCancelBtn = $('ca-fork-cancel-btn');
    if (forkCancelBtn) forkCancelBtn.onclick = closeForkModal;

    const forkBrowseBtn = $('ca-fork-browse-btn');
    if (forkBrowseBtn) {
      forkBrowseBtn.onclick = async () => {
        const cur = $('ca-fork-workdir') ? $('ca-fork-workdir').value : '';
        const dir = await pickFolder(cur);
        if (dir && $('ca-fork-workdir')) $('ca-fork-workdir').value = dir;
      };
    }

    const forkSubmitBtn = $('ca-fork-submit-btn');
    if (forkSubmitBtn) forkSubmitBtn.onclick = submitForkAgent;

    // Direct click-to-focus for all field containers
    document.querySelectorAll('.ca-field').forEach(field => {
      field.addEventListener('click', (e) => {
        const input = field.querySelector('input:not([type=checkbox]):not([type=radio]), textarea, select');
        if (input && e.target !== input) {
          input.focus();
        }
      });
    });

    // Close dropdown on outside click
    document.addEventListener('click', (e) => {
      const wrap = $('custom-agent-picker-wrap');
      if (wrap && !wrap.contains(e.target)) {
        closePickerDropdown();
      }
    });

    renderSidebarAgentsList();
    loadCustomAgents();
    loadAvailableTools();
  }

  /* ---------------- Settings Page Integration ---------------- */

  function renderSettingsAgentsGrid() {
    const grid = $('settings-agents-content');
    if (!grid) return;

    const mine = customAgentsList.filter(a => a.scope === 'mine');
    if (!mine.length) {
      grid.innerHTML = '<div class="mon-empty">You have no agents yet. Click "+ New Custom Agent", or fork a starter from Customize → Agents.</div>';
      return;
    }

    let html = '';
    for (const a of mine) {
      const isStarter = a.scope === 'template';
      const canEdit = !!a.can_edit && !isStarter;
      const toolsCount = (a.tool_allowlist && a.tool_allowlist.length) ? a.tool_allowlist.length : 'All';
      html += `
        <div class="card agent-card" data-q="${esc([a.name, a.slug, a.description, a.work_dir, a.share_status].join(' ').toLowerCase())}" style="display:flex; flex-direction:column; gap:8px; padding:12px; border-radius:8px; background:var(--panel2); border:1px solid var(--border);">
          <div style="display:flex; align-items:flex-start; justify-content:space-between; gap:8px;">
            <div style="display:flex; align-items:center; gap:8px;">
              <span style="font-size:22px; line-height:1;">${esc(a.icon || '🤖')}</span>
              <div>
                <div style="font-weight:700; font-size:13px; color:var(--text);">${esc(a.name)}</div>
                <div class="mono dim" style="font-size:10.5px;">/${esc(a.slug)} ${isStarter ? '· <span class="badge ok" style="font-size:9px; padding:1px 4px;">Template</span>' : a.scope === 'shared' ? '· <span class="badge" style="font-size:9px; padding:1px 4px;">Shared</span>' : ''}</div>
                ${(a.subagent_warnings && a.subagent_warnings.length) ? `<div class="dim" style="font-size:10px; color:var(--amber, #d9a400);" title="spawn_agent sub-agents never get these tools">⚠ as a sub-agent: no ${esc(a.subagent_warnings.join(', '))}</div>` : ''}
              </div>
            </div>
            <span class="chip mono" style="font-size:10px;">${esc(a.preferred_lane || 'auto')}</span>
          </div>

          <div style="font-size:11.5px; color:var(--dim); line-height:1.4; flex:1;">
            ${esc(a.description || 'No description provided.')}
          </div>

          <div style="display:flex; flex-wrap:wrap; gap:4px; font-size:10px; color:var(--dim);">
            <span class="chip" style="font-size:9.5px;">🛠️ Tools: ${toolsCount}</span>
            ${a.work_dir ? `<span class="chip" style="font-size:9.5px;" title="${esc(a.work_dir)}">📁 ${esc(a.work_dir.split(/[\\/]/).filter(Boolean).pop() || a.work_dir)}</span>` : ''}
            ${a.share_status ? `<span class="chip ca-share-chip ca-share-${esc(a.share_status)}" style="font-size:9.5px;">${{ pending: 'Waiting for approval', approved: 'Shared', rejected: 'Not approved' }[a.share_status] || ''}</span>` : ''}
            <span class="chip" style="font-size:9.5px;">⚡ Effort: ${esc(a.reasoning_effort || 'medium')}</span>
            <span class="chip" style="font-size:9.5px;">🌡️ ${a.temperature != null ? a.temperature : 0.4}</span>
          </div>

          <div class="ca-actions">
            <button class="ca-act btn-settings-fork" data-id="${a.id}" title="Clone into your personal agents">Fork</button>
            ${canEdit ? `<button class="ca-act btn-settings-edit" data-id="${a.id}">Edit</button>` : ''}
            ${canEdit ? `<button class="ca-act danger btn-settings-del" data-id="${a.id}">Delete</button>` : ''}
            <button class="ca-act primary btn-settings-run" data-id="${a.id}">Select</button>
          </div>
        </div>
      `;
    }

    grid.innerHTML = `<input type="search" id="ca-grid-filter" class="ca-filter" placeholder="Filter your agents by name, folder or status" autocomplete="off" value="${esc(agentGridFilter)}">` + html;
    const gf = grid.querySelector('#ca-grid-filter');
    const applyGridFilter = () => {
      agentGridFilter = gf.value;
      const q = gf.value.trim().toLowerCase();
      grid.querySelectorAll('.agent-card').forEach(card => { card.style.display = !q || card.dataset.q.includes(q) ? '' : 'none'; });
    };
    gf.oninput = applyGridFilter;
    applyGridFilter();

    grid.querySelectorAll('.btn-settings-fork').forEach(b => {
      b.onclick = async () => { await forkCustomAgent(b.dataset.id); };
    });
    grid.querySelectorAll('.btn-settings-edit').forEach(b => {
      b.onclick = () => {
        const a = customAgentsList.find(x => String(x.id) === String(b.dataset.id));
        if (a) openAgentBuilder(a);
      };
    });
    grid.querySelectorAll('.btn-settings-del').forEach(b => {
      b.onclick = async () => {
        editingAgentId = parseInt(b.dataset.id);
        await deleteCurrentAgent();
      };
    });
    grid.querySelectorAll('.btn-settings-run').forEach(b => {
      b.onclick = () => {
        const a = customAgentsList.find(x => String(x.id) === String(b.dataset.id));
        if (a) {
          setActiveCustomAgent(a);
          if (typeof toast === 'function') toast(`Active agent set to ${a.name}`);
        }
      };
    });

    const createSettingsBtn = $('btn-create-agent-settings');
    if (createSettingsBtn) {
      createSettingsBtn.onclick = () => openAgentBuilder();
    }
  }

  // main window: follow selections made inside the Settings iframe
  window.addEventListener('message', (ev) => {
    if (ev.origin !== window.location.origin || !ev.data || ev.data.type !== 'custom-agent-selected') return;
    if (ev.data.id == null) { setActiveCustomAgent(null, true); return; }
    const a = customAgentsList.find(x => String(x.id) === String(ev.data.id));
    if (a) setActiveCustomAgent(a, true);
    else loadCustomAgents();
  });

  // Export globally
  window.getActiveCustomAgentId = getActiveCustomAgentId;
  window.customAgentRequestOverrides = requestOverrides;
  window.getActiveCustomAgent = getActiveCustomAgent;
  window.setActiveCustomAgent = setActiveCustomAgent;
  window.selectCustomAgent = selectCustomAgent;
  window.clearActiveCustomAgent = clearActiveCustomAgent;
  window.startAgentChatSession = startAgentChatSession;
  window.bindCustomAgentSession = bindSession;
  window.openAgentBuilder = openAgentBuilder;
  window.openForkModal = openForkModal;
  window.loadCustomAgents = loadCustomAgents;
  window.renderSidebarAgentsList = renderSidebarAgentsList;
  window.customAgentsList = () => customAgentsList;
  window.customAgents = {
    getActiveId: getActiveCustomAgentId,
    getActive: getActiveCustomAgent,
    setActive: setActiveCustomAgent,
    select: selectCustomAgent,
    clear: clearActiveCustomAgent,
    startSession: startAgentChatSession,
    bindSession,
    list: () => customAgentsList,
    openModal: openAgentBuilder,
    openFork: openForkModal,
    reload: loadCustomAgents,
    restoreForSession,
  };

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', initUI);
  } else {
    initUI();
  }
})();

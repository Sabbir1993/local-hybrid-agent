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
    return activeCustomAgent ? activeCustomAgent.id : null;
  }

  function getActiveCustomAgent() {
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

  function setActiveCustomAgent(agent, fromParentSync = false, noBind = false) {
    activeCustomAgent = agent || null;
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

  function restoreForSession(sid) {
    const aid = _sessionMap()[String(sid)];
    const a = aid != null ? customAgentsList.find(x => String(x.id) === String(aid)) : null;
    if (a) {
      if (!activeCustomAgent || activeCustomAgent.id !== a.id) setActiveCustomAgent(a, false, true);
    } else if (activeCustomAgent) {
      activeCustomAgent = null;          // not bound: plain session (keep the map as is)
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
      const found = customAgentsList.find(a => String(a.id) === String(savedId));
      if (found) {
        setActiveCustomAgent(found);
      }
    }
  }

  async function startAgentChatSession(agent) {
    setActiveCustomAgent(agent, false, true);   // bound to the new session below

    const isCurRunning = curSession && window.bgJobs && window.bgJobs.has(String(curSession.id));
    curSession = null;
    messages = [];
    try {
      localStorage.removeItem(agentMode ? 'active_agent_session_id' : 'active_chat_session_id');
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
        body: JSON.stringify({ title }),
      });
      const data = await res.json();
      if (data && data.session) {
        curSession = data.session;
        bindSession(data.session.id, agent);
        try {
          localStorage.setItem(agentMode ? 'active_agent_session_id' : 'active_chat_session_id', String(data.session.id));
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

    if (!customAgentsList.length) {
      list.innerHTML = `<div class="dim" style="font-size:11px; padding:6px 4px;">No agents found</div>`;
      return;
    }

    const myAgents = customAgentsList.filter(a => a.scope === 'mine');
    const sharedAgents = customAgentsList.filter(a => a.scope === 'shared');
    const starterAgents = customAgentsList.filter(a => a.scope === 'template');

    let html = '';

    if (myAgents.length) {
      html += `<div style="font-size:8.5px; color:var(--dim); font-weight:700; letter-spacing:0.8px; text-transform:uppercase; padding:3px 4px 1px;">My Agents</div>`;
      for (const a of myAgents) {
        const isSel = activeCustomAgent && activeCustomAgent.id === a.id;
        html += `
          <div class="sidebar-agent-row ${isSel ? 'selected' : ''}" data-agent-id="${a.id}" title="${esc(a.name)}: ${esc(a.description)}">
            <span class="sidebar-agent-icon">${esc(a.icon || '🤖')}</span>
            <div class="sidebar-agent-info">
              <div class="sidebar-agent-name">${esc(a.name)}</div>
              <div class="sidebar-agent-desc">${esc(a.description || a.preferred_lane || '')}</div>
            </div>
            <div class="sidebar-agent-actions">
              <button type="button" class="btn ghost btn-icon-xs edit-agent-btn" data-edit-id="${a.id}" title="Edit Agent">✏️</button>
            </div>
          </div>
        `;
      }
    }

    const forkable = (label, items) => {
      if (!items.length) return;
      html += `<div style="font-size:8.5px; color:var(--dim); font-weight:700; letter-spacing:0.8px; text-transform:uppercase; padding:5px 4px 1px;">${label}</div>`;
      for (const a of items) {
        const isSel = activeCustomAgent && activeCustomAgent.id === a.id;
        html += `
          <div class="sidebar-agent-row ${isSel ? 'selected' : ''}" data-agent-id="${a.id}" title="${esc(a.name)}: ${esc(a.description)}">
            <span class="sidebar-agent-icon">${esc(a.icon || '🤖')}</span>
            <div class="sidebar-agent-info">
              <div class="sidebar-agent-name">${esc(a.name)}</div>
              <div class="sidebar-agent-desc">${esc(a.description || '')}</div>
            </div>
            <div class="sidebar-agent-actions">
              <button type="button" class="btn ghost btn-icon-xs fork-agent-btn" data-fork-id="${a.id}" title="Fork / customize">🍴</button>
            </div>
          </div>
        `;
      }
    };
    forkable('Shared with me', sharedAgents);
    forkable('Starter Templates', starterAgents);

    list.innerHTML = html;

    list.querySelectorAll('.sidebar-agent-row').forEach(row => {
      row.onclick = (e) => {
        if (e.target.closest('.edit-agent-btn') || e.target.closest('.fork-agent-btn')) return;
        const aid = row.getAttribute('data-agent-id');
        const agent = customAgentsList.find(a => String(a.id) === String(aid));
        if (agent) {
          if (activeCustomAgent && String(activeCustomAgent.id) === String(aid)) {
            clearActiveCustomAgent();
            if (typeof toast === 'function') toast('Switched to General Chat');
            return;
          }
          startAgentChatSession(agent);
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
      btn.onclick = async (e) => {
        e.stopPropagation();
        const id = btn.getAttribute('data-fork-id');
        await forkCustomAgent(id);
      };
    });
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
    const starterAgents = customAgentsList.filter(a => a.scope !== 'mine');

    if (myAgents.length) {
      html += `<div class="agent-dropdown-section-label">My Custom Agents</div>`;
      for (const a of myAgents) {
        const isSel = activeCustomAgent && activeCustomAgent.id === a.id;
        html += `
          <div class="agent-picker-item ${isSel ? 'selected' : ''}" data-agent-id="${a.id}">
            <span class="agent-item-icon">${esc(a.icon || '🤖')}</span>
            <div class="agent-item-text">
              <div class="agent-item-name">${esc(a.name)}</div>
              <div class="agent-item-desc">${esc(a.description)}</div>
            </div>
            <div class="agent-item-actions">
              <button class="btn ghost btn-icon-xs edit-agent-btn" data-edit-id="${a.id}" title="Edit Agent">✏️</button>
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
          if (a) setActiveCustomAgent(a);
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
      btn.onclick = async (e) => {
        e.stopPropagation();
        closePickerDropdown();
        await forkCustomAgent(btn.dataset.forkId);
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
      <div id="ca-tools-list-wrap" style="display:${isAllSelected ? 'none' : 'grid'};">
    `;

    for (const [cat, tools] of Object.entries(groups)) {
      html += `<div class="ca-tool-group">
        <div class="ca-tool-group-title">${esc(cat)}</div>
        <div class="ca-tool-group-items">`;
      for (const t of tools) {
        const checked = selectedTools.includes(t.name);
        html += `
          <label class="ca-tool-item" title="${esc(t.description)}">
            <input type="checkbox" class="ca-tool-cb" value="${esc(t.name)}" ${checked ? 'checked' : ''}>
            <span class="ca-tool-label">${esc(t.label || t.name)}</span>
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
      };
    });
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
    };

    try {
      if (editingAgentId) {
        await api(`/custom-agents/${editingAgentId}`, {
          method: 'PUT',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify(payload),
        });
        if (typeof toast === 'function') toast(`Updated agent "${name}"`);
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

  async function forkCustomAgent(agentId) {
    try {
      const forked = await api(`/custom-agents/${agentId}/fork`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({}),
      });
      if (typeof toast === 'function') toast(`Forked as "${forked.name}"`);
      await refreshAllCustomAgentViews();
      setActiveCustomAgent(forked);
    } catch (e) {
      if (typeof toast === 'function') toast(`Fork failed: ${e.message}`, true);
    }
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

    const saveBtn = $('ca-btn-save');
    if (saveBtn) saveBtn.onclick = saveCustomAgent;

    const cancelBtn = $('ca-btn-cancel');
    if (cancelBtn) cancelBtn.onclick = closeAgentBuilder;

    const deleteBtn = $('ca-btn-delete');
    if (deleteBtn) deleteBtn.onclick = deleteCurrentAgent;

    const modalClose = $('ca-modal-close');
    if (modalClose) modalClose.onclick = closeAgentBuilder;

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

    loadCustomAgents();
    loadAvailableTools();
  }

  /* ---------------- Settings Page Integration ---------------- */

  function renderSettingsAgentsGrid() {
    const grid = $('settings-agents-content');
    if (!grid) return;

    if (!customAgentsList.length) {
      grid.innerHTML = '<div class="mon-empty">No custom agents found. Click "+ New Custom Agent" to create one.</div>';
      return;
    }

    let html = '';
    for (const a of customAgentsList) {
      const isStarter = a.scope === 'template';
      const canEdit = !!a.can_edit && !isStarter;
      const toolsCount = (a.tool_allowlist && a.tool_allowlist.length) ? a.tool_allowlist.length : 'All';
      html += `
        <div class="card agent-card" style="display:flex; flex-direction:column; gap:8px; padding:12px; border-radius:8px; background:var(--panel2); border:1px solid var(--border);">
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
            <span class="chip" style="font-size:9.5px;">⚡ Effort: ${esc(a.reasoning_effort || 'medium')}</span>
            <span class="chip" style="font-size:9.5px;">🌡️ ${a.temperature != null ? a.temperature : 0.4}</span>
          </div>

          <div style="display:flex; align-items:center; justify-content:flex-end; gap:6px; margin-top:4px; border-top:1px solid var(--border-subtle); padding-top:8px;">
            <button class="btn ghost btn-sm btn-settings-fork" data-id="${a.id}" title="Clone into your personal agents">Fork</button>
            ${canEdit ? `<button class="btn ghost btn-sm btn-settings-edit" data-id="${a.id}">Edit</button>` : ''}
            ${canEdit ? `<button class="btn red btn-sm btn-settings-del" data-id="${a.id}">Delete</button>` : ''}
            <button class="btn blue btn-sm btn-settings-run" data-id="${a.id}">Select</button>
          </div>
        </div>
      `;
    }

    grid.innerHTML = html;

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
  window.clearActiveCustomAgent = clearActiveCustomAgent;
  window.startAgentChatSession = startAgentChatSession;
  window.openAgentBuilder = openAgentBuilder;
  window.loadCustomAgents = loadCustomAgents;
  window.customAgentsList = () => customAgentsList;
  window.customAgents = {
    getActiveId: getActiveCustomAgentId,
    getActive: getActiveCustomAgent,
    setActive: setActiveCustomAgent,
    clear: clearActiveCustomAgent,
    startSession: startAgentChatSession,
    list: () => customAgentsList,
    openModal: openAgentBuilder,
    reload: loadCustomAgents,
    restoreForSession,
  };

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', initUI);
  } else {
    initUI();
  }
})();

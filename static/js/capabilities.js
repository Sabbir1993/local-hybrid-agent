/* ---------------- capabilities panel ---------------- */
async function loadCapabilities() {
  const box = $('caps-content');
  if (!box) return;
  try {
    const d = await (await fetch('/control/capabilities')).json();
    if (d.error) { box.innerHTML = '<div class="mon-empty">' + esc(d.error) + '</div>'; return; }
    let h = `<div class="rep-cell" style="display:flex; align-items:center; justify-content:space-between;"><span><b>${d.total_tools}</b> tools registered</span></div>`;
    // Web
    h += capSection('web', '🌐 Web Browsing', d.web.enabled,
      (d.web.tools || []).map(t => `
        <div class="cap-tool-entry">
          <span class="cap-tool-badge web"><span class="tool-badge-ico">🌐</span><code>${esc(t.name)}</code></span>
          <span class="cap-tool-desc">${esc(t.description || '')}</span>
        </div>`).join(''),
      'web_fetch pulls page text; web_search queries DuckDuckGo (keyless)');

    // Skills
    h += capSection('skills', '🎯 Skills', d.skills.enabled,
      (d.skills.items || []).map(s => `
        <div class="cap-tool-entry">
          <span class="cap-tool-badge skill"><span class="tool-badge-ico">🎯</span><code>${esc(s.name)}</code></span>
          <span class="cap-tool-desc">${esc(s.description || '')}</span>
        </div>`).join('') || '<div class="cap-item dim" style="padding:4px 6px;">none in skills/ yet</div>',
      'Reusable instruction packs loaded from .agents/skills/*/SKILL.md');

    // MCP
    const canManageMcp = !!(window.hasPerm && window.hasPerm('settings.orchestration.configure'));
    const mcpInner = (d.mcp.servers || []).length
      ? d.mcp.servers.map(s => `
          <div class="cap-item" style="margin-bottom:6px;">
            <div style="display:flex; align-items:center; gap:6px; margin-bottom:4px;">
              <span class="cap-dot ${s.status === 'ready' ? 'on' : (s.status === 'error' ? 'err' : '')}" title="${esc(s.status)}"></span>
              <b>${esc(s.name)}</b> <span class="dim">(${esc(s.transport)}) · ${s.status === 'ready' ? s.tools.length + ' tool(s)' : esc(s.status)}</span>
              ${canManageMcp ? `<span style="margin-left:auto; display:flex; gap:4px;">
                <button class="btn ghost mcp-srv-reconnect" data-name="${esc(s.name)}" style="width:auto; margin:0; padding:1px 8px; font-size:10px;" title="Reconnect (e.g. after finishing an OAuth login)">↻</button>
                <button class="btn ghost mcp-srv-edit" data-name="${esc(s.name)}" style="width:auto; margin:0; padding:1px 8px; font-size:10px;">Edit</button>
                <button class="btn ghost mcp-srv-del" data-name="${esc(s.name)}" style="width:auto; margin:0; padding:1px 8px; font-size:10px; color:var(--red);">✕</button>
              </span>` : ''}
            </div>
            ${s.error ? `<div class="dim" style="font-size:10px; color:var(--red); margin-bottom:4px;">${esc(s.error)}</div>` : ''}
            ${(s.tools || []).map(t => `
              <div class="cap-tool-entry sub">
                <span class="cap-tool-badge mcp"><span class="tool-badge-ico">🔌</span><code>${esc(t.name)}</code></span>
                ${t.description ? `<span class="cap-tool-desc">${esc(t.description)}</span>` : ''}
              </div>`).join('')}
          </div>`).join('')
      : '<div class="cap-item dim" style="padding:4px 6px;">no servers configured yet</div>';
    h += capSection('mcp', '🔌 MCP Servers', d.mcp.enabled,
      mcpInner + (canManageMcp ? mcpEditorHtml() : ''),
      'External tool servers via Model Context Protocol (stdio / http)');

    // Plugins
    const canManagePlugins = canManageMcp;
    h += capSection('plugins', '🧩 Plugins', d.plugins.enabled,
      ((d.plugins.items || []).map(p => pluginRowHtml(p, canManagePlugins)).join('')
        || '<div class="cap-item dim" style="padding:4px 6px;">no plugins installed yet</div>')
        + (canManagePlugins ? pluginBrowserHtml() : ''),
      'Python modules loaded from plugins/*/plugin.py · catalog = reviewed bundles in plugin_catalog/');

    // Shell
    const sh = d.shell || {};
    const canConfigureGlobal = !!(window.hasPerm && window.hasPerm('settings.shell.configure'));
    const shellInner = `
      <div class="cap-tool-entry" style="margin-bottom:6px;">
        <span class="cap-tool-badge shell"><span class="tool-badge-ico">⌨️</span><code>run_shell</code></span>
        <span class="cap-tool-desc">Workspace shell command execution</span>
      </div>
      <div class="cap-item">
        <label style="display:flex; align-items:center; gap:6px; cursor:${canConfigureGlobal ? 'pointer' : 'default'};">
          <input type="checkbox" id="shell-ask" ${sh.ask_first ? 'checked' : ''} ${canConfigureGlobal ? '' : 'disabled'} style="accent-color:var(--green);">
          <b>Ask before running</b> <span class="dim">(permission modal; matched patterns skip asking${canConfigureGlobal ? '' : ' — admin-only setting'})</span>
        </label>
      </div>
      <div class="cap-item">
        <b>Global allowed patterns</b> <span class="dim">(wildcards ok, <code>*</code> = allow all — admin-managed, applies to every user)</span>
        <div id="shell-pats" style="display:flex; flex-direction:column; gap:3px; margin-top:4px;">
          ${(sh.allow_patterns || []).map((p, i) => `
            <div style="display:flex; gap:5px; align-items:center;">
              <input type="text" class="shell-pat" data-i="${i}" value="${esc(p)}" ${canConfigureGlobal ? '' : 'disabled'} style="flex:1; background:var(--bg-input); color:var(--text); border:1px solid var(--border); border-radius:5px; padding:3px 7px; font-size:10.5px; font-family:monospace;">
              ${canConfigureGlobal ? `<button class="btn ghost shell-pat-del" data-i="${i}" style="width:auto; margin:0; padding:2px 7px; font-size:10px; color:var(--red);">✕</button>` : ''}
            </div>`).join('') || '<div class="dim" style="font-size:10.5px;">(none)</div>'}
        </div>
        ${canConfigureGlobal ? `
        <div style="display:flex; gap:5px; margin-top:5px;">
          <input type="text" id="shell-pat-new" placeholder="e.g. git * or npx skills * or *" style="flex:1; background:var(--bg-input); color:var(--text); border:1px solid var(--border); border-radius:5px; padding:3px 7px; font-size:10.5px; font-family:monospace;">
          <button class="btn ghost" id="shell-pat-add" style="width:auto; margin:0; padding:3px 10px; font-size:10.5px;">+ Add</button>
        </div>
        <button class="btn accent" id="shell-pats-save" style="width:auto; margin:6px 0 0; padding:4px 12px; font-size:10.5px;">💾 Save patterns</button>
        ` : `<p class="dim" style="font-size:10px; margin:5px 0 0;">Only an admin can edit the global list. Use the shell permission prompt's "Always allow for me" to add your own.</p>`}
      </div>`;
    h += capSection('shell', '⌨️ Shell Execution', sh.enabled, shellInner,
      'run_shell tool — agent runs commands like "npx skills add …" in the workspace');

    // Agent Task step cap (no on/off toggle - always bounded)
    const ag = d.agent || {};
    h += `<div class="cap-group" style="margin-top:10px;">
      <div class="cap-head"><span>🤖 Agent Task</span></div>
      <div class="cap-body">
        <div style="display:flex; align-items:center; gap:6px;">
          <span>Max steps per run</span>
          <input type="number" id="agent-max-steps" min="${ag.min || 5}" max="${ag.max || 200}" value="${ag.max_steps || 60}"
            ${canManageMcp ? '' : 'disabled'} style="width:64px; margin:0; background:var(--bg-input); color:var(--text); border:1px solid var(--border); border-radius:5px; padding:2px 6px; font-size:11px;">
          ${canManageMcp ? '<button class="btn accent" id="agent-steps-save" style="width:auto; margin:0; padding:3px 10px; font-size:10.5px;">Save</button>' : ''}
          <span class="dim" style="font-size:9.5px;">${ag.min || 5}–${ag.max || 200}</span>
        </div>
        <div class="dim" style="font-size:9.5px; margin-top:4px;">At the cap a run pauses with a Continue button; runs repeating the same tool calls stop early.</div>
      </div>
    </div>`;

    // Agent Library (.agents/agents + .agents/commands), admin-managed allow/deny
    let lib = null;
    try { lib = await (await fetch('/control/agent_library')).json(); } catch (e) {}
    const canManageLib = !!(window.hasPerm && window.hasPerm('settings.agents.configure'));
    if (lib && !lib.error) h += agentLibraryHtml(lib, canManageLib);

    box.innerHTML = h;
    if (lib && !lib.error && canManageLib) wireAgentLibrary(box, lib);
    if (canManageMcp) wireMcpEditor(box);
    if (canManagePlugins) wirePlugins(box);
    scheduleMcpStatusPoll(d.mcp.servers || []);
    box.querySelectorAll('.cap-toggle').forEach(t => {
      t.onclick = async () => {
        const section = t.dataset.section;
        const enable = !t.classList.contains('on');
        try {
          const r = await fetch('/control/capabilities', {
            method: 'POST', headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ section, enabled: enable }),
          });
          const j = await r.json();
          if (!r.ok) throw new Error(j.error || r.status);
          toast(`Capability '${section}' ${enable ? 'enabled' : 'disabled'} ✓`);
          loadCapabilities();
        } catch (e) { toast('Toggle failed: ' + e.message, true); }
      };
    });

    const stepsBtn = box.querySelector('#agent-steps-save');
    if (stepsBtn) stepsBtn.onclick = async () => {
      try {
        const r = await fetch('/control/agent_settings', {
          method: 'POST', headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ max_steps: parseInt(box.querySelector('#agent-max-steps').value, 10) || 60 }),
        });
        const j = await r.json();
        if (!r.ok) throw new Error(j.error || j.detail || r.status);
        box.querySelector('#agent-max-steps').value = j.max_steps;
        toast(`Agent Task cap set to ${j.max_steps} steps ✓`);
      } catch (e) { toast('Save failed: ' + e.message, true); }
    };

    // shell section controls
    const askEl = box.querySelector('#shell-ask');
    if (askEl) askEl.onchange = async () => {
      try {
        const r = await fetch('/control/shell_settings', {
          method: 'POST', headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ ask_first: askEl.checked }),
        });
        if (!r.ok) throw new Error('save failed');
        toast(askEl.checked ? 'Shell: will ask before unmatched commands' : 'Shell: no permission prompts');
      } catch (e) { toast('Save failed: ' + e.message, true); }
    };
    const addBtn = box.querySelector('#shell-pat-add');
    const newPat = box.querySelector('#shell-pat-new');
    if (addBtn && newPat) addBtn.onclick = () => {
      if (!newPat.value.trim()) return;
      const wrap = box.querySelector('#shell-pats');
      const i = wrap.querySelectorAll('.shell-pat').length;
      const row = document.createElement('div');
      row.style.cssText = 'display:flex; gap:5px; align-items:center;';
      row.innerHTML = `<input type="text" class="shell-pat" value="${esc(newPat.value.trim())}" style="flex:1; background:var(--bg-input); color:var(--text); border:1px solid var(--border); border-radius:5px; padding:3px 7px; font-size:10.5px; font-family:monospace;">
        <button class="btn ghost shell-pat-del" style="width:auto; margin:0; padding:2px 7px; font-size:10px; color:var(--red);">✕</button>`;
      wrap.appendChild(row);
      newPat.value = '';
      wirePatDel(row.querySelector('.shell-pat-del'));
    };
    const saveBtn = box.querySelector('#shell-pats-save');
    if (saveBtn) saveBtn.onclick = async () => {
      const pats = [...box.querySelectorAll('.shell-pat')].map(i => i.value.trim()).filter(Boolean);
      try {
        const r = await fetch('/control/shell_settings', {
          method: 'POST', headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ allow_patterns: pats }),
        });
        if (!r.ok) throw new Error('save failed');
        toast(`Saved ${pats.length} shell pattern(s) ✓`);
      } catch (e) { toast('Save failed: ' + e.message, true); }
    };
    function wirePatDel(btn) {
      btn.onclick = () => btn.closest('div[style]').remove();
    }
    box.querySelectorAll('.shell-pat-del').forEach(wirePatDel);
  } catch (e) {
    box.innerHTML = '<div class="mon-empty">Failed: ' + esc(e.message) + '</div>';
  }
}

/* ---------------- Agent Library: profiles + prompt commands, allow/deny ---------------- */
const LIB_STATE_STYLE = {
  allowed: 'color:var(--green);', denied: 'color:var(--red);', shadowed: 'opacity:0.6;',
};

function libGlob(p) {
  const rx = String(p).toLowerCase().replace(/[.+^${}()|[\]\\]/g, '\\$&').replace(/\*/g, '.*').replace(/\?/g, '.');
  return new RegExp('^' + rx + '$');
}

function agentLibraryHtml(lib, canEdit) {
  const row = (kind, it) => {
    const warn = (it.warnings || []).length
      ? `<div class="dim" style="font-size:9.5px; flex-basis:100%;">⚠ ${esc(it.warnings.join('; '))}</div>` : '';
    const btn = canEdit && it.state !== 'shadowed'
      ? `<button class="btn ghost lib-flip" data-kind="${kind}" data-name="${esc(it.name)}" data-state="${it.state}"
           style="width:auto; margin:0 0 0 auto; padding:1px 8px; font-size:10px;">${it.state === 'allowed' ? 'Deny' : 'Allow'}</button>` : '';
    const title = it.state === 'shadowed' ? 'a built-in command, skill or role with the same name takes priority' : '';
    return `<div class="cap-tool-entry" style="flex-wrap:wrap;">
        <span class="cap-tool-badge skill"><span class="tool-badge-ico">${kind === 'agents' ? '🤖' : '📚'}</span><code>${esc(it.name)}</code></span>
        <span style="font-size:10px; ${LIB_STATE_STYLE[it.state] || ''}" title="${title}">${esc(it.state)}</span>
        ${btn}
        <span class="cap-tool-desc" style="flex-basis:100%;">${esc(it.description || '')}</span>
        ${warn}
      </div>`;
  };
  const order = { allowed: 0, denied: 1, shadowed: 2 };
  const sorted = arr => [...(arr || [])].sort((a, b) => (order[a.state] - order[b.state]) || a.name.localeCompare(b.name));
  const count = arr => (arr || []).filter(x => x.state === 'allowed').length;
  const inp = 'background:var(--bg-input); color:var(--text); border:1px solid var(--border); border-radius:5px; font-size:10.5px;';
  const listEd = (kind, key) => `
    <div style="margin-top:4px;"><b style="font-size:10.5px;">${kind} · ${key}</b> <span class="dim" style="font-size:9.5px;">(names or wildcards, one per line)</span>
      <textarea class="lib-list" data-kind="${kind}" data-key="${key}" rows="3"
        style="width:100%; box-sizing:border-box; ${inp} padding:3px 7px; font-family:monospace;">${esc(((lib.config[kind] || {})[key] || []).join('\n'))}</textarea>
    </div>`;
  const inner = `
    <div class="cap-item">
      <label style="display:flex; align-items:center; gap:6px;">
        <b>Default for unlisted files</b>
        <select id="lib-policy" ${canEdit ? '' : 'disabled'} style="${inp}">
          <option value="deny" ${lib.default_policy !== 'allow' ? 'selected' : ''}>deny</option>
          <option value="allow" ${lib.default_policy === 'allow' ? 'selected' : ''}>allow</option>
        </select>
        <span class="dim" style="font-size:9.5px;">deny always wins over allow${canEdit ? '' : ' — admin-only setting'}</span>
      </label>
    </div>
    <details class="cap-item"><summary><b>🤖 Agent profiles</b> <span class="dim">(${count(lib.agents)}/${(lib.agents || []).length} allowed · spawn_agent roles)</span></summary>
      ${sorted(lib.agents).map(it => row('agents', it)).join('') || '<div class="dim">none in .agents/agents/</div>'}
    </details>
    <details class="cap-item"><summary><b>📚 Prompt commands</b> <span class="dim">(${count(lib.commands)}/${(lib.commands || []).length} allowed · /slash in Agent mode)</span></summary>
      ${sorted(lib.commands).map(it => row('commands', it)).join('') || '<div class="dim">none in .agents/commands/</div>'}
    </details>
    ${canEdit ? `<details class="cap-item"><summary><b>Edit allow / deny lists</b></summary>
      ${listEd('agents', 'allow')}${listEd('agents', 'deny')}${listEd('commands', 'allow')}${listEd('commands', 'deny')}
      <button class="btn accent" id="lib-save" style="width:auto; margin:6px 0 0; padding:4px 12px; font-size:10.5px;">💾 Save lists</button>
    </details>` : ''}`;
  // own toggle class: the generic .cap-toggle handler posts to /control/capabilities
  return `<div class="cap-group">
    <div class="cap-head">
      <span>📚 Agent Library</span>
      <button class="cap-toggle-lib ${lib.enabled ? 'on' : ''}" ${canEdit ? '' : 'disabled'}
        title="${canEdit ? 'Enable/disable the Agent Library' : 'Admin-only setting'}">${lib.enabled ? 'ON' : 'OFF'}</button>
    </div>
    <div class="cap-body" style="${lib.enabled ? '' : 'opacity:0.45;'}">${inner}
      <div class="dim" style="font-size:9.5px; margin-top:4px;">Agent profiles from .agents/agents and prompt commands from .agents/commands · org-wide, admin-managed · every change is audited</div>
    </div>
  </div>`;
}

async function saveAgentLibrary(body) {
  const r = await fetch('/control/agent_library', {
    method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body),
  });
  const j = await r.json().catch(() => ({}));
  if (!r.ok) throw new Error(j.error || j.detail || r.status);
  if (window.refreshLibraryCommands) window.refreshLibraryCommands();
  return j;
}

function wireAgentLibrary(box, lib) {
  const run = async (body, msg) => {
    try { await saveAgentLibrary(body); toast(msg); loadCapabilities(); }
    catch (e) { toast('Save failed: ' + e.message, true); }
  };
  const tog = box.querySelector('.cap-toggle-lib');
  if (tog) tog.onclick = () => run({ enabled: !lib.enabled }, `Agent Library ${lib.enabled ? 'disabled' : 'enabled'} ✓`);
  const pol = box.querySelector('#lib-policy');
  if (pol) pol.onchange = () => run({ default_policy: pol.value }, `Default policy: ${pol.value} ✓`);
  box.querySelectorAll('.lib-flip').forEach(b => b.onclick = () => {
    const kind = b.dataset.kind, name = b.dataset.name, lname = name.toLowerCase();
    const cur = lib.config[kind] || { allow: [], deny: [] };
    const allow = (cur.allow || []).filter(p => String(p).toLowerCase() !== lname);
    const deny = (cur.deny || []).filter(p => String(p).toLowerCase() !== lname);
    if (b.dataset.state === 'allowed') {
      deny.push(name);
    } else {
      const blocker = deny.find(p => libGlob(p).test(lname));
      if (blocker) { toast(`'${name}' is blocked by the deny pattern '${blocker}' — edit the deny list`, true); return; }
      allow.push(name);
    }
    run({ [kind]: { allow, deny } }, `${name}: ${b.dataset.state === 'allowed' ? 'denied' : 'allowed'} ✓`);
  });
  const save = box.querySelector('#lib-save');
  if (save) save.onclick = () => {
    const body = { agents: {}, commands: {} };
    box.querySelectorAll('.lib-list').forEach(t => {
      body[t.dataset.kind][t.dataset.key] = t.value.split(/[\n,]/).map(x => x.trim()).filter(Boolean);
    });
    run(body, 'Agent Library lists saved ✓');
  };
}

function capSection(id, title, enabled, innerHtml, note) {
  return `<div class="cap-group">
    <div class="cap-head">
      <span>${title}</span>
      <button class="cap-toggle ${enabled ? 'on' : ''}" data-section="${id}" title="Enable/disable this capability">${enabled ? 'ON' : 'OFF'}</button>
    </div>
    <div class="cap-body" style="${enabled ? '' : 'opacity:0.45;'}">${innerHtml || ''}${note ? `<div class="dim" style="font-size:9.5px; margin-top:4px;">${esc(note)}</div>` : ''}</div>
  </div>`;
}

/* ---------------- MCP custom servers: add / edit / remove / paste JSON ---------------- */
const MCP_INP = 'background:var(--bg-input); color:var(--text); border:1px solid var(--border); border-radius:5px; padding:3px 7px; font-size:10.5px; font-family:monospace;';
let _mcpStatusTimer = null;
let _mcpConfigs = {};   // name -> public config from /mcp/servers (no secret values)

function mcpEditorHtml() {
  return `
    <div style="display:flex; gap:5px; margin-top:6px;">
      <button class="btn ghost" id="mcp-add-open" style="width:auto; margin:0; padding:3px 10px; font-size:10.5px;">+ Add MCP server</button>
      <button class="btn ghost" id="mcp-import-open" style="width:auto; margin:0; padding:3px 10px; font-size:10.5px;">Paste JSON</button>
    </div>
    <div id="mcp-import" class="cap-item" hidden style="border:1px solid var(--border); border-radius:6px; padding:6px 8px; margin-top:5px;">
      <div class="dim" style="font-size:10px; margin-bottom:4px;">Claude-Desktop format: <code>{"mcpServers": {"name": {"command": "npx", "args": [...]}}}</code></div>
      <textarea id="mcp-import-json" rows="7" style="${MCP_INP} width:100%; box-sizing:border-box;" placeholder='{"mcpServers": {"my-server": {"command": "npx", "args": ["-y", "mcp-remote", "https://example.com/mcp"]}}}'></textarea>
      <div style="display:flex; gap:5px; margin-top:5px;">
        <button class="btn accent" id="mcp-import-save" style="width:auto; margin:0; padding:3px 12px; font-size:10.5px;">Import &amp; connect</button>
        <button class="btn ghost" id="mcp-import-cancel" style="width:auto; margin:0; padding:3px 10px; font-size:10.5px;">Cancel</button>
      </div>
      <div id="mcp-import-result" class="dim" style="font-size:10px; margin-top:4px;"></div>
    </div>
    <div id="mcp-form" class="cap-item" hidden style="border:1px solid var(--border); border-radius:6px; padding:6px 8px; margin-top:5px;">
     <div style="display:flex; flex-direction:column; gap:5px;">
      <b id="mcp-form-title">Add MCP server</b>
      <label style="display:flex; gap:6px; align-items:center;"><span style="width:70px;">Name</span>
        <input type="text" id="mcp-f-name" placeholder="e.g. sslcommerz" style="${MCP_INP} flex:1;"></label>
      <label style="display:flex; gap:6px; align-items:center;"><span style="width:70px;">Transport</span>
        <select id="mcp-f-transport" style="${MCP_INP} flex:1;">
          <option value="stdio">stdio (local command, e.g. npx mcp-remote)</option>
          <option value="http">http (streamable-HTTP URL)</option>
        </select></label>
      <div id="mcp-f-stdio" style="display:flex; flex-direction:column; gap:5px;">
        <label style="display:flex; gap:6px; align-items:center;"><span style="width:70px;">Command</span>
          <input type="text" id="mcp-f-command" placeholder="npx" style="${MCP_INP} flex:1;"></label>
        <label style="display:flex; gap:6px; align-items:flex-start;"><span style="width:70px;">Args</span>
          <textarea id="mcp-f-args" rows="3" placeholder="one per line, e.g.&#10;-y&#10;mcp-remote&#10;https://example.com/mcp" style="${MCP_INP} flex:1;"></textarea></label>
        <div id="mcp-f-allowed" class="dim" style="font-size:9.5px; margin-left:76px;"></div>
      </div>
      <label id="mcp-f-http" style="display:none; gap:6px; align-items:center;"><span style="width:70px;">URL</span>
        <input type="text" id="mcp-f-url" placeholder="https://example.com/mcp" style="${MCP_INP} flex:1;"></label>
      <div>
        <div style="display:flex; align-items:center; gap:6px;"><span style="width:70px;">Env vars</span>
          <button class="btn ghost" id="mcp-f-env-add" style="width:auto; margin:0; padding:1px 8px; font-size:10px;">+ var</button>
          <span class="dim" style="font-size:9.5px;">secret values go to the OS keychain, never config/app.json</span></div>
        <div id="mcp-f-env" style="display:flex; flex-direction:column; gap:3px; margin-top:3px;"></div>
      </div>
      <label style="display:flex; gap:6px; align-items:center; cursor:pointer;">
        <input type="checkbox" id="mcp-f-disabled" style="accent-color:var(--green);"> Disabled (saved, not started)</label>
      <div style="display:flex; gap:5px;">
        <button class="btn accent" id="mcp-f-save" style="width:auto; margin:0; padding:3px 12px; font-size:10.5px;">💾 Save &amp; connect</button>
        <button class="btn ghost" id="mcp-f-cancel" style="width:auto; margin:0; padding:3px 10px; font-size:10.5px;">Cancel</button>
      </div>
     </div>
    </div>`;
}

function mcpEnvRow(key = '', value = '', secret = false, stored = false) {
  const row = document.createElement('div');
  row.className = 'mcp-env-row';
  row.style.cssText = 'display:flex; gap:4px; align-items:center;';
  row.innerHTML = `
    <input type="text" class="mcp-env-key" value="${esc(key)}" placeholder="KEY" style="${MCP_INP} width:130px;">
    <input type="${secret ? 'password' : 'text'}" class="mcp-env-val" value="${esc(value)}" autocomplete="new-password"
      placeholder="${stored ? '•••• stored — leave blank to keep' : 'value'}" style="${MCP_INP} flex:1;">
    <label class="dim" style="font-size:10px; display:flex; gap:3px; align-items:center; cursor:pointer;">
      <input type="checkbox" class="mcp-env-secret" ${secret ? 'checked' : ''} ${stored ? 'disabled' : ''}> secret</label>
    <button class="btn ghost mcp-env-del" style="width:auto; margin:0; padding:1px 7px; font-size:10px; color:var(--red);">✕</button>`;
  row.querySelector('.mcp-env-secret').onchange = e => {
    row.querySelector('.mcp-env-val').type = e.target.checked ? 'password' : 'text';
  };
  row.querySelector('.mcp-env-del').onclick = () => row.remove();
  return row;
}

function wireMcpEditor(box) {
  const form = box.querySelector('#mcp-form');
  const imp = box.querySelector('#mcp-import');
  const $f = id => box.querySelector('#mcp-f-' + id);
  let editing = null;

  fetch('/mcp/servers').then(r => r.json()).then(d => {
    _mcpConfigs = {};
    (d.servers || []).forEach(s => { _mcpConfigs[s.name] = s; });
    $f('allowed').textContent = 'allowed commands: ' + (d.allowed_commands || []).join(', ');
  }).catch(() => {});

  const syncTransport = () => {
    const http = $f('transport').value === 'http';
    $f('stdio').style.display = http ? 'none' : 'flex';
    $f('http').style.display = http ? 'flex' : 'none';
  };
  $f('transport').onchange = syncTransport;
  $f('env-add').onclick = () => $f('env').appendChild(mcpEnvRow());

  const openForm = cfg => {
    editing = cfg ? cfg.name : null;
    imp.hidden = true;
    box.querySelector('#mcp-form-title').textContent = cfg ? `Edit MCP server: ${cfg.name}` : 'Add MCP server';
    $f('name').value = cfg ? cfg.name : '';
    $f('name').disabled = !!cfg;
    $f('transport').value = cfg ? cfg.transport : 'stdio';
    $f('command').value = cfg ? (cfg.command || '') : 'npx';
    $f('args').value = cfg ? (cfg.args || []).join('\n') : '';
    $f('url').value = cfg ? (cfg.url || '') : '';
    $f('disabled').checked = !!(cfg && cfg.disabled);
    $f('env').innerHTML = '';
    if (cfg) {
      Object.entries(cfg.env || {}).forEach(([k, v]) => $f('env').appendChild(mcpEnvRow(k, v, false)));
      (cfg.secret_env_keys || []).forEach(k => $f('env').appendChild(mcpEnvRow(k, '', true, true)));
    }
    syncTransport();
    form.hidden = false;
    $f('name').focus();
  };

  box.querySelector('#mcp-add-open').onclick = () => openForm(null);
  $f('cancel').onclick = () => { form.hidden = true; };
  box.querySelector('#mcp-import-open').onclick = () => { form.hidden = true; imp.hidden = !imp.hidden; };
  box.querySelector('#mcp-import-cancel').onclick = () => { imp.hidden = true; };

  $f('save').onclick = async () => {
    const env = {}, secret_env = {};
    for (const row of $f('env').querySelectorAll('.mcp-env-row')) {
      const k = row.querySelector('.mcp-env-key').value.trim();
      if (!k) continue;
      const v = row.querySelector('.mcp-env-val').value;
      if (row.querySelector('.mcp-env-secret').checked) secret_env[k] = v; else env[k] = v;
    }
    const body = {
      name: $f('name').value.trim(),
      transport: $f('transport').value,
      command: $f('command').value.trim(),
      args: $f('args').value.split('\n').map(a => a.trim()).filter(Boolean),
      url: $f('url').value.trim(),
      env, secret_env,
      disabled: $f('disabled').checked,
    };
    try {
      const r = await fetch(editing ? `/mcp/servers/${encodeURIComponent(editing)}` : '/mcp/servers', {
        method: editing ? 'PUT' : 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(body),
      });
      const j = await r.json();
      if (!r.ok) throw new Error(j.error || j.detail || r.status);
      toast(`MCP server '${body.name}' saved — ${j.status.status}…`);
      loadCapabilities();
    } catch (e) { toast('Save failed: ' + e.message, true); }
  };

  box.querySelector('#mcp-import-save').onclick = async () => {
    const out = box.querySelector('#mcp-import-result');
    let config;
    try { config = JSON.parse(box.querySelector('#mcp-import-json').value); }
    catch (e) { out.textContent = 'Invalid JSON: ' + e.message; return; }
    try {
      const r = await fetch('/mcp/import', {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ config }),
      });
      const j = await r.json();
      if (!r.ok) throw new Error(j.error || j.detail || r.status);
      const bad = j.results.filter(x => x.error);
      const ok = j.results.length - bad.length;
      if (bad.length) {
        out.innerHTML = bad.map(x => `<div style="color:var(--red);">${esc(x.name)}: ${esc(x.error)}</div>`).join('');
        toast(`Imported ${ok}, ${bad.length} failed`, true);
        if (ok) setTimeout(loadCapabilities, 1500);
      } else {
        toast(`Imported ${ok} MCP server(s) — connecting…`);
        loadCapabilities();
      }
    } catch (e) { out.textContent = 'Import failed: ' + e.message; }
  };

  box.querySelectorAll('.mcp-srv-edit').forEach(btn => {
    btn.onclick = () => {
      const cfg = _mcpConfigs[btn.dataset.name];
      if (!cfg) { toast('Config not loaded yet — try again', true); return; }
      if (cfg.managed) { toast('Catalog connector — use Connect / Disconnect above', true); return; }
      openForm(cfg);
    };
  });
  box.querySelectorAll('.mcp-srv-reconnect').forEach(btn => {
    btn.onclick = async () => {
      try {
        const r = await fetch(`/mcp/servers/${encodeURIComponent(btn.dataset.name)}/reconnect`, { method: 'POST' });
        const j = await r.json();
        if (!r.ok) throw new Error(j.error || j.detail || r.status);
        toast(`Reconnecting ${btn.dataset.name}…`);
        loadCapabilities();
      } catch (e) { toast('Reconnect failed: ' + e.message, true); }
    };
  });
  box.querySelectorAll('.mcp-srv-del').forEach(btn => {
    btn.onclick = async () => {
      const name = btn.dataset.name;
      if (!confirm(`Remove MCP server '${name}'? Its tools disappear for every user.`)) return;
      try {
        const r = await fetch(`/mcp/servers/${encodeURIComponent(name)}`, { method: 'DELETE' });
        const j = await r.json();
        if (!r.ok) throw new Error(j.error || j.detail || r.status);
        toast(`MCP server '${name}' removed`);
        loadCapabilities();
      } catch (e) { toast('Remove failed: ' + e.message, true); }
    };
  });
}

// servers connect in the background (npx download / OAuth) - refresh until none is 'connecting',
// but never while the user has the add/import form open (a reload would wipe their input)
function scheduleMcpStatusPoll(servers) {
  if (_mcpStatusTimer) clearTimeout(_mcpStatusTimer);
  if (!servers.some(s => s.status === 'connecting' || s.status === 'idle')) return;
  _mcpStatusTimer = setTimeout(() => {
    const box = $('caps-content');
    const busy = box && [...box.querySelectorAll('#mcp-form, #mcp-import')].some(el => !el.hidden);
    if (busy) { scheduleMcpStatusPoll(servers); return; }
    loadCapabilities();
  }, 3000);
}

/* ---------------- Plugins: installed list + browsable catalog ---------------- */
const PLUGIN_BTN = 'width:auto; margin:0; padding:1px 8px; font-size:10px;';
let _pluginCatalog = [];
let _pluginCategory = 'all';

function pluginRowHtml(p, canManage) {
  const state = p.error ? 'err' : (p.loaded ? 'on' : '');
  const why = p.error ? 'failed to load' : (p.loaded ? 'loaded' : (p.enabled ? 'not loaded' : 'disabled'));
  return `
    <div class="cap-item" style="margin-bottom:6px;">
      <div style="display:flex; align-items:center; gap:6px; margin-bottom:4px;">
        <span class="cap-dot ${state}" title="${why}"></span>
        <b>${esc(p.name)}</b>
        <span class="dim">· ${p.tools.length} tool(s), ${p.prompt_fragments} prompt frag(s)${p.enabled ? '' : ' · disabled'}${p.modified ? ' · locally modified' : ''}</span>
        ${canManage ? `<span style="margin-left:auto; display:flex; gap:4px;">
          <button class="btn ghost plugin-enable" data-name="${esc(p.name)}" data-enabled="${p.enabled ? '1' : ''}" style="${PLUGIN_BTN}">${p.enabled ? 'Disable' : 'Enable'}</button>
          ${p.from_catalog && !p.modified ? `<button class="btn ghost plugin-uninstall" data-name="${esc(p.name)}" style="${PLUGIN_BTN} color:var(--red);" title="Remove from plugins/ (can be reinstalled from the catalog)">✕</button>` : ''}
        </span>` : ''}
      </div>
      ${p.error ? `<div class="dim" style="font-size:10px; color:var(--red); margin-bottom:4px;">${esc(p.error)}</div>` : ''}
      ${(p.tools || []).map(t => `
        <div class="cap-tool-entry sub">
          <span class="cap-tool-badge plugin"><span class="tool-badge-ico">⚡</span><code>${esc(t.split('__').pop())}</code></span>
        </div>`).join('')}
    </div>`;
}

function pluginBrowserHtml() {
  return `
    <div style="display:flex; gap:5px; margin-top:6px;">
      <button class="btn ghost" id="plugin-browse-open" style="width:auto; margin:0; padding:3px 10px; font-size:10.5px;">+ Browse plugins</button>
      <button class="btn ghost" id="plugin-reload" style="width:auto; margin:0; padding:3px 10px; font-size:10.5px;" title="Re-import every enabled plugin (picks up edits to plugins/*/plugin.py)">↻ Reload</button>
    </div>
    <div id="plugin-browser" class="cap-item" hidden style="border:1px solid var(--border); border-radius:6px; padding:6px 8px; margin-top:5px;">
      <div style="display:flex; gap:5px; align-items:center; margin-bottom:5px;">
        <input type="text" id="plugin-search" placeholder="Search plugins…" style="${MCP_INP} flex:1;">
        <button class="btn ghost" id="plugin-browse-close" style="${PLUGIN_BTN}">Close</button>
      </div>
      <div id="plugin-cats" style="display:flex; gap:4px; flex-wrap:wrap; margin-bottom:5px;"></div>
      <div id="plugin-cards" style="display:grid; grid-template-columns:repeat(auto-fill, minmax(220px, 1fr)); gap:6px;"></div>
      <div class="dim" style="font-size:9.5px; margin-top:5px;">Only reviewed plugins from <code>plugin_catalog/</code> can be installed here — plugins run inside the server as Python, so nothing is downloaded from the internet.</div>
    </div>`;
}

function renderPluginCatalog(box) {
  const q = (box.querySelector('#plugin-search')?.value || '').trim().toLowerCase();
  const cats = ['all', ...new Set(_pluginCatalog.map(p => p.category))];
  box.querySelector('#plugin-cats').innerHTML = cats.map(c => `
    <button class="btn ${c === _pluginCategory ? 'accent' : 'ghost'} plugin-cat" data-cat="${esc(c)}" style="${PLUGIN_BTN}">${esc(c)}</button>`).join('');
  const list = _pluginCatalog.filter(p =>
    (_pluginCategory === 'all' || p.category === _pluginCategory)
    && (!q || [p.name, p.title, p.description, ...(p.tools || [])].join(' ').toLowerCase().includes(q)));
  box.querySelector('#plugin-cards').innerHTML = list.map(p => `
    <div class="plugin-card" style="border:1px solid var(--border); border-radius:6px; padding:6px 8px; display:flex; flex-direction:column; gap:4px;">
      <div style="display:flex; align-items:center; gap:6px;">
        <span class="tool-badge-ico">⚡</span><b>${esc(p.title)}</b>
        <span class="dim" style="font-size:9.5px;">${esc(p.version ? 'v' + p.version : '')}</span>
        <span style="margin-left:auto;">${p.installed
          ? `<span class="dim" style="font-size:10px;">${p.modified ? 'installed (modified)' : 'installed ✓'}</span>`
          : `<button class="btn accent plugin-install" data-name="${esc(p.name)}" style="${PLUGIN_BTN}">Install</button>`}</span>
      </div>
      <div class="dim" style="font-size:10px;">${esc(p.description)}</div>
      <div style="display:flex; gap:3px; flex-wrap:wrap;">${(p.tools || []).map(t => `<code style="font-size:9.5px;">${esc(t)}</code>`).join('')}</div>
      <div class="dim" style="font-size:9px;">${esc(p.category)}${p.author ? ' · ' + esc(p.author) : ''}${p.sha256 ? ` · <span title="sha256 of plugin.py: ${esc(p.sha256)}">sha256 ${esc(p.sha256.slice(0, 12))}…</span>` : ''}</div>
    </div>`).join('') || '<div class="dim" style="font-size:10.5px;">no matching plugins</div>';

  box.querySelectorAll('.plugin-cat').forEach(b => {
    b.onclick = () => { _pluginCategory = b.dataset.cat; renderPluginCatalog(box); };
  });
  box.querySelectorAll('.plugin-install').forEach(b => {
    b.onclick = async () => {
      b.disabled = true; b.textContent = 'Installing…';
      try {
        const r = await fetch(`/plugins/${encodeURIComponent(b.dataset.name)}/install`, { method: 'POST' });
        const j = await r.json();
        if (!r.ok) throw new Error(j.error || r.status);
        toast(`Plugin '${b.dataset.name}' installed ✓` + (j.plugin && j.plugin.error ? ' — but failed to load' : ''), !!(j.plugin && j.plugin.error));
        await loadCapabilities();
        openPluginBrowser($('caps-content'));
      } catch (e) { toast('Install failed: ' + e.message, true); b.disabled = false; b.textContent = 'Install'; }
    };
  });
}

async function openPluginBrowser(box) {
  const panel = box.querySelector('#plugin-browser');
  if (!panel) return;
  panel.hidden = false;
  try {
    const d = await (await fetch('/plugins/catalog')).json();
    _pluginCatalog = d.plugins || [];
    renderPluginCatalog(box);
  } catch (e) {
    box.querySelector('#plugin-cards').innerHTML = '<div class="dim" style="font-size:10.5px;">catalog failed: ' + esc(e.message) + '</div>';
  }
}

function wirePlugins(box) {
  const open = box.querySelector('#plugin-browse-open');
  if (open) open.onclick = () => openPluginBrowser(box);
  const close = box.querySelector('#plugin-browse-close');
  if (close) close.onclick = () => { box.querySelector('#plugin-browser').hidden = true; };
  const search = box.querySelector('#plugin-search');
  if (search) search.oninput = () => renderPluginCatalog(box);
  const reload = box.querySelector('#plugin-reload');
  if (reload) reload.onclick = async () => {
    try {
      const r = await fetch('/plugins/reload', { method: 'POST' });
      if (!r.ok) throw new Error((await r.json()).error || r.status);
      toast('Plugins reloaded ✓');
      loadCapabilities();
    } catch (e) { toast('Reload failed: ' + e.message, true); }
  };
  box.querySelectorAll('.plugin-enable').forEach(b => {
    b.onclick = async () => {
      const enable = !b.dataset.enabled;
      try {
        const r = await fetch(`/plugins/${encodeURIComponent(b.dataset.name)}/enable`, {
          method: 'POST', headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ enabled: enable }),
        });
        if (!r.ok) throw new Error((await r.json()).error || r.status);
        toast(`Plugin '${b.dataset.name}' ${enable ? 'enabled' : 'disabled'}`);
        loadCapabilities();
      } catch (e) { toast('Update failed: ' + e.message, true); }
    };
  });
  box.querySelectorAll('.plugin-uninstall').forEach(b => {
    b.onclick = async () => {
      if (!confirm(`Uninstall plugin '${b.dataset.name}'? Its tools are removed immediately; you can reinstall it from the catalog.`)) return;
      try {
        const r = await fetch(`/plugins/${encodeURIComponent(b.dataset.name)}`, { method: 'DELETE' });
        if (!r.ok) throw new Error((await r.json()).error || r.status);
        toast(`Plugin '${b.dataset.name}' uninstalled`);
        loadCapabilities();
      } catch (e) { toast('Uninstall failed: ' + e.message, true); }
    };
  });
}

/* ---------------- MCP connector catalog (click-to-authorize) ---------------- */
let _mcpPollTimer = null;

async function loadMcpConnectors(box) {
  const mount = box.querySelector('#mcp-connectors');
  if (!mount) return;
  try {
    const d = await (await fetch('/mcp/catalog')).json();
    mount.innerHTML = (d.connectors || []).map(c => `
      <div class="cap-item mcp-connector" data-id="${esc(c.id)}" style="border:1px solid var(--border); border-radius:6px; padding:6px 8px; margin-bottom:4px;">
        <div style="display:flex; align-items:center; justify-content:space-between; gap:8px;">
          <span><span class="cap-dot ${c.connected ? 'on' : ''}"></span><b>${esc(c.name)}</b> <span class="dim">${esc(c.description || '')}</span></span>
          ${c.connected
            ? '<button class="btn ghost mcp-disconnect" style="width:auto; margin:0; padding:2px 10px; font-size:10.5px; color:var(--red);">Disconnect</button>'
            : (c.configured
                ? '<button class="btn accent mcp-connect" style="width:auto; margin:0; padding:2px 10px; font-size:10.5px;">Connect</button>'
                : '<span class="dim" style="font-size:10px;" title="Add a client_id under capabilities.mcp_catalog_overrides in config/app.json">not configured</span>')}
        </div>
        <div class="mcp-connector-status dim" style="font-size:10px; margin-top:3px;"></div>
      </div>`).join('') || '<div class="dim" style="font-size:10.5px;">no known connectors</div>';

    mount.querySelectorAll('.mcp-connect').forEach(btn => {
      btn.onclick = () => startMcpAuthorize(btn.closest('.mcp-connector'));
    });
    mount.querySelectorAll('.mcp-disconnect').forEach(btn => {
      btn.onclick = async () => {
        const id = btn.closest('.mcp-connector').dataset.id;
        try {
          const r = await fetch(`/mcp/${id}/disable`, { method: 'POST' });
          if (!r.ok) throw new Error((await r.json()).error || r.status);
          toast(`${id} disconnected`);
          loadCapabilities();
        } catch (e) { toast('Disconnect failed: ' + e.message, true); }
      };
    });
  } catch (e) {
    mount.innerHTML = '<div class="dim" style="font-size:10.5px;">connector list failed: ' + esc(e.message) + '</div>';
  }
}

async function startMcpAuthorize(card) {
  const id = card.dataset.id;
  const statusEl = card.querySelector('.mcp-connector-status');
  try {
    const r = await fetch(`/mcp/${id}/authorize/start`, { method: 'POST' });
    const d = await r.json();
    if (!r.ok) throw new Error(d.error || r.status);
    statusEl.innerHTML = `Go to <a href="${esc(d.verification_uri)}" target="_blank" rel="noopener">${esc(d.verification_uri)}</a> and enter code <b class="mono">${esc(d.user_code)}</b> — waiting for approval…`;
    if (_mcpPollTimer) clearInterval(_mcpPollTimer);
    const deadline = Date.now() + d.expires_in * 1000;
    _mcpPollTimer = setInterval(async () => {
      if (Date.now() > deadline) {
        clearInterval(_mcpPollTimer);
        statusEl.textContent = 'Authorization expired — try again.';
        return;
      }
      try {
        const pr = await (await fetch(`/mcp/${id}/authorize/poll?session=${encodeURIComponent(d.session)}`)).json();
        if (pr.status === 'success') {
          clearInterval(_mcpPollTimer);
          toast(`${id} connected ✓`);
          loadCapabilities();
        } else if (pr.status === 'expired' || pr.status === 'denied' || pr.status === 'error') {
          clearInterval(_mcpPollTimer);
          statusEl.textContent = `Authorization ${pr.status}${pr.error ? ': ' + pr.error : ''}.`;
        }
      } catch { /* transient network hiccup, keep polling */ }
    }, (d.interval || 5) * 1000);
  } catch (e) {
    statusEl.textContent = 'Failed to start: ' + e.message;
  }
}


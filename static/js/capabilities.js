/* ---------------- capabilities panel ---------------- */
async function loadCapabilities() {
  const box = $('caps-content');
  if (!box) return;
  try {
    const d = await (await fetch('/control/capabilities')).json();
    if (d.error) { box.innerHTML = '<div class="mon-empty">' + esc(d.error) + '</div>'; return; }
    let _capActiveTab = window._capTab || 'memory';
    const tabBtn = (id, label, enabled) => {
      let badge = '';
      if (enabled === true) badge = `<span class="cap-pill-badge on">ON</span>`;
      else if (enabled === false) badge = `<span class="cap-pill-badge off">OFF</span>`;
      return `<button type="button" role="tab" class="cap-tab" data-tab="${id}"><span>${label}</span>${badge}</button>`;
    };

    let h = `<div class="rep-bar" style="display:flex; align-items:center; justify-content:space-between; margin-bottom:12px;"><span><b>${d.total_tools}</b> tools registered</span></div>`;
    
    // Navigation: grouped by what the person is doing, not one long row of tabs
    const navGroups = [
      ['About you', 'Notes that follow you between chats', [['memory', '🧠 My memory', null]]],
      ['What the agent can use', 'Switch abilities on or off', [
        ['web', '🌐 Web', d.web.enabled], ['shell', '⌨️ Shell', (d.shell || {}).enabled],
        ['skills', '🎯 Skills', d.skills.enabled], ['mcp', '🔌 MCP servers', d.mcp.enabled], ['plugins', '🧩 Plugins', d.plugins.enabled]]],
      ['How it works', 'Limits, routing and shared commands', [
        ['task', '🤖 Task limits', null], ['router', '🧭 Router', null], ['library', '📚 Library', null]]],
    ];
    h += `<div class="cap-layout"><nav role="tablist" class="cap-rail" aria-label="Agent capabilities">
      ${navGroups.map(([title, sub, tabs]) => `<div class="cap-rail-group"><div class="cap-rail-title">${title}</div><div class="cap-rail-sub">${sub}</div>${tabs.map(([id, label, en]) => tabBtn(id, label, en)).join('')}</div>`).join('')}
    </nav><div class="cap-panes">`;

    // global servers: admin-managed; personal ("just for me") servers: any user, for themselves
    const canManageMcp = !!(window.hasPerm && window.hasPerm('settings.orchestration.configure'));
    const canAddMcp = canManageMcp || !!(window.hasPerm && window.hasPerm('chat.use'));

    // 1. Web Pane
    const webInner = `
      <div style="margin-bottom:12px;">
        ${(d.web.tools || []).map(t => `
          <div class="cap-tool-entry">
            <span class="cap-tool-badge web"><span class="tool-badge-ico">🌐</span><code>${esc(t.name)}</code></span>
            <span class="cap-tool-desc">${esc(t.description || '')}</span>
          </div>`).join('')}
      </div>
      <div class="cap-item" style="margin-top:12px; padding:14px; background:var(--panel2); border:1px solid var(--border); border-radius:10px;">
        <div style="display:flex; align-items:center; justify-content:space-between; margin-bottom:12px; flex-wrap:wrap; gap:8px;">
          <div>
            <b style="font-size:13px; display:flex; align-items:center; gap:6px;">📊 Thinking-Wise Tool Call &amp; Token Budget Matrix</b>
            <div class="dim" style="font-size:11px; margin-top:2px;">Configure tool/web call limits, token budget, and max rounds per thinking level (as selected in the chat prompt bar)</div>
          </div>
          ${canManageMcp ? '<button class="btn accent" id="web-budget-save" style="width:auto; margin:0; padding:5px 14px; font-size:11.5px; font-weight:600; border-radius:6px;">💾 Save Budget</button>' : ''}
        </div>
        <div style="overflow-x:auto; margin-top:8px;">
          <table class="cap-budget-matrix" style="width:100%; border-collapse:collapse; font-size:12px; min-width:480px;">
            <thead>
              <tr style="border-bottom:1px solid var(--border-subtle); color:var(--dim); font-size:11px; text-align:left;">
                <th style="padding:6px 10px; font-weight:600;">Thinking Level</th>
                <th style="padding:6px 10px; font-weight:600;">Max Tool Calls</th>
                <th style="padding:6px 10px; font-weight:600;">Token Budget</th>
                <th style="padding:6px 10px; font-weight:600;">Max Tool Rounds</th>
              </tr>
            </thead>
            <tbody>
              ${(() => {
                const matrix = d.web.thinking_budgets || {};
                const defaults = {
                  none: { tool_calls: 0, token_budget: 6000, max_rounds: 0 },
                  low: { tool_calls: 4, token_budget: 8000, max_rounds: 4 },
                  medium: { tool_calls: 8, token_budget: 12000, max_rounds: 12 },
                  high: { tool_calls: 16, token_budget: 16000, max_rounds: 20 },
                  extra: { tool_calls: 25, token_budget: 20000, max_rounds: 30 },
                  deep: { tool_calls: 35, token_budget: 25000, max_rounds: 40 },
                };
                const levels = [
                  { key: 'none', label: 'None', desc: 'No thinking (direct answer)' },
                  { key: 'low', label: 'Low', desc: 'Quick check' },
                  { key: 'medium', label: 'Medium', desc: 'Standard analysis' },
                  { key: 'high', label: 'High', desc: 'Thorough research' },
                  { key: 'extra', label: 'Extra', desc: 'Deep synthesis' },
                  { key: 'deep', label: 'Deep research', desc: 'Exhaustive web discovery' },
                ];
                return levels.map(l => {
                  const row = matrix[l.key] || defaults[l.key] || {};
                  const tc = row.tool_calls !== undefined ? row.tool_calls : (l.key === 'none' ? 0 : 8);
                  const tb = row.token_budget !== undefined ? row.token_budget : (l.key === 'none' ? 6000 : 12000);
                  const mr = row.max_rounds !== undefined ? row.max_rounds : (l.key === 'none' ? 0 : 12);
                  return `
                    <tr style="border-bottom:1px solid rgba(255,255,255,0.04);" data-matrix-lvl="${l.key}">
                      <td style="padding:8px 10px; font-weight:600; white-space:nowrap;">
                        <div>${esc(l.label)}</div>
                        <div class="dim" style="font-size:9.5px; font-weight:normal;">${esc(l.desc)}</div>
                      </td>
                      <td style="padding:6px 10px;">
                        <input type="number" min="0" max="100" class="matrix-tool-calls" value="${tc}"
                          ${canManageMcp ? '' : 'disabled'} style="width:85px; padding:4px 6px; border-radius:6px; font-size:12px; background:var(--panel); border:1px solid var(--border);">
                        ${l.key === 'none' ? '<span class="dim" style="font-size:9.5px; display:block; margin-top:2px;">0 = no tool call</span>' : ''}
                      </td>
                      <td style="padding:6px 10px;">
                        <input type="number" min="500" max="100000" step="500" class="matrix-token-budget" value="${tb}"
                          ${canManageMcp ? '' : 'disabled'} style="width:105px; padding:4px 6px; border-radius:6px; font-size:12px; background:var(--panel); border:1px solid var(--border);">
                      </td>
                      <td style="padding:6px 10px;">
                        <input type="number" min="0" max="100" class="matrix-max-rounds" value="${mr}"
                          ${canManageMcp ? '' : 'disabled'} style="width:85px; padding:4px 6px; border-radius:6px; font-size:12px; background:var(--panel); border:1px solid var(--border);">
                      </td>
                    </tr>
                  `;
                }).join('');
              })()}
            </tbody>
          </table>
        </div>
        <div style="margin-top:14px; padding-top:10px; border-top:1px solid var(--border-subtle); display:flex; align-items:center; gap:10px; flex-wrap:wrap;">
          <label style="display:block; font-size:11px; font-weight:600;">Web Content Truncation Cap (Characters):</label>
          <input type="number" id="web-soft-chars" min="500" max="50000" step="500" value="${d.web.soft_chars || 20000}"
            ${canManageMcp ? '' : 'disabled'} style="width:120px; padding:4px 8px; border-radius:6px; font-size:12px; background:var(--panel); border:1px solid var(--border);">
          <span class="dim" style="font-size:10px;">characters. Trims web search fetch results to keep context clean.</span>
        </div>
      </div>
    `;
    h += capSection('web', '🌐 Web Browsing', d.web.enabled, webInner,
      'web_fetch pulls page text; web_search queries DuckDuckGo (keyless)');

    // 2. Skills Pane
    h += capSection('skills', '🎯 Skills', d.skills.enabled,
      (d.skills.items || []).map(s => `
        <div class="cap-tool-entry">
          <span class="cap-tool-badge skill"><span class="tool-badge-ico">🎯</span><code>${esc(s.name)}</code></span>
          <span class="cap-tool-desc">${esc(s.description || '')}</span>
        </div>`).join('') || '<div class="cap-item dim" style="padding:4px 6px;">none in skills/ yet</div>',
      'Reusable instruction packs loaded from .agents/skills/*/SKILL.md');

    // 3. MCP Pane

    const mcpInner = (d.mcp.servers || []).length
      ? d.mcp.servers.map(s => `
          <details class="mcp-server-fold cap-item" style="margin-bottom:6px;">
            <summary class="mcp-server-head">
              <span class="mcp-chevron">▶</span>
              <span class="cap-dot ${s.status === 'ready' ? 'on' : (s.status === 'error' ? 'err' : '')}" title="${esc(s.status)}"></span>
              <b>${esc(s.name)}</b> <span class="mcp-scope-badge ${s.scope === 'user' ? 'user' : ''}" title="${s.scope === 'user' ? 'Only you can use this server' : 'Available to every user'}">${s.scope === 'user' ? 'personal' : 'global'}</span>
              <span class="dim">(${esc(s.transport)}) · ${s.status === 'ready' ? s.tools.length + ' tool(s)' : esc(s.status)}</span>
              ${(s.scope === 'user' || canManageMcp) ? `<span style="margin-left:auto; display:flex; gap:4px;" data-stop>
                ${(s.transport === 'http' && (s.auth || s.auth_required)) ? (s.signed_in
                  ? `<button class="btn ghost mcp-srv-signout" data-name="${esc(s.name)}" data-scope="${esc(s.scope || 'global')}" style="width:auto; margin:0; padding:1px 8px; font-size:10px;" title="Forget the stored sign-in (you can revoke the app's access in your account settings too)">🔓 Sign out</button>`
                  : `<button class="btn accent mcp-srv-oauth" data-name="${esc(s.name)}" data-scope="${esc(s.scope || 'global')}" style="width:auto; margin:0; padding:1px 8px; font-size:10px;" title="Sign in to this server's account (opens your browser)">🔑 Connect account</button>`) : ''}
                <button class="btn ghost mcp-srv-reconnect" data-name="${esc(s.name)}" data-scope="${esc(s.scope || 'global')}" style="width:auto; margin:0; padding:1px 8px; font-size:10px;" title="Reconnect (e.g. after finishing an OAuth login)">↻</button>
                <button class="btn ghost mcp-srv-edit" data-name="${esc(s.name)}" data-scope="${esc(s.scope || 'global')}" style="width:auto; margin:0; padding:1px 8px; font-size:10px;">Edit</button>
                <button class="btn ghost mcp-srv-del" data-name="${esc(s.name)}" data-scope="${esc(s.scope || 'global')}" style="width:auto; margin:0; padding:1px 8px; font-size:10px; color:var(--red);">✕</button>
              </span>` : ''}
            </summary>
            ${s.error ? `<div class="dim" style="font-size:10px; color:var(--red); margin:4px 0 4px 18px;">${esc(s.error)}</div>` : ''}
            ${(s.transport === 'http' && s.auth_required && !s.signed_in) ? `<div class="dim" style="font-size:10px; color:var(--amber, #d9a400); margin:4px 0 4px 18px;">Tool calls need a signed-in account: press 🔑 Connect account.</div>` : ''}
            <div class="mcp-tools-list">
              ${(s.tools || []).map(t => `
                <div class="cap-tool-entry sub">
                  <span class="cap-tool-badge mcp"><span class="tool-badge-ico">🔌</span><code>${esc(t.name)}</code></span>
                  ${t.description ? `<span class="cap-tool-desc">${esc(t.description)}</span>` : ''}
                </div>`).join('') || '<div class="dim" style="font-size:10px; padding:2px 0;">No tools exposed.</div>'}
            </div>
          </details>`).join('')
      : '<div class="cap-item dim" style="padding:4px 6px;">no servers configured yet</div>';
    h += capSection('mcp', '🔌 MCP Servers', d.mcp.enabled,
      mcpInner + (canAddMcp ? mcpEditorHtml(canManageMcp) : ''),
      'External tool servers via Model Context Protocol (stdio / http)');

    // 4. Plugins Pane
    const canManagePlugins = canManageMcp;
    h += capSection('plugins', '🧩 Plugins', d.plugins.enabled,
      ((d.plugins.items || []).map(p => pluginRowHtml(p, canManagePlugins)).join('')
        || '<div class="cap-item dim" style="padding:4px 6px;">no plugins installed yet</div>')
        + (canManagePlugins ? pluginBrowserHtml() : ''),
      'Python modules loaded from plugins/*/plugin.py · catalog = reviewed bundles in plugin_catalog/');

    // 5. Shell Pane
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
        <label style="display:flex; align-items:center; justify-content:space-between; gap:10px;">
          <div>
            <b>Default Terminal Shell</b>
            <div class="dim" style="font-size:11px;">Preferred shell for companion and console terminal</div>
          </div>
          <select id="setting-terminal-shell" style="background:var(--bg-input); color:var(--text); border:1px solid var(--border); border-radius:5px; padding:4px 8px; font-size:11.5px; outline:none; cursor:pointer;">
            <option value="powershell">PowerShell (Default)</option>
            <option value="cmd">Command Prompt (CMD)</option>
            <option value="bash">Git Bash / WSL (Bash)</option>
            <option value="pwsh">PowerShell Core (PWSH)</option>
          </select>
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
        <div style="margin-top:10px;"><b>Allowed for me</b>
          <span class="dim" style="font-size:10px;"> - commands you chose to always allow (your account only)</span>
          <div id="shell-my-pats" class="dim" style="font-size:10.5px; margin-top:4px;">Loading...</div></div>
      </div>`;
    h += capSection('shell', '⌨️ Shell Execution', sh.enabled, shellInner,
      'run_shell tool — agent runs commands like "npx skills add …" in the workspace');

    // 6. Agent Task & Limits Pane
    const ag = d.agent || {};
    let limits = null;
    if (canManageMcp) {
      try { const lr = await fetch('/control/agent_limits'); if (lr.ok) limits = await lr.json(); } catch (e) {}
    }
    const taskInner = `
      <div class="cap-item" style="margin-bottom:10px;">
        <b>Task Step Limits</b>
        <div style="display:flex; align-items:center; gap:8px; margin-top:8px;">
          <span style="min-width:140px;">Max steps per run</span>
          <input type="number" id="agent-max-steps" min="${ag.min || 5}" max="${ag.max || 200}" value="${ag.max_steps || 200}"
            ${canManageMcp ? '' : 'disabled'} style="width:70px; margin:0; background:var(--bg-input); color:var(--text); border:1px solid var(--border); border-radius:5px; padding:3px 7px; font-size:11px;">
          <span class="dim" style="font-size:9.5px;">${ag.min || 5}–${ag.max || 200}</span>
        </div>
        <div style="display:flex; align-items:center; gap:8px; margin-top:8px;">
          <span style="min-width:140px;">Wall-clock limit</span>
          <input type="number" id="agent-run-timeout" min="0" max="1440" value="${Math.round((ag.run_timeout_s != null ? ag.run_timeout_s : 1800) / 60)}"
            ${canManageMcp ? '' : 'disabled'} style="width:70px; margin:0; background:var(--bg-input); color:var(--text); border:1px solid var(--border); border-radius:5px; padding:3px 7px; font-size:11px;">
          <span class="dim" style="font-size:9.5px;">minutes (0 = off)</span>
        </div>
        <div style="display:flex; align-items:center; gap:8px; margin-top:8px;">
          <span style="min-width:140px;">Token budget per run</span>
          <input type="number" id="agent-run-budget" min="0" max="50" step="0.5" value="${((ag.run_token_budget != null ? ag.run_token_budget : 2500000) / 1000000).toFixed(1).replace(/\.0$/, '')}"
            ${canManageMcp ? '' : 'disabled'} style="width:70px; margin:0; background:var(--bg-input); color:var(--text); border:1px solid var(--border); border-radius:5px; padding:3px 7px; font-size:11px;">
          <span class="dim" style="font-size:9.5px;">million new tokens (0 = off)</span>
          ${canManageMcp ? '<button class="btn accent" id="agent-steps-save" style="width:auto; margin:0 0 0 10px; padding:3px 12px; font-size:10.5px;">Save</button>' : ''}
        </div>
        <div class="dim" style="font-size:9.5px; margin-top:6px;">The token budget counts only NEW tokens each step (the resent history is cached), warns the model at 50 / 70 / 90%, and lets it finish the item it is on before stopping with a Continue button. Runs repeating the same tool calls stop early. The wall-clock limit is what actually protects the GPU.</div>
      </div>
      ${limits ? agentLimitsHtml(limits) : ''}`;
    h += capSection('task', '🤖 Agent Task', true, taskInner, 'Execution step caps, timeouts, and resource guardrails', false);

    // 7. Memory Pane
    h += `<div class="ru-pane cap-pane" data-pane="memory" style="display:none;">
      ${agentMemoryHtml()}
    </div>`;

    // 8. Agent Library Pane
    let lib = null;
    try { lib = await (await fetch('/control/agent_library')).json(); } catch (e) {}
    const canManageLib = !!(window.hasPerm && window.hasPerm('settings.agents.configure'));
    if (lib && !lib.error) {
      h += agentLibraryHtml(lib, canManageLib);
    } else {
      h += `<div class="ru-pane cap-pane" data-pane="library" style="display:none;"><div class="dim" style="padding:10px;">Library not configured.</div></div>`;
    }

    // 9. Router Pane
    let rt = null;
    try { const rr = await fetch('/control/router'); if (rr.ok) rt = await rr.json(); } catch (e) {}
    const canManageRouter = !!(window.hasPerm && window.hasPerm('settings.router.configure'));
    if (rt) {
      h += routerHtml(rt, canManageRouter);
    } else {
      h += `<div class="ru-pane cap-pane" data-pane="router" style="display:none;"><div class="dim" style="padding:10px;">Router stats not available.</div></div>`;
    }

    h += '</div></div>';
    box.innerHTML = h;

    const _capShowTab = (tab) => {
      window._capTab = tab;
      box.querySelectorAll('.cap-tab').forEach(b => {
        const on = b.dataset.tab === tab;
        b.classList.toggle('on', on);
        b.setAttribute('aria-selected', on ? 'true' : 'false');
      });
      box.querySelectorAll('.cap-pane').forEach(p => {
        p.style.display = p.dataset.pane === tab ? '' : 'none';
      });
    };
    box.querySelectorAll('.cap-tab').forEach(b => {
      b.onclick = () => _capShowTab(b.dataset.tab);
    });
    _capShowTab(_capActiveTab);

    capHelpAttach(box);
    if (limits) wireAgentLimits(box);
    wireAgentMemory(box);
    if (lib && !lib.error && canManageLib) wireAgentLibrary(box, lib);
    if (rt && canManageRouter) wireRouter(box, rt);
    if (canAddMcp) wireMcpEditor(box, canManageMcp);
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
          body: JSON.stringify({
            max_steps: parseInt(box.querySelector('#agent-max-steps').value, 10) || 200,
            run_timeout_s: (parseInt(box.querySelector('#agent-run-timeout').value, 10) || 0) * 60,
            run_token_budget: Math.round((parseFloat(box.querySelector('#agent-run-budget').value) || 0) * 1000000),
          }),
        });
        const j = await r.json();
        if (!r.ok) throw new Error(j.error || j.detail || r.status);
        box.querySelector('#agent-max-steps').value = j.max_steps;
        box.querySelector('#agent-run-timeout').value = Math.round((j.run_timeout_s || 0) / 60);
        box.querySelector('#agent-run-budget').value = ((j.run_token_budget || 0) / 1000000).toFixed(1).replace(/[.]0$/, '');
        toast(`Agent limits set: ${j.max_steps} steps, ${Math.round((j.run_timeout_s || 0) / 60)} min, ${((j.run_token_budget || 0) / 1000000).toFixed(1)}M tokens ✓`);
      } catch (e) { toast('Save failed: ' + e.message, true); }
    };

    const webSaveBtn = box.querySelector('#web-budget-save');
    if (webSaveBtn) webSaveBtn.onclick = async () => {
      try {
        const matrixRows = box.querySelectorAll('tr[data-matrix-lvl]');
        const thinkingBudgets = {};
        matrixRows.forEach(row => {
          const lvl = row.dataset.matrixLvl;
          const toolCalls = parseInt(row.querySelector('.matrix-tool-calls').value, 10);
          const tokenBudget = parseInt(row.querySelector('.matrix-token-budget').value, 10);
          const maxRounds = parseInt(row.querySelector('.matrix-max-rounds').value, 10);
          thinkingBudgets[lvl] = {
            tool_calls: isNaN(toolCalls) ? (lvl === 'none' ? 0 : 8) : toolCalls,
            token_budget: isNaN(tokenBudget) ? (lvl === 'none' ? 6000 : 12000) : tokenBudget,
            max_rounds: isNaN(maxRounds) ? (lvl === 'none' ? 0 : 12) : maxRounds,
          };
        });

        const softChars = parseInt(box.querySelector('#web-soft-chars').value, 10) || 20000;
        const payload = {
          thinking_budgets: thinkingBudgets,
          max_web_calls: thinkingBudgets.medium ? thinkingBudgets.medium.tool_calls : 8,
          deep_max_web_calls: thinkingBudgets.deep ? thinkingBudgets.deep.tool_calls : 25,
          max_tool_rounds: thinkingBudgets.medium ? thinkingBudgets.medium.max_rounds : 12,
          deep_max_tool_rounds: thinkingBudgets.deep ? thinkingBudgets.deep.max_rounds : 30,
          soft_chars: softChars,
        };
        const r = await fetch('/control/web_settings', {
          method: 'POST', headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify(payload),
        });
        const j = await r.json();
        if (!r.ok) throw new Error(j.error || j.detail || r.status);
        toast(`Thinking-wise budget matrix saved successfully ✓`);
      } catch (e) { toast('Save failed: ' + e.message, true); }
    };

    // shell section controls
    const myPats = box.querySelector('#shell-my-pats');
    const loadMyPats = async () => {
      if (!myPats) return;
      try {
        const j = await (await fetch('/agent/allowed-commands')).json();
        const list = j.patterns || [];
        myPats.innerHTML = list.length ? list.map(p => `<div style="display:flex; gap:6px; align-items:center; margin:2px 0;">
            <code style="flex:1;">${esc(p)}</code>
            <button class="btn ghost my-pat-del" data-p="${esc(p)}" style="width:auto; margin:0; padding:2px 8px; font-size:10px; color:var(--red);">Forget</button></div>`).join('')
          : 'None yet. Choose "Always allow for me" in a command prompt to add one.';
        myPats.querySelectorAll('.my-pat-del').forEach(b => {
          b.onclick = async () => {
            const r = await fetch('/agent/allowed-commands?pattern=' + encodeURIComponent(b.dataset.p), { method: 'DELETE' });
            if (r.ok) loadMyPats(); else toast('Could not remove it', true);
          };
        });
      } catch (e) { myPats.textContent = 'Could not load your allowed commands.'; }
    };
    loadMyPats();
    const termShellEl = box.querySelector('#setting-terminal-shell');
    if (termShellEl) {
      termShellEl.value = localStorage.getItem('default_terminal_shell') || 'powershell';
      termShellEl.onchange = () => {
        localStorage.setItem('default_terminal_shell', termShellEl.value);
        const termSelect = document.getElementById('term-shell-select');
        if (termSelect) termSelect.value = termShellEl.value;
      };
    }
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
        ${it.source === 'native' ? '<span class="dim" style="font-size:9.5px;" title="app-owned command in config/commands (ported to this app\'s tools and lanes)">native</span>' : ''}
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
    <div class="cap-item">
      <label style="display:flex; align-items:center; gap:6px; flex-wrap:wrap;">
        <b>/multi-* lanes</b>
        ${['backend', 'frontend'].map(k => `<span>${k}</span>
          <select class="lib-lane" data-role="${k}" ${canEdit ? '' : 'disabled'} style="${inp}">
            ${(lib.lane_options || [{ name: 'main', label: 'main' }, { name: 'executor', label: 'executor' }]).map(o => `<option value="${esc(o.name)}" ${((lib.multi_lanes || {})[k] || (k === 'backend' ? 'main' : 'executor')) === o.name ? 'selected' : ''}>${esc(o.label)}</option>`).join('')}
          </select>`).join('')}
        <span class="dim" style="font-size:9.5px;">which model plays each analyst in /multi-plan, /multi-execute… (add models in Models &amp; Jobs)</span>
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
  return `<div class="ru-pane cap-pane" data-pane="library" style="display:none;">
    <div class="cap-pane-card" style="display:flex; align-items:center; justify-content:space-between; margin-bottom:12px; padding:12px 16px; background:var(--panel2); border-radius:8px; border:1px solid var(--border); gap:12px;">
      <div>
        <div style="font-weight:600; font-size:14px; color:var(--text); display:flex; align-items:center; gap:8px;">
          <span>📚 Agent Library</span>
          ${lib.enabled ? '<span class="cap-pill-badge on">Active</span>' : '<span class="cap-pill-badge off">Disabled</span>'}
        </div>
        <div class="dim" style="font-size:11px; margin-top:3px;">Agent profiles from .agents/agents and prompt commands from .agents/commands · org-wide, admin-managed</div>
      </div>
      <button class="cap-toggle-lib ${lib.enabled ? 'on' : ''}" ${canEdit ? '' : 'disabled'}
        data-stop style="width:auto; margin:0; padding:4px 14px; font-size:11px; font-weight:700;"
        title="${canEdit ? 'Enable/disable the Agent Library' : 'Admin-only setting'}">${lib.enabled ? 'ON' : 'OFF'}</button>
    </div>
    <div class="cap-pane-body" style="${lib.enabled ? '' : 'opacity:0.6;'}">
      ${inner}
      <div class="dim" style="font-size:9.5px; margin-top:8px;">Every change is audited.</div>
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
  box.querySelectorAll('.lib-lane').forEach(sel => sel.onchange = () =>
    run({ multi_lanes: { [sel.dataset.role]: sel.value } }, `/multi-* ${sel.dataset.role} lane: ${sel.value} ✓`));
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

/* ---------------- Router: usage stats, tuner suggestions, rules ---------------- */
/* ---------------- Router pane ---------------- */
const RT_TYPE_HINT = {
  action: 'Do something: run commands, edit files, use tools',
  creation: 'Build something: write a file, page or document',
  question: 'Answer a question',
  other: 'Anything that fits no other type',
  greeting: 'Hello and thanks, answered directly',
};

// the "how routing works" diagram; every box maps to a setting in Routing rules
function routerFlowSvg() {
  const box = (x, y, w, h, cls, t1, t2) => `<g class="rtf-node ${cls}"><rect x="${x}" y="${y}" width="${w}" height="${h}" rx="10"/>
      <text x="${x + w / 2}" y="${y + (t2 ? h / 2 - 4 : h / 2 + 4)}" text-anchor="middle" class="rtf-t1">${t1}</text>
      ${t2 ? `<text x="${x + w / 2}" y="${y + h / 2 + 12}" text-anchor="middle" class="rtf-t2">${t2}</text>` : ''}</g>`;
  const lbl = (x, y, t, a = 'middle') => `<text x="${x}" y="${y}" text-anchor="${a}" class="rtf-lbl">${t}</text>`;
  return `<svg viewBox="0 0 780 490" class="rtf-svg" role="img" aria-label="Routing flow: classify the request, then router, executor or main model, then record the outcome">
    <defs><marker id="rtf-arr" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="7" markerHeight="7" orient="auto-start-reverse"><path d="M0 0L10 5L0 10z" class="rtf-head"/></marker></defs>
    <g class="rtf-edges" marker-end="url(#rtf-arr)">
      <path d="M380 50V80"/>
      <path d="M380 128V150H115V170"/><path d="M380 128V170"/><path d="M380 128V150H645V170"/>
      <path d="M380 218V250"/>
      <path d="M290 272H240"/>
      <path d="M380 294V340"/>
      <path d="M470 364H645V218" class="rtf-esc"/>
      <path d="M140 294V430"/><path d="M380 388V430"/><path d="M740 194H760V450H740"/>
    </g>
    ${box(280, 10, 200, 40, 'rtf-n', 'Your request')}
    ${box(280, 80, 200, 48, 'rtf-n', 'Classify the request', 'keywords, optional Laya model')}
    ${box(20, 170, 190, 48, 'rtf-n', 'Greeting', 'answered directly, no tools')}
    ${box(285, 170, 190, 48, 'rtf-cpu', 'Router (CPU)', 'fast tool matcher')}
    ${box(550, 170, 190, 48, 'rtf-main', 'Main model', 'reasons and plans')}
    ${box(290, 250, 180, 44, 'rtf-n', 'Confident enough?', 'score at or above the threshold')}
    ${box(40, 250, 200, 44, 'rtf-cpu', 'Runs the tool directly', 'no model call needed')}
    ${box(290, 340, 180, 48, 'rtf-exec', 'Executor', 'small local model')}
    ${box(40, 430, 700, 40, 'rtf-n', 'Every run is recorded: type, lane and outcome (no prompt text)')}
    ${lbl(122, 148, 'greeting', 'middle')}${lbl(388, 164, 'everything else', 'start')}${lbl(640, 148, 'start-on-main types', 'middle')}
    ${lbl(265, 266, 'yes', 'end')}${lbl(388, 322, 'no', 'start')}
    ${lbl(560, 388, 'escalates after repeats, no tool call,', 'middle')}${lbl(560, 404, 'a refusal, or a failing lane', 'middle')}
  </svg>`;
}

function routerHelpHtml() {
  const step = (n, t, d) => `<li><span class="rtm-n">${n}</span><div><b>${t}</b><p>${d}</p></div></li>`;
  const knob = (t, d) => `<div class="rtm-knob"><b>${t}</b><span>${d}</span></div>`;
  return `<div class="rtm-back" id="rt-help" role="dialog" aria-modal="true" aria-labelledby="rtm-title">
    <div class="rtm">
      <div class="rtm-head">
        <div><h2 id="rtm-title">How routing works</h2>
          <p>Every agent request is sorted by type, then handed to the cheapest lane that can do it well. The router learns from past runs.</p></div>
        <button class="rtm-x" data-rt-close aria-label="Close">&times;</button>
      </div>
      <div class="rtm-body">
        <section>
          <h3>The path of one request</h3>
          <div class="rtf-wrap">${routerFlowSvg()}</div>
          <div class="rtf-legend"><span class="rtf-k cpu"></span>CPU router <span class="rtf-k exec"></span>Executor <span class="rtf-k main"></span>Main model <span class="rtf-k esc"></span>Escalation</div>
        </section>
        <section>
          <h3>Step by step</h3>
          <ol class="rtm-steps">
            ${step(1, 'Classify', 'The request is sorted into a type: action, creation, question, other or greeting. The keyword lists in Routing rules decide this. The optional Laya model can double-check the type.')}
            ${step(2, 'Pick a lane', 'Greetings are answered at once. Types ticked under <i>Start on main</i> skip the executor. For the rest, the CPU router tries to match a tool. If its score is at or above the confidence threshold, it runs the tool itself.')}
            ${step(3, 'Execute', 'Otherwise the small executor model does the work step by step. For types ticked under <i>Main plans first</i>, the main model writes a short plan first and the executor then follows it.')}
            ${step(4, 'Escalate if stuck', 'If the executor repeats itself, skips the tool call on an action or creation request, refuses, or its lane keeps failing, the request moves to the main model. A failing lane is retried later.')}
            ${step(5, 'Record and learn', 'The run is saved with its type, lane and result. Once enough runs exist, the tuner suggests rule changes. You approve them, or turn on automatic tuning. A change that makes runs worse is rolled back.')}
          </ol>
        </section>
        <section>
          <h3>What each setting does</h3>
          <div class="rtm-knobs">
            ${knob('Router confidence', 'Higher means the CPU router acts only when it is very sure. Lower means fewer model calls, more mistakes.')}
            ${knob('Escalate after repeats', 'How many identical steps in a row before the request moves to main.')}
            ${knob('Executor success', 'If the executor succeeds less often than this on a type, adaptive routing sends that type to main.')}
            ${knob('Require a tool call', 'Action requests must start with a tool call, not chat text.')}
            ${knob('Executor sees only the current request', 'Keeps the small model focused and its context short.')}
            ${knob('Offer the finish tool', 'Gives the model a clear way to say it is done and hand back the answer.')}
          </div>
        </section>
      </div>
    </div>
  </div>`;
}

function routerHtml(rt, canEdit) {
  const pct = v => (v == null ? '–' : Math.round(v * 100) + '%');
  const st = rt.stats_30d || {};
  const cats = st.categories || {};
  const catNames = Object.keys(cats).sort();
  const totalRuns = catNames.reduce((n, c) => n + (cats[c].runs || 0), 0);
  const maxRuns = Math.max(1, ...catNames.map(c => cats[c].runs || 0));
  const escRuns = catNames.reduce((n, c) => n + (cats[c].runs || 0) * (cats[c].escalation_rate || 0), 0);
  const rows = catNames.map(c => {
    const x = cats[c];
    const rated = (x.thumbs_up || 0) + (x.thumbs_down || 0);
    const hint = RT_TYPE_HINT[c] || '';
    return `<tr><td><span class="rt-type" ${hint ? `title="${esc(hint)}"` : ''}>${esc(c)}</span></td>
      <td><span class="rt-bar"><i style="width:${Math.round(((x.runs || 0) / maxRuns) * 100)}%"></i></span><span class="rt-num">${x.runs || 0}</span></td>
      <td class="rt-num">${x.avg_steps || 0}</td><td class="rt-num">${pct(x.escalation_rate)}</td><td class="rt-num">${pct(x.router_hit_rate)}</td>
      <td class="rt-num">${rated ? `${x.thumbs_up || 0} up · ${x.thumbs_down || 0} down` : '–'}</td></tr>`;
  }).join('');
  const lanes = st.lanes || {};
  const laneTile = (name, label) => {
    const x = lanes[name];
    return `<div class="rt-tile"><span>${label} tool calls</span><b>${x ? pct(x.tool_ok_rate) : '–'}</b><em>${x ? x.tool_calls + ' calls ok' : 'no calls yet'}</em></div>`;
  };
  const outcomes = Object.entries(st.outcomes || {}).map(([o, n]) => `<span class="rt-chip">${esc(o)} <b>${n}</b></span>`).join('');
  const fmtVal = v => esc(Array.isArray(v) ? (v.length ? v.join(', ') : '(none)') : String(v));
  const sugg = (rt.suggestions || []).map(sg => `<div class="rt-sugg">
      <div class="rt-sugg-top"><code>${esc(sg.key)}</code><span class="rt-chg">${fmtVal(sg.current)} <i>to</i> <b>${fmtVal(sg.proposed)}</b></span>
      ${canEdit ? `<span class="rt-act">
        <button class="btn accent rt-decide" data-id="${sg.id}" data-d="apply">Apply</button>
        <button class="btn ghost rt-decide" data-id="${sg.id}" data-d="dismiss">Dismiss</button></span>` : ''}</div>
      <p>${esc((sg.evidence || {}).why || '')}</p>
      <details><summary>Evidence</summary><pre class="mono">${esc(JSON.stringify(sg.evidence, null, 1))}</pre></details>
    </div>`).join('') || '<div class="rt-empty">No pending suggestions. The tuner runs daily and needs at least 30 runs per rule.</div>';
  const s = rt.settings || {};
  const dis = canEdit ? '' : 'disabled';
  const listField = (k, label, hint) => `<label class="rt-field rt-wide"><span>${label}</span><small>${hint}</small>
      <input type="text" class="rt-list" data-key="${k}" value="${esc((s[k] || []).join(', '))}" ${dis}></label>`;
  const mainCats = (rt.categories || []).filter(c => c !== 'greeting');
  const hist = (rt.history || []).slice(0, 8).map(h =>
    `<div>${esc(h.status)} · <code>${esc(h.key)}</code> to ${fmtVal(h.proposed)} · ${esc(h.decided_by || '')} · ${h.decided_at ? new Date(h.decided_at * 1000).toLocaleString() : ''}</div>`).join('');
  const healthRows = Object.entries(rt.lane_health || {}).map(([lane, h]) => {
    const cells = Object.entries(h.categories || {}).map(([c, x]) => {
      const r = x.success_rate;
      const tone = r == null ? '' : r >= 0.85 ? 'good' : r >= 0.7 ? 'warn' : 'bad';
      return `<span class="rt-health ${tone}" title="${x.turns} recent turns">${esc(c)} <b>${pct(r)}</b></span>`;
    }).join('');
    const brk = h.breaker_open
      ? `<span class="rt-breaker" title="Recent turns kept failing; requests go to main and this lane is re-tried periodically">Paused</span>`
      : (h.consecutive_failures ? `<span class="rt-note">${h.consecutive_failures} failed in a row</span>` : '');
    return `<div class="rt-lane"><div class="rt-lane-name"><code>${esc(lane)}</code>${brk}</div><div class="rt-lane-cells">${cells}</div></div>`;
  }).join('') || '<div class="rt-empty">No turns recorded since start-up.</div>';
  const applied = (rt.applied || []).slice(0, 6).map(a => {
    const badge = { watching: 'Watching', kept: 'Kept', rolled_back: 'Rolled back', superseded: 'Superseded' }[a.status] || esc(a.status);
    const res = a.result || {};
    const rates = (res.before_rate != null || res.after_rate != null)
      ? ` · bad runs ${pct(res.before_rate)} to ${pct(res.after_rate)}` : '';
    const canUndo = canEdit && (a.status === 'watching' || a.status === 'kept');
    return `<div class="rt-applied"><span class="rt-state ${esc(a.status)}">${badge}</span> <code>${esc(a.key)}</code> ${fmtVal(a.old)} to <b>${fmtVal(a.new)}</b>
      <span class="rt-note">${a.category ? '· ' + esc(a.category) : ''}${rates}${res.reason ? ' · ' + esc(res.reason) : ''}</span>
      ${canUndo ? `<button class="btn ghost rt-undo" data-id="${a.id}">Roll back</button>` : ''}</div>`;
  }).join('');
  const sw = (k, label, tip) => `<label class="rt-switch" title="${esc(tip)}"><input type="checkbox" class="rt-flag" data-key="${k}" ${s[k] ? 'checked' : ''} ${dis}><i></i><span>${label}<small>${esc(tip)}</small></span></label>`;
  const num = (id, label, hint, attrs, val) => `<label class="rt-field"><span>${label}</span><small>${hint}</small>
      <input type="number" id="${id}" ${attrs} value="${val}" ${dis}></label>`;
  const catPick = (cls, picked) => mainCats.map(c => `<label class="rt-pick"><input type="checkbox" class="${cls}" value="${c}" ${(picked || []).includes(c) ? 'checked' : ''} ${dis}><span>${c}</span></label>`).join('');
  const clf = s.classifier || {};
  return `<div class="ru-pane cap-pane rt-pane" data-pane="router" style="display:none;">
    <div class="rt-hero">
      <div>
        <h3>Router</h3>
        <p>Sorts each request and sends it to the lane that handles it best. Learns from your usage.</p>
      </div>
      <div class="rt-hero-r">
        <span class="cap-pill-badge ${rt.router_available ? 'on' : 'off'}">${esc(rt.engine || '')} router ${rt.router_available ? 'ready' : 'unavailable'}</span>
        <button class="btn ghost" id="rt-help-open">How it works</button>
      </div>
    </div>
    <div class="cap-pane-body rt-body">
      <div class="rt-tiles">
        <div class="rt-tile"><span>Runs, last 30 days</span><b>${totalRuns}</b><em>${catNames.length} request types</em></div>
        <div class="rt-tile"><span>Moved to main</span><b>${totalRuns ? pct(escRuns / totalRuns) : '–'}</b><em>executor could not finish</em></div>
        ${laneTile('main', 'Main')}${laneTile('executor', 'Executor')}
      </div>

      <section class="rt-card">
        <h4>Requests by type</h4>
        ${rows ? `<div class="rt-scroll"><table class="rt-table">
          <thead><tr><th>Type</th><th>Runs</th><th>Avg steps</th><th title="The executor's first step was re-run on main">Moved to main</th><th title="The CPU router picked the tool directly">Router hit</th><th>Feedback</th></tr></thead>
          <tbody>${rows}</tbody></table></div>` : '<div class="rt-empty">No agent runs recorded yet.</div>'}
        ${outcomes ? `<div class="rt-chips"><span class="rt-note">How runs ended</span>${outcomes}</div>` : ''}
      </section>

      <section class="rt-card">
        <h4>Lane health <small>Recent success per lane. A failing lane hands requests to main and is retried later.</small></h4>
        ${healthRows}
      </section>

      <section class="rt-card">
        <h4>Suggestions ${canEdit ? '<button class="btn ghost rt-sm" id="rt-tune">Analyze now</button>' : ''}</h4>
        ${sugg}
        ${canEdit || s.auto_apply !== undefined ? sw('auto_apply', 'Apply safe suggestions automatically', 'Applies one at a time, watches the runs that follow, and rolls it back if they end badly more often. Every step is audited.') : ''}
        ${applied ? `<div class="rt-sub">Automatic changes</div>${applied}` : ''}
        ${hist ? `<details class="rt-hist"><summary>Decision history</summary>${hist}</details>` : ''}
      </section>

      <details class="rt-card rt-rules" open>
        <summary><h4>Routing rules <small>${canEdit ? 'Edit, then save at the bottom' : 'Only admins can change these'}</small></h4></summary>

        <div class="rt-group">
          <div class="rt-gt">Thresholds</div>
          <div class="rt-fields">
            ${num('rt-thresh', 'Router confidence', 'The CPU router acts alone at or above this score', 'min="0.5" max="0.99" step="0.01"', s.confidence_threshold)}
            ${num('rt-streak', 'Escalate after', 'Identical repeated steps before main takes over', 'min="1" max="5"', s.repeat_streak_limit)}
            ${num('rt-minsucc', 'Executor success', 'Below this, adaptive routing sends the type to main', 'min="0.1" max="0.99" step="0.05"', s.min_executor_success)}
          </div>
        </div>

        <div class="rt-group">
          <div class="rt-gt">Behaviour</div>
          <div class="rt-switches">
            ${sw('adaptive', 'Adaptive routing', 'Send a request type to main while the executor is failing on it (see Lane health)')}
            ${sw('tool_choice_required', 'Require a tool call on action requests', 'Ask the model server to make the first step of an action request a tool call')}
            ${sw('executor_fresh_context', 'Executor sees only the current request', 'The small executor gets this request and this run, not the whole chat')}
            ${sw('finish_tool', 'Offer the finish tool', 'An explicit end-of-run signal the model can call with its final answer')}
          </div>
        </div>

        <div class="rt-group">
          <div class="rt-gt">Request classifier (Laya)</div>
          <div class="rt-clf">
            <label class="rt-switch"><input type="checkbox" id="rt-clf-on" ${clf.enabled ? 'checked' : ''} ${dis}><i></i><span>Use the classifier<small>A small model that double-checks the request type.</small></span></label>
            <select id="rt-clf-mode" ${dis}>
              <option value="shadow" ${clf.mode !== 'active' ? 'selected' : ''}>Shadow: log only</option>
              <option value="active" ${clf.mode === 'active' ? 'selected' : ''}>Active: correct the request type</option>
            </select>
          </div>
          <p class="rt-hint">Shadow mode records what it would decide next to each run so you can compare. Active mode corrects the keyword-based type when it is confident. Test it with <code>python scripts/eval_classifier.py</code> first.</p>
        </div>

        <div class="rt-group">
          <div class="rt-gt">Which lane starts</div>
          <div class="rt-row"><span>Start on main, skip the executor</span><div class="rt-picks">${catPick('rt-cat', s.start_on_main_categories)}</div></div>
          <div class="rt-row"><span>Main plans first, then the executor follows</span><div class="rt-picks">${catPick('rt-plan-cat', s.plan_first_categories)}
            <label class="rt-steps">up to <input type="number" id="rt-plan-steps" min="0" max="6" value="${s.plan_first_max_steps}" ${dis}> planning steps</label></div></div>
          <p class="rt-hint">With plan first, main is used only for the planning steps. Set 0 steps or tick no type to turn it off.</p>
        </div>

        <div class="rt-group">
          <div class="rt-gt">Keyword lists</div>
          <div class="rt-fields">
            ${listField('creation_keywords', 'Creation keywords', 'Marks a request as creation. Escalates if no tool is called.')}
            ${listField('action_keywords', 'Action keywords', 'Marks a request as action.')}
            ${listField('refusal_phrases', 'Refusal phrases', 'If the executor says one of these, the request moves to main.')}
            ${listField('greetings', 'Greetings', 'Answered directly, no tools.')}
          </div>
        </div>
        ${canEdit ? '<div class="rt-save"><button class="btn accent" id="rt-save">Save rules</button></div>' : ''}
      </details>

      <p class="rt-privacy">Only request types, lanes and tool outcomes are recorded (no prompt text), and kept 90 days. Suggestions apply only when you approve them or automatic tuning is on. Every change is audited.</p>
    </div>
  </div>`;
}

function wireRouter(box, rt) {
  const call = async (url, body, msg) => {
    try {
      const r = await fetch(url, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body || {}) });
      const d = await r.json().catch(() => ({}));
      if (!r.ok) throw new Error(d.error || d.detail || r.status);
      toast(typeof msg === 'function' ? msg(d) : msg); loadCapabilities();
    } catch (e) { toast('Router: ' + e.message, true); }
  };
  // "How it works" modal lives on <body> so the settings pane's overflow never clips it
  const helpBtn = box.querySelector('#rt-help-open');
  if (helpBtn) helpBtn.onclick = () => {
    document.getElementById('rt-help')?.remove();
    document.body.insertAdjacentHTML('beforeend', routerHelpHtml());
    const m = document.getElementById('rt-help');
    const prevOverflow = document.body.style.overflow;
    document.body.style.overflow = 'hidden';
    const close = () => { m.remove(); document.body.style.overflow = prevOverflow; document.removeEventListener('keydown', onKey); helpBtn.focus(); };
    const onKey = e => { if (e.key === 'Escape') close(); };
    document.addEventListener('keydown', onKey);
    m.addEventListener('click', e => { if (e.target === m || e.target.closest('[data-rt-close]')) close(); });
    m.querySelector('.rtm-x').focus();
  };
  const tune = box.querySelector('#rt-tune');
  if (tune) tune.onclick = () => call('/control/router/tune', null,
    d => (d.new_or_updated || []).length ? `${d.new_or_updated.length} suggestion(s) ready for review` : 'No changes suggested yet');
  box.querySelectorAll('.rt-decide').forEach(b => b.onclick = () =>
    call(`/control/router/suggestions/${b.dataset.id}/${b.dataset.d}`, null, b.dataset.d === 'apply' ? 'Suggestion applied ✓' : 'Suggestion dismissed'));
  box.querySelectorAll('.rt-undo').forEach(b => b.onclick = () =>
    call(`/control/router/applied/${b.dataset.id}/rollback`, null, 'Change rolled back ✓'));
  // the auto-apply switch sits outside "Routing rules" and saves on its own
  const auto = box.querySelector('.rt-flag[data-key="auto_apply"]');
  if (auto) auto.onchange = () => call('/control/router', { auto_apply: auto.checked },
    auto.checked ? 'Automatic tuning on ✓' : 'Automatic tuning off ✓');
  const save = box.querySelector('#rt-save');
  if (save) save.onclick = () => {
    const body = {
      confidence_threshold: parseFloat(box.querySelector('#rt-thresh').value),
      repeat_streak_limit: parseInt(box.querySelector('#rt-streak').value, 10),
      min_executor_success: parseFloat(box.querySelector('#rt-minsucc').value),
      start_on_main_categories: [...box.querySelectorAll('.rt-cat:checked')].map(x => x.value),
      plan_first_categories: [...box.querySelectorAll('.rt-plan-cat:checked')].map(x => x.value),
      plan_first_max_steps: parseInt(box.querySelector('#rt-plan-steps').value, 10) || 0,
    };
    box.querySelectorAll('.rt-flag').forEach(f => { if (f.dataset.key !== 'auto_apply') body[f.dataset.key] = f.checked; });
    body.classifier = { enabled: box.querySelector('#rt-clf-on').checked, mode: box.querySelector('#rt-clf-mode').value };
    box.querySelectorAll('.rt-list').forEach(i => {
      body[i.dataset.key] = i.value.split(',').map(x => x.trim()).filter(Boolean);
    });
    call('/control/router', body, 'Routing rules saved ✓');
  };
}


function capSection(id, title, enabled, innerHtml, note, hasToggle = true) {
  return `<div class="ru-pane cap-pane" data-pane="${id}" style="display:none;">
    <div class="cap-pane-card" style="display:flex; align-items:center; justify-content:space-between; margin-bottom:12px; padding:12px 16px; background:var(--panel2); border-radius:8px; border:1px solid var(--border); gap:12px;">
      <div>
        <div style="font-weight:600; font-size:14px; color:var(--text); display:flex; align-items:center; gap:8px;">
          <span>${title}</span>
          ${hasToggle ? (enabled ? '<span class="cap-pill-badge on">Active</span>' : '<span class="cap-pill-badge off">Disabled</span>') : ''}
        </div>
        ${note ? `<div class="dim" style="font-size:11px; margin-top:3px;">${esc(note)}</div>` : ''}
      </div>
      ${hasToggle ? `<button class="cap-toggle ${enabled ? 'on' : ''}" data-section="${id}" title="Enable/disable this capability" data-stop style="width:auto; margin:0; padding:4px 14px; font-size:11px; font-weight:700;">${enabled ? 'ON' : 'OFF'}</button>` : ''}
    </div>
    <div class="cap-pane-body" style="${enabled ? '' : 'opacity:0.6;'}">
      ${innerHtml || ''}
    </div>
  </div>`;
}

/* ---------------- MCP custom servers: add / edit / remove / paste JSON ---------------- */
const MCP_INP = 'background:var(--bg-input); color:var(--text); border:1px solid var(--border); border-radius:7px; padding:7px 10px; font-size:12px; box-sizing:border-box; font-family:inherit;';
let _mcpStatusTimer = null;
let _mcpConfigs = {};   // "scope:name" -> public config from /mcp/servers (no secret values)

// admins choose per save whether a server is for every user or just themselves
function mcpScopePicker(group) {
  return `
    <div style="display:flex; gap:12px; align-items:center; flex-wrap:wrap;"><span style="width:70px;">Available to</span>
      <label style="display:flex; gap:5px; align-items:center; cursor:pointer; font-size:11px;"><input type="radio" name="${group}" value="global" style="cursor:pointer; accent-color:var(--accent);"> Everyone (global)</label>
      <label style="display:flex; gap:5px; align-items:center; cursor:pointer; font-size:11px;"><input type="radio" name="${group}" value="user" style="cursor:pointer; accent-color:var(--accent);"> Just me</label>
    </div>`;
}

function mcpEditorHtml(canGlobal) {
  return `
    <div style="display:flex; gap:5px; margin-top:6px;">
      <button class="btn ghost" id="mcp-add-open" style="width:auto; margin:0; padding:3px 10px; font-size:10.5px;">+ Add MCP server</button>
      <button class="btn ghost" id="mcp-import-open" style="width:auto; margin:0; padding:3px 10px; font-size:10.5px;">Paste JSON</button>
    </div>
    ${canGlobal ? '' : `<div id="mcp-user-hint" class="dim" style="font-size:9.5px; margin-top:4px;">Servers you add are personal: only you see their tools, and secrets are stored for your account only.</div>`}
    ${canGlobal ? `
    <div class="cap-item" style="margin-top:5px;">
      <div style="display:flex; gap:5px; align-items:center; flex-wrap:wrap;">
        <span title="Non-admins can add personal stdio servers only with these npx/uvx packages. Approve only packages that call a remote API, never ones that read or write this server's disk.">Packages users may run</span>
        <input type="text" id="mcp-user-pkgs" placeholder="e.g. mcp-atlassian, @scope/pkg" style="${MCP_INP} flex:1; min-width:160px;">
        <button class="btn ghost" id="mcp-user-pkgs-save" style="width:auto; margin:0; padding:2px 10px; font-size:10px;">Save</button>
      </div>
      <div class="dim" style="font-size:9.5px; margin-top:2px;">Non-admins can add a public https URL, or npx/uvx with one of these packages, as a personal server.</div>
    </div>` : ''}
    <div id="mcp-import" class="cap-item" hidden style="border:1px solid var(--border); border-radius:6px; padding:6px 8px; margin-top:5px;">
      <div class="dim" style="font-size:10px; margin-bottom:4px;">Claude-Desktop format: <code>{"mcpServers": {"name": {"command": "npx", "args": [...]}}}</code></div>
      <textarea id="mcp-import-json" rows="7" style="${MCP_INP} width:100%; box-sizing:border-box;" placeholder='{"mcpServers": {"my-server": {"command": "npx", "args": ["-y", "mcp-remote", "https://example.com/mcp"]}}}'></textarea>
      ${canGlobal ? `<div style="margin-top:5px;">${mcpScopePicker('mcp-i-scope')}</div>` : ''}
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
      ${canGlobal ? `<div id="mcp-f-scope-row">${mcpScopePicker('mcp-f-scope')}</div>` : ''}
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
      <div id="mcp-f-oauth-wrap" style="display:none; flex-direction:column; gap:5px;">
        <label style="display:flex; gap:6px; align-items:center; cursor:pointer;">
          <input type="checkbox" id="mcp-f-oauth" style="accent-color:var(--accent);"> Sign in with OAuth (Gmail, Google Workspace, most hosted MCP servers)</label>
        <div id="mcp-f-oauth-fields" style="display:none; flex-direction:column; gap:5px; margin-left:18px;">
          <label style="display:flex; gap:6px; align-items:center;"><span style="width:70px;">Client ID</span>
            <input type="text" id="mcp-f-oauth-client" placeholder="blank = register automatically (if the server supports it)" style="${MCP_INP} flex:1;"></label>
          <label style="display:flex; gap:6px; align-items:center;"><span style="width:70px;">Secret</span>
            <input type="password" id="mcp-f-oauth-secret" autocomplete="new-password" placeholder="client secret (Google desktop clients) - stored in the OS keychain" style="${MCP_INP} flex:1;"></label>
          <label style="display:flex; gap:6px; align-items:center;"><span style="width:70px;">Scopes</span>
            <input type="text" id="mcp-f-oauth-scopes" placeholder="space-separated, least privilege" style="${MCP_INP} flex:1;"></label>
          <details><summary class="dim" style="font-size:10px; cursor:pointer;">Advanced: endpoints</summary>
            <div style="display:flex; flex-direction:column; gap:5px; margin-top:4px;">
              <label style="display:flex; gap:6px; align-items:center;"><span style="width:70px;">Auth URL</span>
                <input type="text" id="mcp-f-oauth-authurl" placeholder="auto-discovered" style="${MCP_INP} flex:1;"></label>
              <label style="display:flex; gap:6px; align-items:center;"><span style="width:70px;">Token URL</span>
                <input type="text" id="mcp-f-oauth-tokenurl" placeholder="auto-discovered" style="${MCP_INP} flex:1;"></label>
            </div></details>
          <div class="dim" style="font-size:9.5px;">After saving, press 🔑 Connect account. The sign-in opens in your browser through the companion app; tokens are kept per user in the OS keychain.</div>
        </div>
      </div>
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

function wireMcpEditor(box, canGlobal) {
  const form = box.querySelector('#mcp-form');
  const imp = box.querySelector('#mcp-import');
  const $f = id => box.querySelector('#mcp-f-' + id);
  let editing = null;   // {name, scope} of the server being edited, null = adding
  const pickedScope = group => {
    if (!canGlobal) return 'user';   // non-admins can only add personal servers
    const r = box.querySelector(`input[name="${group}"]:checked`);
    return r ? r.value : null;
  };
  const setScope = (group, scope, locked) => {
    box.querySelectorAll(`input[name="${group}"]`).forEach(r => {
      r.checked = r.value === scope;
      r.disabled = !!locked;
      r.style.cursor = locked ? 'not-allowed' : 'pointer';
      if (r.parentElement) {
        r.parentElement.style.cursor = locked ? 'not-allowed' : 'pointer';
        r.parentElement.style.opacity = locked ? '0.6' : '1';
      }
    });
  };
  const scopeQ = scope => `?scope=${encodeURIComponent(scope || 'global')}`;

  box.querySelectorAll('input[name="mcp-f-scope"]').forEach(r => {
    r.addEventListener('change', () => {
      if (editing) {
        const sc = pickedScope('mcp-f-scope') || editing.scope;
        box.querySelector('#mcp-form-title').textContent =
          `Edit ${sc === 'user' ? 'personal' : 'global'} MCP server: ${editing.name}`;
      }
    });
  });

  fetch('/mcp/servers').then(r => r.json()).then(d => {
    _mcpConfigs = {};
    (d.servers || []).forEach(s => { _mcpConfigs[`${s.scope}:${s.name}`] = s; });
    const pkgs = d.user_allowed_packages || [];
    $f('allowed').textContent = canGlobal
      ? 'allowed commands: ' + (d.allowed_commands || []).join(', ') + ' · personal servers of non-admins: npx/uvx + approved packages'
      : 'personal stdio servers: npx or uvx with an approved package: ' + (pkgs.join(', ') || '(none yet — ask an admin)');
    const pkgInp = box.querySelector('#mcp-user-pkgs');
    if (pkgInp) pkgInp.value = pkgs.join(', ');
  }).catch(() => {});

  const pkgSave = box.querySelector('#mcp-user-pkgs-save');
  if (pkgSave) pkgSave.onclick = async () => {
    const packages = box.querySelector('#mcp-user-pkgs').value.split(/[\s,]+/).map(x => x.trim()).filter(Boolean);
    try {
      const r = await fetch('/mcp/user-packages', {
        method: 'PUT', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ packages }),
      });
      const j = await r.json();
      if (!r.ok) throw new Error(j.error || j.detail || r.status);
      box.querySelector('#mcp-user-pkgs').value = j.packages.join(', ');
      toast('Approved packages saved ✓');
    } catch (e) { toast('Save failed: ' + e.message, true); }
  };

  const syncTransport = () => {
    const http = $f('transport').value === 'http';
    $f('stdio').style.display = http ? 'none' : 'flex';
    $f('http').style.display = http ? 'flex' : 'none';
    $f('oauth-wrap').style.display = http ? 'flex' : 'none';
    $f('oauth-fields').style.display = (http && $f('oauth').checked) ? 'flex' : 'none';
  };
  $f('transport').onchange = syncTransport;
  $f('oauth').onchange = syncTransport;
  // known remote servers: sensible least-privilege defaults
  $f('url').addEventListener('change', () => {
    if (/gmailmcp\.googleapis\.com/.test($f('url').value) && !$f('oauth-scopes').value) {
      $f('oauth').checked = true;
      $f('oauth-scopes').value = 'https://www.googleapis.com/auth/gmail.readonly https://www.googleapis.com/auth/gmail.compose';
      syncTransport();
    }
  });
  $f('env-add').onclick = () => $f('env').appendChild(mcpEnvRow());

  const openForm = cfg => {
    editing = cfg ? { name: cfg.name, scope: cfg.scope || 'global' } : null;
    imp.hidden = true;
    box.querySelector('#mcp-form-title').textContent = cfg
      ? `Edit ${cfg.scope === 'user' ? 'personal' : 'global'} MCP server: ${cfg.name}` : 'Add MCP server';
    $f('name').value = cfg ? cfg.name : '';
    $f('name').disabled = !!cfg;
    if (cfg) {
      $f('name').style.opacity = '0.65';
      $f('name').style.cursor = 'not-allowed';
      $f('name').title = 'Server name cannot be changed';
      let lock = box.querySelector('#mcp-f-name-lock');
      if (!lock) {
        lock = document.createElement('span');
        lock.id = 'mcp-f-name-lock';
        $f('name').parentNode.appendChild(lock);
      }
      lock.innerHTML = '🔒 <span class="dim" style="font-size:9.5px;">locked</span>';
      lock.style.cssText = 'font-size:11px; display:inline-flex; align-items:center; gap:3px; margin-left:6px;';
    } else {
      $f('name').style.opacity = '1';
      $f('name').style.cursor = 'text';
      $f('name').title = '';
      box.querySelector('#mcp-f-name-lock')?.remove();
    }
    // Keep availability editable: only server name remains locked on edit
    setScope('mcp-f-scope', cfg ? (cfg.scope || 'global') : null, false);
    $f('transport').value = cfg ? cfg.transport : 'stdio';
    $f('command').value = cfg ? (cfg.command || '') : 'npx';
    $f('args').value = cfg ? (cfg.args || []).join('\n') : '';
    $f('url').value = cfg ? (cfg.url || '') : '';
    $f('disabled').checked = !!(cfg && cfg.disabled);
    $f('env').innerHTML = '';
    if (cfg) {
      Object.entries(cfg.env || {}).forEach(([k, v]) => $f('env').appendChild(mcpEnvRow(k, v, false)));
      (cfg.secret_env_keys || []).filter(k => k !== 'OAUTH_CLIENT_SECRET')
        .forEach(k => $f('env').appendChild(mcpEnvRow(k, '', true, true)));
    }
    const au = (cfg && cfg.auth) || null;
    $f('oauth').checked = !!au;
    $f('oauth-client').value = au ? (au.client_id || '') : '';
    $f('oauth-scopes').value = au ? (au.scopes || []).join(' ') : '';
    $f('oauth-authurl').value = au ? (au.auth_url || '') : '';
    $f('oauth-tokenurl').value = au ? (au.token_url || '') : '';
    $f('oauth-secret').value = '';
    const secretStored = !!(cfg && (cfg.secret_env_keys || []).includes('OAUTH_CLIENT_SECRET'));
    $f('oauth-secret').placeholder = secretStored ? '•••• stored — leave blank to keep'
      : 'client secret (Google desktop clients) - stored in the OS keychain';
    $f('oauth-secret').dataset.stored = secretStored ? '1' : '';
    syncTransport();
    form.hidden = false;
    if (!cfg) $f('name').focus();
  };

  box.querySelector('#mcp-add-open').onclick = () => openForm(null);
  $f('cancel').onclick = () => { form.hidden = true; };
  box.querySelector('#mcp-import-open').onclick = () => {
    form.hidden = true;
    imp.hidden = !imp.hidden;
    if (!imp.hidden) setScope('mcp-i-scope', null, false);
  };
  box.querySelector('#mcp-import-cancel').onclick = () => { imp.hidden = true; };

  $f('save').onclick = async () => {
    const scope = pickedScope('mcp-f-scope') || (editing ? editing.scope : null);
    if (!scope) { toast('Choose who this server is for: everyone (global) or just you', true); return; }
    const name = $f('name').value.trim();
    if (!name) { toast('Please enter a server name', true); return; }
    const env = {}, secret_env = {};
    for (const row of $f('env').querySelectorAll('.mcp-env-row')) {
      const k = row.querySelector('.mcp-env-key').value.trim();
      if (!k) continue;
      const v = row.querySelector('.mcp-env-val').value;
      if (row.querySelector('.mcp-env-secret').checked) secret_env[k] = v; else env[k] = v;
    }
    const from_scope = editing ? editing.scope : null;
    let auth = null;
    if ($f('transport').value === 'http' && $f('oauth').checked) {
      auth = { type: 'oauth' };
      const cid = $f('oauth-client').value.trim();
      const scopes = $f('oauth-scopes').value.split(/\s+/).map(x => x.trim()).filter(Boolean);
      if (cid) auth.client_id = cid;
      if (scopes.length) auth.scopes = scopes;
      if ($f('oauth-authurl').value.trim()) auth.auth_url = $f('oauth-authurl').value.trim();
      if ($f('oauth-tokenurl').value.trim()) auth.token_url = $f('oauth-tokenurl').value.trim();
      const sec = $f('oauth-secret').value;
      if (sec) secret_env.OAUTH_CLIENT_SECRET = sec;
      else if ($f('oauth-secret').dataset.stored) secret_env.OAUTH_CLIENT_SECRET = '';   // keep the stored one
    }
    const body = {
      name,
      scope,
      from_scope: from_scope || undefined,
      transport: $f('transport').value,
      command: $f('command').value.trim(),
      args: $f('args').value.split('\n').map(a => a.trim()).filter(Boolean),
      url: $f('url').value.trim(),
      env, secret_env,
      auth: auth || undefined,
      disabled: $f('disabled').checked,
    };
    try {
      const url = editing
        ? `/mcp/servers/${encodeURIComponent(editing.name)}${from_scope ? `?from_scope=${encodeURIComponent(from_scope)}` : ''}`
        : '/mcp/servers';
      const r = await fetch(url, {
        method: editing ? 'PUT' : 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(body),
      });
      const j = await r.json();
      if (!r.ok) throw new Error(j.error || j.detail || r.status);
      toast(`${scope === 'user' ? 'Personal' : 'Global'} MCP server '${body.name}' saved — ${j.status ? j.status.status : 'saved'}…`);
      loadCapabilities();
    } catch (e) { toast('Save failed: ' + e.message, true); }
  };

  box.querySelector('#mcp-import-save').onclick = async () => {
    const out = box.querySelector('#mcp-import-result');
    const scope = pickedScope('mcp-i-scope');
    if (!scope) { out.textContent = 'Choose who these servers are for: everyone (global) or just you.'; return; }
    let config;
    try { config = JSON.parse(box.querySelector('#mcp-import-json').value); }
    catch (e) { out.textContent = 'Invalid JSON: ' + e.message; return; }
    try {
      const r = await fetch('/mcp/import', {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ config, scope }),
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
      const cfg = _mcpConfigs[`${btn.dataset.scope}:${btn.dataset.name}`];
      if (!cfg) { toast('Config not loaded yet — try again', true); return; }
      if (cfg.managed) { toast('Catalog connector — use Connect / Disconnect above', true); return; }
      openForm(cfg);
    };
  });
  box.querySelectorAll('.mcp-srv-oauth').forEach(btn => {
    btn.onclick = async () => {
      const name = btn.dataset.name, scope = btn.dataset.scope || 'global';
      try {
        const r = await fetch(`/mcp/servers/${encodeURIComponent(name)}/oauth/start`, {
          method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ scope }),
        });
        const j = await r.json();
        if (!r.ok) throw new Error(j.error || j.detail || r.status);
        if (j.mode === 'redirect' && j.auth_url) window.open(j.auth_url, '_blank', 'noopener');
        toast(j.message || 'Finish signing in in your browser…');
        btn.disabled = true;
        btn.textContent = '⏳ Waiting for sign-in…';
        const t0 = Date.now();
        const poll = async () => {
          const s = await fetch(`/mcp/servers/${encodeURIComponent(name)}/oauth/status${scopeQ(scope)}`).then(x => x.json()).catch(() => ({}));
          if (s.state === 'done' || s.signed_in && s.state !== 'pending') { toast(`Signed in to ${name} ✓`); loadCapabilities(); return; }
          if (s.state === 'error') { toast(`Sign-in failed: ${s.error || 'unknown error'}`, true); loadCapabilities(); return; }
          if (Date.now() - t0 > 330000) { toast('Sign-in timed out', true); loadCapabilities(); return; }
          setTimeout(poll, 2000);
        };
        setTimeout(poll, 2000);
      } catch (e) { toast('Sign-in failed: ' + e.message, true); }
    };
  });
  box.querySelectorAll('.mcp-srv-signout').forEach(btn => {
    btn.onclick = async () => {
      const name = btn.dataset.name;
      if (!confirm(`Forget the stored sign-in for '${name}'?`)) return;
      try {
        const r = await fetch(`/mcp/servers/${encodeURIComponent(name)}/oauth/disconnect`, {
          method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ scope: btn.dataset.scope || 'global' }),
        });
        if (!r.ok) throw new Error((await r.json()).error || r.status);
        toast(`Signed out of ${name}`);
        loadCapabilities();
      } catch (e) { toast('Sign-out failed: ' + e.message, true); }
    };
  });
  box.querySelectorAll('.mcp-srv-reconnect').forEach(btn => {
    btn.onclick = async () => {
      try {
        const r = await fetch(`/mcp/servers/${encodeURIComponent(btn.dataset.name)}/reconnect${scopeQ(btn.dataset.scope)}`, { method: 'POST' });
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
      const personal = btn.dataset.scope === 'user';
      if (!confirm(personal ? `Remove your personal MCP server '${name}'?`
                            : `Remove MCP server '${name}'? Its tools disappear for every user.`)) return;
      try {
        const r = await fetch(`/mcp/servers/${encodeURIComponent(name)}${scopeQ(btn.dataset.scope)}`, { method: 'DELETE' });
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
    <details class="mcp-server-fold cap-item" style="margin-bottom:6px;">
      <summary class="mcp-server-head">
        <span class="mcp-chevron">▶</span>
        <span class="cap-dot ${state}" title="${why}"></span>
        <b>${esc(p.name)}</b>
        <span class="dim">· ${p.tools.length} tool(s), ${p.prompt_fragments} prompt frag(s)${p.enabled ? '' : ' · disabled'}${p.modified ? ' · locally modified' : ''}</span>
        ${canManage ? `<span style="margin-left:auto; display:flex; gap:4px;" data-stop>
          <button class="btn ghost plugin-enable" data-name="${esc(p.name)}" data-enabled="${p.enabled ? '1' : ''}" style="${PLUGIN_BTN}">${p.enabled ? 'Disable' : 'Enable'}</button>
          ${p.from_catalog && !p.modified ? `<button class="btn ghost plugin-uninstall" data-name="${esc(p.name)}" style="${PLUGIN_BTN} color:var(--red);" title="Remove from plugins/ (can be reinstalled from the catalog)">✕</button>` : ''}
        </span>` : ''}
      </summary>
      ${p.error ? `<div class="dim" style="font-size:10px; color:var(--red); margin:4px 0 4px 18px;">${esc(p.error)}</div>` : ''}
      <div class="mcp-tools-list">
        ${(p.tools || []).map(t => `
          <div class="cap-tool-entry sub">
            <span class="cap-tool-badge plugin"><span class="tool-badge-ico">⚡</span><code>${esc(t.split('__').pop())}</code></span>
          </div>`).join('') || '<div class="dim" style="font-size:10px; padding:2px 0;">No tools exposed.</div>'}
      </div>
    </details>`;
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


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
      (d.web.tools || []).map(t => `<div class="cap-item">🔗 <b>${esc(t.name)}</b> — ${esc(t.description || '')}</div>`).join(''),
      'web_fetch pulls page text; web_search queries DuckDuckGo (keyless)');

    // Skills
    h += capSection('skills', '🎯 Skills', d.skills.enabled,
      (d.skills.items || []).map(s => `<div class="cap-item">📘 <b>${esc(s.name)}</b> — ${esc(s.description || '')}</div>`).join('') || '<div class="cap-item dim">none in skills/ yet</div>',
      'Reusable instruction packs loaded from skills/*/SKILL.md');

    // MCP
    const mcpInner = (d.mcp.servers || []).length
      ? d.mcp.servers.map(s => `
          <div class="cap-item">
            <span class="cap-dot ${s.status === 'ready' ? 'on' : (s.status === 'error' ? 'err' : '')}" title="${esc(s.status)}"></span>
            <b>${esc(s.name)}</b> <span class="dim">(${esc(s.transport)}) · ${s.tools.length} tool(s)</span>
            ${s.error ? `<div class="dim" style="font-size:10px; color:var(--red);">${esc(s.error)}</div>` : ''}
            ${(s.tools || []).map(t => `<div class="dim" style="font-size:10px; padding-left:14px;">↳ ${esc(t.name)} — ${esc(t.description || '')}</div>`).join('')}
          </div>`).join('')
      : '<div class="cap-item dim">no servers configured (config.json → capabilities.mcp_servers)</div>';
    h += capSection('mcp', '🔌 MCP Servers', d.mcp.enabled, mcpInner,
      'External tool servers via Model Context Protocol (stdio / http)');

    // Plugins
    h += capSection('plugins', '🧩 Plugins', d.plugins.enabled,
      (d.plugins.items || []).map(p => `
        <div class="cap-item">⚡ <b>${esc(p.name)}</b> <span class="dim">· ${p.tools.length} tool(s), ${p.prompt_fragments} prompt frag(s)</span>
          ${(p.tools || []).map(t => `<div class="dim" style="font-size:10px; padding-left:14px;">↳ ${esc(t.split('__').pop())}</div>`).join('')}
        </div>`).join('') || '<div class="cap-item dim">none in plugins/ yet</div>',
      'Python modules loaded from plugins/*/plugin.py');

    // Shell
    const sh = d.shell || {};
    const shellInner = `
      <div class="cap-item">
        <label style="display:flex; align-items:center; gap:6px; cursor:pointer;">
          <input type="checkbox" id="shell-ask" ${sh.ask_first ? 'checked' : ''} style="accent-color:var(--green);">
          <b>Ask before running</b> <span class="dim">(permission modal; matched patterns skip asking)</span>
        </label>
      </div>
      <div class="cap-item">
        <b>Allowed patterns</b> <span class="dim">(wildcards ok, <code>*</code> = allow all)</span>
        <div id="shell-pats" style="display:flex; flex-direction:column; gap:3px; margin-top:4px;">
          ${(sh.allow_patterns || []).map((p, i) => `
            <div style="display:flex; gap:5px; align-items:center;">
              <input type="text" class="shell-pat" data-i="${i}" value="${esc(p)}" style="flex:1; background:var(--bg-input); color:var(--text); border:1px solid var(--border); border-radius:5px; padding:3px 7px; font-size:10.5px; font-family:monospace;">
              <button class="btn ghost shell-pat-del" data-i="${i}" style="width:auto; margin:0; padding:2px 7px; font-size:10px; color:var(--red);">✕</button>
            </div>`).join('')}
        </div>
        <div style="display:flex; gap:5px; margin-top:5px;">
          <input type="text" id="shell-pat-new" placeholder="e.g. git * or npx skills * or *" style="flex:1; background:var(--bg-input); color:var(--text); border:1px solid var(--border); border-radius:5px; padding:3px 7px; font-size:10.5px; font-family:monospace;">
          <button class="btn ghost" id="shell-pat-add" style="width:auto; margin:0; padding:3px 10px; font-size:10.5px;">+ Add</button>
        </div>
        <button class="btn accent" id="shell-pats-save" style="width:auto; margin:6px 0 0; padding:4px 12px; font-size:10.5px;">💾 Save patterns</button>
      </div>`;
    h += capSection('shell', '⌨️ Shell Execution', sh.enabled, shellInner,
      'run_shell tool — agent runs commands like "npx skills add …" in the workspace');

    box.innerHTML = h;
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

function capSection(id, title, enabled, innerHtml, note) {
  return `<div class="cap-group">
    <div class="cap-head">
      <span>${title}</span>
      <button class="cap-toggle ${enabled ? 'on' : ''}" data-section="${id}" title="Enable/disable this capability">${enabled ? 'ON' : 'OFF'}</button>
    </div>
    <div class="cap-body" style="${enabled ? '' : 'opacity:0.45;'}">${innerHtml || ''}${note ? `<div class="dim" style="font-size:9.5px; margin-top:4px;">${esc(note)}</div>` : ''}</div>
  </div>`;
}

$('btn-theme').onclick = () => cycleTheme();
$('btn-settings').onclick = () => setSettings();
$('settings-close').onclick = () => setSettings(false);

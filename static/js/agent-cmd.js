/* ---------------- @ file tagging & / commands (agent mode) ---------------- */
let cmdMenu = { open: false, kind: null, items: [], sel: 0, tokenStart: -1 };

function cmdMenuClose() {
  cmdMenu.open = false; cmdMenu.kind = null; cmdMenu.items = []; cmdMenu.sel = 0; cmdMenu.tokenStart = -1;
  const m = $('cmd-menu');
  if (m) { m.style.display = 'none'; m.innerHTML = ''; }
}

function cmdMenuRender() {
  const m = $('cmd-menu');
  if (!m || !cmdMenu.open || !cmdMenu.items.length) { cmdMenuClose(); return; }
  m.innerHTML = cmdMenu.items.map((it, i) => `
    <div class="cmd-item ${i === cmdMenu.sel ? 'sel' : ''}" data-i="${i}">
      <span class="cmd-icon">${it.icon || ''}</span>
      <span class="cmd-name">${esc(it.name)}</span>
      ${it.desc ? `<span class="cmd-desc">${esc(it.desc)}</span>` : ''}
    </div>`).join('');
  m.style.display = 'block';
  m.querySelectorAll('.cmd-item').forEach(el => {
    el.onclick = () => cmdMenuPick(parseInt(el.dataset.i));
  });
}

async function cmdMenuOpen(kind, query) {
  cmdMenu.open = true; cmdMenu.kind = kind; cmdMenu.sel = 0;
  const m = $('cmd-menu');
  if (kind === 'files') {
    // fetch the workspace flat file list once per open (agent mode, project set)
    try {
      const d = await (await fetch('/agent/workspace')).json();
      const q = query.toLowerCase();
      cmdMenu.items = (d.files || [])
        .filter(f => !q || f.path.toLowerCase().includes(q))
        .slice(0, 12)
        .map(f => ({ icon: wsFileIcon(f.path), name: f.path, desc: `${(f.size / 1024).toFixed(1)} KB`, value: f.path }));
    } catch (e) { cmdMenuClose(); return; }
  } else if (kind === 'slash') {
    const all = [
      { icon: '📋', name: 'plan', desc: 'switch to Plan mode (read-only, propose)' },
      { icon: '🔨', name: 'build', desc: 'switch to Build mode (execute)' },
    ];
    try {
      const d = await (await fetch('/control/capabilities')).json();
      (d.skills && d.skills.items || []).forEach(s => {
        all.push({ icon: '🎯', name: s.name, desc: s.description || '', isSkill: true });
      });
    } catch (e) {}
    const q = query.toLowerCase();
    cmdMenu.items = all.filter(i => !q || i.name.toLowerCase().includes(q)).slice(0, 14);
    if (!cmdMenu.items.length) { cmdMenuClose(); return; }
  }
  cmdMenuRender();
}

function cmdMenuPick(i) {
  const it = cmdMenu.items[i];
  if (!it) return;
  const input = $('input');
  const before = input.value.slice(0, cmdMenu.tokenStart);
  const afterStart = cmdMenu.tokenStart + currentToken(input).length;
  const after = input.value.slice(afterStart);
  if (cmdMenu.kind === 'files') {
    input.value = before + '@' + it.value + ' ' + after.replace(/^\s?/, '');
  } else if (cmdMenu.kind === 'slash') {
    if (it.name === 'plan') { window._setPlanMode && window._setPlanMode(true); }
    else if (it.name === 'build') { window._setPlanMode && window._setPlanMode(false); }
    else if (it.isSkill) {
      input.value = before + '/' + it.name + ' ' + after;
    }
    else { input.value = before + '/' + it.name + ' ' + after; }
  }
  cmdMenuClose();
  input.focus();
}

function currentToken(input) {
  // text from the last @ or / up to the caret (or end of the current word)
  const pos = input.selectionStart != null ? input.selectionStart : input.value.length;
  const upto = input.value.slice(0, pos);
  const m = upto.match(/(?:^|\s)([@\/])([^@\/\s]*)$/);
  return m ? { ch: m[1], text: m[2], start: pos - m[2].length - 1 } : null;
}

(function initCmdMenu() {
  const input = $('input');
  if (!input) return;
  input.addEventListener('input', () => {
    if (!agentMode) { cmdMenuClose(); return; }
    const tok = currentToken(input);
    if (tok && tok.ch === '@') {
      if (!cmdMenu.open || cmdMenu.kind !== 'files') cmdMenu.tokenStart = tok.start;
      cmdMenuOpen('files', tok.text);
    } else if (tok && tok.ch === '/' && tok.start === 0) {
      if (!cmdMenu.open || cmdMenu.kind !== 'slash') cmdMenu.tokenStart = tok.start;
      cmdMenuOpen('slash', tok.text);
    } else {
      cmdMenuClose();
    }
  });
  input.addEventListener('keydown', e => {
    if (!cmdMenu.open || !cmdMenu.items.length) return;
    if (e.key === 'ArrowDown') { e.preventDefault(); cmdMenu.sel = (cmdMenu.sel + 1) % cmdMenu.items.length; cmdMenuRender(); }
    else if (e.key === 'ArrowUp') { e.preventDefault(); cmdMenu.sel = (cmdMenu.sel - 1 + cmdMenu.items.length) % cmdMenu.items.length; cmdMenuRender(); }
    else if (e.key === 'Enter' || e.key === 'Tab') { e.preventDefault(); e.stopPropagation(); cmdMenuPick(cmdMenu.sel); }
    else if (e.key === 'Escape') { cmdMenuClose(); }
  }, true);   // capture: run before the global Enter-to-send handler
})();

/* @tagged files get expanded into the prompt on send (agent mode) */
async function expandAtTags(text) {
  if (!text.includes('@')) return text;
  // @path tokens: @rel/path.ext (letters, digits, _ - . / \ space-free)
  const tags = [...text.matchAll(/@([\w\-./\\]+\.[\w]+)/g)].map(m => m[1]);
  if (!tags.length) return text;
  let out = text;
  for (const t of tags.slice(0, 5)) {
    try {
      const r = await fetch('/agent/ws/file?path=' + encodeURIComponent(t));
      const d = await r.json();
      if (!r.ok || d.error) continue;
      const clipped = d.content.length > 12000
        ? d.content.slice(0, 12000) + '\n... (truncated, ' + d.content.length + ' chars)' : d.content;
      out = out.split('@' + t).join('see file "' + t + '":\n' + clipped);
    } catch (e) { /* leave the tag as-is */ }
  }
  return out;
}

/* mode switcher: Chat vs Agent */
function setAppMode(isAgent, isUserSwitch = false) {
  const prevMode = agentMode;
  agentMode = !!isAgent;
  try { localStorage.setItem('app_mode', agentMode ? 'agent' : 'chat'); } catch (e) {}

  const btnChat = $('mode-chat');
  const btnAgent = $('mode-agent');
  if (btnChat) btnChat.classList.toggle('active', !agentMode);
  if (btnAgent) btnAgent.classList.toggle('active', agentMode);
  const banner = $('agent-banner');
  if (banner) banner.style.display = 'none';
  const projCard = $('projects-card');
  if (projCard) projCard.style.display = agentMode ? 'block' : 'none';
  // plan/build select + engine select only in agent mode
  const planSel = $('agent-plan-sel');
  if (planSel) planSel.style.display = agentMode ? 'inline-block' : 'none';
  const eng = $('agent-engine');
  if (eng) eng.style.display = agentMode ? 'inline-block' : 'none';
  const webToggle = $('btn-web-toggle');
  if (webToggle) webToggle.style.display = agentMode ? 'none' : 'flex';
  if (agentMode && window._setPlanMode) window._setPlanMode(planMode);   // refresh placeholder
  // workspace side panel needs agent mode + an active project
  if (!agentMode && wsPanelOpen) setWsPanel(false);
  updateWsRail();
  const sTitle = $('session-title');
  if (sTitle) {
    sTitle.textContent = agentMode ? 'Project Tasks' : 'Recent Chats';
    sTitle.title = agentMode ? 'Project-scoped task sessions' : 'Generic chat sessions';
  }
  const sel = $('profile');
  const mName = (sel && sel.value) ? sel.value.split('\\').pop().split('/').pop() : 'model';
  if (agentMode) {
    $('input').placeholder = 'Describe a coding task (e.g. "Find and fix bug in main.py", "Refactor the database queries")...';
    if (isUserSwitch) toast(curProject ? `Agent Task mode active (workspace: ${curProject.name || curProject})` : 'Agent Task mode active');
  } else {
    $('input').placeholder = `Message ${mName}... (Enter to send, Shift+Enter for newline)`;
    if (isUserSwitch) toast('Chat mode active — direct conversation with LLM');
  }

  // Switching between modes opens a new fresh chat window and loads relevant session list
  if (isUserSwitch && prevMode !== agentMode) {
    if (ctrl) {
      try { ctrl.abort(); } catch (e) {}
      ctrl = null;
      setGenUI(false);
    }
    curSession = null;
    messages = [];
    try {
      localStorage.removeItem(agentMode ? 'active_agent_session_id' : 'active_chat_session_id');
    } catch (e) {}
    renderAll();
    loadSessions(false);
    if ($('input')) {
      $('input').value = '';
      $('input').focus();
    }
    clearAttachments();
  } else {
    loadSessions(true);
  }
}

$('mode-chat').onclick = () => setAppMode(false, true);
$('mode-agent').onclick = () => setAppMode(true, true);

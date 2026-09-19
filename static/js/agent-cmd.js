/* ---------------- @ file tagging & / commands (agent mode) ---------------- */
let cmdMenu = { open: false, kind: null, items: [], sel: 0, tokenStart: -1 };

/* ---- armed slash command: rendered as a <name> chip instead of literal text ---- */
let armedCmd = null;
/* ---- armed skills: multiple can be attached at once, each its own chip ---- */
let armedSkills = [];

const CMD_HINTS = {
  compact: {
    icon: '🧹',
    ph: 'Optional: extra instructions for the summary (e.g. focus on the database schema) — Enter to compact',
    title: '/compact — compress the conversation into a summary and keep going',
    hint: 'Enter to compact',
  },
  plan: {
    icon: '📋',
    ph: 'Describe what to plan (read-only, will not make changes) — Enter to propose',
    title: '/plan — Plan mode (explores & proposes changes)',
    hint: 'Enter to plan',
  },
  build: {
    icon: '🔨',
    ph: 'Describe what to build or fix (executes changes in workspace) — Enter to execute',
    title: '/build — Build mode (executes changes)',
    hint: 'Enter to build',
  },
};

/* Default composer placeholder for the current mode (mirrors setAppMode). */
function defaultInputPlaceholder() {
  const sel = $('profile');
  const mName = (sel && sel.value) ? sel.value.split('\\').pop().split('/').pop() : 'model';
  return agentMode
    ? 'Describe a coding task (e.g. "Find and fix bug in main.py", "Refactor the database queries")...'
    : `Message ${mName}... (Enter to send, Shift+Enter for newline)`;
}

/* While a command is armed the placeholder becomes that command's argument hint. */
function refreshInputPlaceholder() {
  const input = $('input');
  if (!input) return;
  if (armedCmd) {
    const h = CMD_HINTS[armedCmd.name] || {};
    input.placeholder = h.ph || (armedCmd.desc ? `${armedCmd.desc} — Enter to run` : `Instructions for /${armedCmd.name} — Enter to run`);
  } else {
    input.placeholder = defaultInputPlaceholder();
  }
}

function cmdChipRender() {
  const bar = $('cmd-chip-bar');
  if (!bar) return;
  if (!armedCmd && !armedSkills.length) { bar.innerHTML = ''; return; }
  const skillChips = armedSkills.map(s => `
     <div class="cmd-chip" title="${esc(s.desc || ('/' + s.name))}">
       <span class="cmd-chip-icon">${esc(s.icon || '🎯')}</span>
       <span class="cmd-chip-name">&lt;${esc(s.name)}&gt;</span>
       <span class="cmd-chip-x" data-skill="${esc(s.name)}" title="Remove skill (or press Backspace on an empty box)">✕</span>
     </div>`).join('');
  let cmdChip = '';
  if (armedCmd) {
    const h = CMD_HINTS[armedCmd.name] || {};
    cmdChip =
      `<div class="cmd-chip" title="${esc(h.title || armedCmd.desc || ('/' + armedCmd.name))}">
         <span class="cmd-chip-icon">${esc(armedCmd.icon || '')}</span>
         <span class="cmd-chip-name">&lt;${esc(armedCmd.name)}&gt;</span>
         <span class="cmd-chip-hint">${esc(h.hint || 'Enter to run')}</span>
         <span class="cmd-chip-x" data-x="1" title="Remove command (or press Backspace on an empty box)">✕</span>
       </div>`;
  }
  bar.innerHTML = skillChips + cmdChip;
  const x = bar.querySelector('[data-x]');
  if (x) x.onclick = () => { disarmCmd(); const i = $('input'); if (i) i.focus(); };
  bar.querySelectorAll('[data-skill]').forEach(el => {
    el.onclick = () => { removeSkill(el.dataset.skill); const i = $('input'); if (i) i.focus(); };
  });
}

/* Arm a slash command: the composer is cleared, the chip shows <name>, and the
   placeholder turns into the command's argument hint (like Claude Code). */
function armCmd(name, opts = {}) {
  const input = $('input');
  if (!input) return;
  const h = CMD_HINTS[name] || {};
  armedCmd = {
    name,
    icon: opts.icon || h.icon || '⌘',
    desc: opts.desc || '',
    isSkill: !!opts.isSkill
  };
  input.value = opts.keepText || '';
  refreshInputPlaceholder();
  cmdChipRender();
  input.focus();
}

function disarmCmd() {
  const input = $('input');
  armedCmd = null;
  cmdChipRender();
  refreshInputPlaceholder();
}

/* Arm a skill: unlike armCmd, multiple skills can be armed at once. Only the
   "/skillname" token typed is removed from the composer — everything else
   the user already typed is kept. */
function armSkill(it, keepText) {
  const input = $('input');
  if (!input) return;
  if (!armedSkills.some(s => s.name === it.name)) {
    armedSkills.push({ name: it.name, icon: it.icon || '🎯', desc: it.desc || '' });
  }
  input.value = keepText != null ? keepText : input.value;
  refreshInputPlaceholder();
  cmdChipRender();
  input.focus();
}

function removeSkill(name) {
  armedSkills = armedSkills.filter(s => s.name !== name);
  cmdChipRender();
  refreshInputPlaceholder();
}

function clearArmedSkills() {
  armedSkills = [];
  cmdChipRender();
  refreshInputPlaceholder();
}

/* Run an armed command, with whatever is typed in the composer as its argument. */
function runArmedCmd(cmd, text) {
  if (!cmd) return;
  const arg = (text || '').trim();
  if (cmd.name === 'compact') {
    disarmCmd();
    if (typeof doCompact === 'function') doCompact(arg);
    return;
  }
  if (cmd.name === 'plan') {
    disarmCmd();
    if (window._setPlanMode) window._setPlanMode(true);
    if (arg) {
      if (agentMode) runAgentSSE(arg); else send(arg);
    }
    return;
  }
  if (cmd.name === 'build') {
    disarmCmd();
    if (window._setPlanMode) window._setPlanMode(false);
    if (arg) {
      if (agentMode) runAgentSSE(arg); else send(arg);
    }
    return;
  }
  const line = arg ? ('/' + cmd.name + ' ' + arg).trim() : ('/' + cmd.name);
  disarmCmd();
  if (agentMode) runAgentSSE(line); else send(line);
}

window.armCmd = armCmd;
window.disarmCmd = disarmCmd;
window.runArmedCmd = runArmedCmd;
window.getArmedCmd = () => armedCmd;
window.armSkill = armSkill;
window.removeSkill = removeSkill;
window.clearArmedSkills = clearArmedSkills;
window.getArmedSkills = () => armedSkills.slice();
window.defaultInputPlaceholder = defaultInputPlaceholder;
window.refreshInputPlaceholder = refreshInputPlaceholder;

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
    el.onmousedown = (e) => {
      e.preventDefault();
    };
    el.onclick = (e) => {
      e.preventDefault();
      e.stopPropagation();
      cmdMenuPick(parseInt(el.dataset.i));
    };
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
    // /plan and /build are agent-mode only; /compact works in both (agent mode
    // requires an active project — enforced by doCompact and the backend).
    const all = agentMode ? [
      { icon: '📋', name: 'plan', desc: 'switch to Plan mode (read-only, propose)' },
      { icon: '🔨', name: 'build', desc: 'switch to Build mode (execute)' },
      { icon: '🧹', name: 'compact', desc: 'compress conversation history (needs an active project)' },
    ] : [
      { icon: '🧹', name: 'compact', desc: 'compress conversation history' },
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
  const kind = cmdMenu.kind;
  const tok = currentToken(input);
  const tokLen = tok ? (tok.text.length + 1) : 0;
  const startIdx = cmdMenu.tokenStart >= 0 ? cmdMenu.tokenStart : 0;
  const before = input.value.slice(0, startIdx);
  const after = input.value.slice(startIdx + tokLen);
  cmdMenuClose();

  if (kind === 'files') {
    input.value = before + '@' + it.value + ' ' + after.replace(/^\s?/, '');
    input.focus();
  } else if (kind === 'slash') {
    const keepText = (before + after).replace(/^\s+/, '');
    if (it.isSkill) {
      armSkill(it, keepText);
      return;
    }
    if (it.name === 'plan') {
      if (window._setPlanMode) window._setPlanMode(true);
    } else if (it.name === 'build') {
      if (window._setPlanMode) window._setPlanMode(false);
    }
    armCmd(it.name, { ...it, keepText });
  }
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
    // If user typed '/compact ' directly, auto-arm it
    if (input.value.trim().toLowerCase() === '/compact' && input.value.endsWith(' ')) {
      cmdMenuClose();
      armCmd('compact');
      return;
    }
    // slash menu works in chat mode too (/compact); @ file tags stay agent-only
    const tok = currentToken(input);
    if (tok && tok.ch === '@' && agentMode) {
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
    if (e.key === 'Backspace' && !input.value && (armedCmd || armedSkills.length)) {
      e.preventDefault();
      if (armedSkills.length) removeSkill(armedSkills[armedSkills.length - 1].name);
      else disarmCmd();
      return;
    }
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
  refreshInputPlaceholder();
  if (agentMode) {
    if (isUserSwitch) toast(curProject ? `Agent Task mode active (workspace: ${curProject.name || curProject})` : 'Agent Task mode active — please select a project');
  } else {
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

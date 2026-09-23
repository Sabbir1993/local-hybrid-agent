/* ---------------- @ file tagging & / commands (agent mode) ---------------- */
let cmdMenu = { open: false, kind: null, items: [], sel: 0, tokenStart: -1 };

/* ---- armed slash command: rendered as a <name> chip instead of literal text ---- */
let armedCmd = null;

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
  init: {
    icon: '🧭',
    ph: 'Optional: what to focus on (e.g. the payment module) — Enter to scan the project and write AGENTS.md',
    title: '/init — scan the project and write AGENTS.md (auto-loaded into every agent task)',
    hint: 'Enter to initialize',
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
  if (!armedCmd) { bar.innerHTML = ''; return; }
  const h = CMD_HINTS[armedCmd.name] || {};
  bar.innerHTML =
    `<div class="cmd-chip" title="${esc(h.title || armedCmd.desc || ('/' + armedCmd.name))}">
       <span class="cmd-chip-icon">${esc(armedCmd.icon || '')}</span>
       <span class="cmd-chip-name">&lt;${esc(armedCmd.name)}&gt;</span>
       <span class="cmd-chip-hint">${esc(h.hint || 'Enter to run')}</span>
       <span class="cmd-chip-x" data-x="1" title="Remove command (or press Backspace on an empty box)">✕</span>
     </div>`;
  const x = bar.querySelector('[data-x]');
  if (x) x.onclick = () => { disarmCmd(); const i = $('input'); if (i) i.focus(); };
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
  input.value = '';
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
  if (cmd.name === 'init') {
    disarmCmd();
    runInit(arg);
    return;
  }
  const line = arg ? ('/' + cmd.name + ' ' + arg).trim() : ('/' + cmd.name);
  disarmCmd();
  if (agentMode) runAgentSSE(line); else send(line);
}

/* ---- /init: scan the project and write AGENTS.md (like Claude Code / Codex) ----
   The backend injects AGENTS.md into every agent / sub-agent prompt for the
   active project (core/project_context.py); this just runs the init task. */
async function fetchProjectInstructions() {
  try {
    const r = await fetch('/agent/project_instructions', { headers: { ...getDeviceHeaders() } });
    return r.ok ? await r.json() : null;
  } catch (e) { return null; }
}

async function runInit(extra) {
  if (!agentMode) { toast('/init works in Agent Task mode', true); return; }
  if (!curProject || !curProject.id) { flashProjectsCard(); return; }
  const d = await fetchProjectInstructions();
  if (!d || !d.init_prompt) { toast('Could not load the /init prompt from the server', true); return; }
  if (window._setPlanMode) window._setPlanMode(false);   // /init writes a file -> Build mode
  let prompt = d.init_prompt;
  if (extra) prompt += `

Extra focus from the user: ${extra}`;
  await runAgentSSE(prompt);
  refreshProjectInitHint();
}

/* Composer hint after a project is selected: AGENTS.md loaded, or offer /init. */
async function refreshProjectInitHint() {
  const bar = $('proj-init-bar');
  if (!bar) return;
  if (!agentMode || !curProject || !curProject.id) { bar.style.display = 'none'; bar.innerHTML = ''; return; }
  const pid = curProject.id;
  const d = await fetchProjectInstructions();
  if (!curProject || curProject.id !== pid) return;            // project changed meanwhile
  if (!d) { bar.style.display = 'none'; return; }
  const dismissKey = `init_hint_dismissed_${pid}`;
  if (d.exists) {
    bar.innerHTML = `<span class="proj-init-ok" title="Injected into every agent task for this project">📘 ${esc(d.filename)} loaded</span>`;
  } else {
    let dismissed = false;
    try { dismissed = localStorage.getItem(dismissKey) === '1'; } catch (e) {}
    if (dismissed) { bar.style.display = 'none'; bar.innerHTML = ''; return; }
    bar.innerHTML = `<span>🧭 No AGENTS.md in this project — run <b>/init</b> so the agent learns its commands and structure.</span>
      <button class="btn ghost proj-init-btn" data-init>Run /init</button>
      <span class="cmd-chip-x" data-x title="Dismiss for this project">✕</span>`;
    bar.querySelector('[data-init]').onclick = () => armCmd('init');
    bar.querySelector('[data-x]').onclick = () => {
      try { localStorage.setItem(dismissKey, '1'); } catch (e) {}
      bar.style.display = 'none'; bar.innerHTML = '';
    };
  }
  bar.style.display = 'flex';
}

window.runInit = runInit;
window.refreshProjectInitHint = refreshProjectInitHint;
window.armCmd = armCmd;
window.disarmCmd = disarmCmd;
window.runArmedCmd = runArmedCmd;
window.getArmedCmd = () => armedCmd;
window.defaultInputPlaceholder = defaultInputPlaceholder;
window.refreshInputPlaceholder = refreshInputPlaceholder;

function cmdMenuClose() {
  cmdMenu.open = false; cmdMenu.kind = null; cmdMenu.items = []; cmdMenu.sel = 0; cmdMenu.tokenStart = -1;
  const m = $('cmd-menu');
  if (m) { m.style.display = 'none'; m.innerHTML = ''; }
}

const CMD_GROUP_LABELS = { mode: 'Mode', utility: 'Utility', skills: 'Skills' };

function cmdMenuRender() {
  const m = $('cmd-menu');
  if (!m || !cmdMenu.open || !cmdMenu.items.length) { cmdMenuClose(); return; }
  m.innerHTML = cmdMenu.items.map((it, i) => {
    const prevCat = i > 0 ? cmdMenu.items[i - 1].category : null;
    const label = it.category && it.category !== prevCat
      ? `<div class="cmd-group-label">${esc(CMD_GROUP_LABELS[it.category] || it.category)}</div>` : '';
    return `${label}
    <div class="cmd-item ${i === cmdMenu.sel ? 'sel' : ''}" data-i="${i}">
      <span class="cmd-icon">${it.icon || ''}</span>
      <span class="cmd-name">${esc(it.name)}</span>
      ${it.desc ? `<span class="cmd-desc">${esc(it.desc)}</span>` : ''}
    </div>`;
  }).join('');
  m.style.display = 'block';
  // keep the keyboard selection visible when arrowing past the menu's max-height
  const selEl = m.querySelector('.cmd-item.sel');
  if (selEl) {
    if (cmdMenu.sel === 0) {
      m.scrollTop = 0;   // also reveal the first group label
    } else if (selEl.offsetTop < m.scrollTop) {
      // scrolling up onto a group's first item: show its label too
      const prev = selEl.previousElementSibling;
      const top = prev && prev.classList.contains('cmd-group-label') ? prev.offsetTop : selEl.offsetTop;
      m.scrollTop = top;
    } else if (selEl.offsetTop + selEl.offsetHeight > m.scrollTop + m.clientHeight) {
      m.scrollTop = selEl.offsetTop + selEl.offsetHeight - m.clientHeight;
    }
  }
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
        .map(f => ({ icon: wsFileIcon(f.path), name: f.path,
                     desc: typeof f.size === 'number' ? `${(f.size / 1024).toFixed(1)} KB` : '', value: f.path }));
    } catch (e) { cmdMenuClose(); return; }
  } else if (kind === 'slash') {
    // /plan and /build are agent-mode only; /compact works in both (agent mode
    // requires an active project — enforced by doCompact and the backend).
    const all = agentMode ? [
      { icon: '📋', name: 'plan', desc: 'switch to Plan mode (read-only, propose)', category: 'mode' },
      { icon: '🔨', name: 'build', desc: 'switch to Build mode (execute)', category: 'mode' },
      { icon: '🧭', name: 'init', desc: 'scan the project and write AGENTS.md (auto-loaded into agent tasks)', category: 'utility' },
      { icon: '🧹', name: 'compact', desc: 'compress conversation history (needs an active project)', category: 'utility' },
      { icon: '🤖', name: 'subagent', desc: 'delegate a sub-task to a focused sub-agent', category: 'utility', template: true },
      { icon: '🧑‍🤝‍🧑', name: 'multiagent', desc: 'delegate multiple roles (planner/coder/reviewer) in one prompt', category: 'utility', template: true },
    ] : [
      { icon: '🧹', name: 'compact', desc: 'compress conversation history', category: 'utility' },
    ];
    try {
      const d = await (await fetch('/control/capabilities')).json();
      (d.skills && d.skills.items || []).forEach(s => {
        all.push({ icon: '🎯', name: s.name, desc: s.description || '', isSkill: true, category: 'skills' });
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
  } else if (kind === 'slash' && it.template) {
    // Structured-arg commands (e.g. subagent/multiagent) don't fit the arm-and-type-argument
    // pattern, so insert an editable prompt skeleton instead of arming a chip.
    const skeleton = it.name === 'multiagent'
      ? 'Please delegate this task to multiple sub-agents in order:\n1. planner: \n2. coder: \n3. reviewer: '
      : 'Please delegate the following to a sub-agent: ';
    input.value = before + skeleton + after.replace(/^\s?/, '');
    input.focus();
    const pos = before.length + skeleton.length;
    input.setSelectionRange(pos, pos);
  } else if (kind === 'slash') {
    if (it.name === 'plan') {
      if (window._setPlanMode) window._setPlanMode(true);
    } else if (it.name === 'build') {
      if (window._setPlanMode) window._setPlanMode(false);
    }
    armCmd(it.name, it);
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
    if (e.key === 'Backspace' && !input.value && armedCmd) {
      e.preventDefault();
      disarmCmd();
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
let companionConnected = false;

function updateAgentModeAvailability(connected, hostname) {
  companionConnected = !!connected;
  const isNative = typeof isNativeAppClient === 'function' ? isNativeAppClient() : (
    (typeof window !== 'undefined' && window.electronAPI && window.electronAPI.isNativeApp) ||
    (typeof navigator !== 'undefined' && (navigator.userAgent.includes("A770NativeApp") || navigator.userAgent.includes("Electron")))
  );
  const btnAgent = $('mode-agent');
  const btnChat = $('mode-chat');

  if (btnChat) {
    btnChat.disabled = false;
  }

  if (btnAgent) {
    if (isNative) {
      // For native app: both Chat and Agent Task are enabled
      btnAgent.disabled = false;
      btnAgent.classList.remove('disabled');
      if (companionConnected) {
        btnAgent.title = `Agent Task mode (🟢 Connected to ${hostname || 'your device'} — operations run on your machine)`;
      } else {
        btnAgent.title = 'Agent Task mode (🖥️ Native app — local workspace operations active)';
      }
    } else {
      // For web: only chat will be enabled, agent task is disabled
      btnAgent.disabled = true;
      btnAgent.classList.add('disabled');
      btnAgent.title = 'Agent Task mode is only enabled in the desktop native app. In web browser, only Chat is enabled.';
      if (agentMode) {
        setAppMode(false, false);
      }
    }
  }
}

function setAppMode(isAgent, isUserSwitch = false) {
  const isNative = typeof isNativeAppClient === 'function' ? isNativeAppClient() : (
    (typeof window !== 'undefined' && window.electronAPI && window.electronAPI.isNativeApp) ||
    (typeof navigator !== 'undefined' && (navigator.userAgent.includes("A770NativeApp") || navigator.userAgent.includes("Electron")))
  );

  // In web browser, only chat is permitted
  if (isAgent && !isNative) {
    if (isUserSwitch) {
      toast('Agent Task mode is only enabled in the desktop native app. In web browser, only Chat is enabled.');
    }
    isAgent = false;
  }

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
  // cloud model picker: visible in agent mode when mode needs a cloud executor
  if (typeof _updateCloudModelSelVisibility === 'function') _updateCloudModelSelVisibility();
  const webToggle = $('btn-web-toggle');
  if (webToggle) webToggle.style.display = agentMode ? 'none' : 'flex';
  const deepToggle = $('btn-deep-toggle');
  if (deepToggle) deepToggle.style.display = agentMode ? 'none' : 'flex';
  if (agentMode && window._setPlanMode) window._setPlanMode(planMode);   // refresh placeholder
  // workspace side panel needs agent mode + an active project
  if (!agentMode && wsPanelOpen) setWsPanel(false);
  updateWsRail();
  refreshProjectInitHint();
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
  } else if (isUserSwitch) {
    loadSessions(true);
  }
}

if ($('mode-chat')) {
  $('mode-chat').onclick = () => setAppMode(false, true);
}
if ($('mode-agent')) {
  $('mode-agent').onclick = () => {
    const isNative = typeof isNativeAppClient === 'function' ? isNativeAppClient() : false;
    if (!isNative) {
      toast('Agent Task mode is only enabled in the desktop native app. In web browser, only Chat is enabled.');
      return;
    }
    setAppMode(true, true);
  };
}

// Initial availability update on load
updateAgentModeAvailability(false, null);

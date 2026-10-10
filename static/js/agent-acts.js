/* ---------------- agent action rendering ---------------- */
function toolIcon(name) {
  switch (name) {
    case 'list_files': return '📁';
    case 'project_overview': return '🗺️';
    case 'find_symbol': case 'find_references': case 'file_outline': return '🧭';
    case 'read_file': return '📄';
    case 'grep': return '🔍';
    case 'write_file': return '💾';
    case 'edit_file': return '✏️';
    case 'append_file': return '➕';
    case 'insert_at_line': return '✏️';
    case 'memory_list': case 'memory_read': case 'memory_write': case 'memory_str_replace':
    case 'memory_append': case 'memory_delete': return '🧠';
    case 'run_python': return '🐍';
    case 'list_diff': return '📊';
    case 'revert': return '↩️';
    case 'analyze_image': return '🖼️';
    case 'search_memory': return '🧠';
    case 'search_knowledge_base': return '🏢';
    case 'create_plan': return '📋';
    case 'update_plan_item': return '✔️';
    case 'get_plan': return '🗒️';
    case 'spawn_agent':
    case 'spawn_parallel_agents': return '🤖';
    case 'web_search_images': return '🖼️';
    default:
      if (/^browser_/.test(name)) return '🌐';
      if (/^mobile_/.test(name)) return '📱';
      return '🛠️';
  }
}

function parseStepsFromActs(acts) {
  if (!acts || !acts.length) return [];
  const steps = [];
  let curStep = null;
  let curTool = null;

  acts.forEach(a => {
    if (a.type === 'step') {
      curStep = {
        step: a.step,
        total: a.total,
        lane: null,
        thought: '',
        tools: [],
      };
      steps.push(curStep);
      curTool = null;
    } else {
      if (!curStep) {
        curStep = { step: 1, total: 1, lane: null, thought: '', tools: [] };
        steps.push(curStep);
      }
      if (a.type === 'lane') {
        curStep.lane = a.lane;
      } else if (a.type === 'thought' || a.type === 'reasoning') {
        curStep.thought += (curStep.thought ? '\n\n' : '') + (a.text || '');
      } else if (a.type === 'tool_call') {
        const laneInfo = (typeof curStep.lane === 'object' && curStep.lane) ? curStep.lane : (curStep.lane ? { model: curStep.lane, display: curStep.lane } : {});
        curTool = {
          id: a.id,
          name: a.name,
          args: a.args || {},
          model: a.model || laneInfo.display || laneInfo.model || '',
          device: a.device || laneInfo.device || '',
          verify: null,
          result: null,
          ok: true
        };
        curStep.tools.push(curTool);
      } else if (a.type === 'verify') {
        if (curTool && (curTool.name === a.name || (a.id && curTool.id === a.id))) {
          curTool.verify = a;
        } else {
          const match = curStep.tools.slice().reverse().find(t => t.name === a.name);
          if (match) match.verify = a;
        }
      } else if (a.type === 'tool_result') {
        if (curTool && (curTool.name === a.name || (a.id && curTool.id === a.id)) && curTool.result === null) {
          curTool.result = a.result;
          curTool.ok = a.ok !== false;
          if (a.diff) curTool.diff = a.diff;
          if (a.image) curTool.image = a.image;
        } else {
          const match = curStep.tools.slice().reverse().find(t => t.name === a.name && t.result === null);
          if (match) {
            match.result = a.result;
            match.ok = a.ok !== false;
            if (a.diff) match.diff = a.diff;
            if (a.image) match.image = a.image;
          } else {
            curStep.tools.push({ id: a.id, name: a.name, args: {}, verify: null, result: a.result, ok: a.ok !== false, diff: a.diff, image: a.image });
          }
        }
      }
    }
  });
  return steps;
}

function formatToolArgs(name, args) {
  if (!args || typeof args !== 'object') return String(args || '');
  let out = '';
  for (const [k, v] of Object.entries(args)) {
    if (typeof v === 'string' && (v.includes('\n') || v.length > 50)) {
      out += `--- ${k} ---\n${v}\n\n`;
    } else {
      out += `${k}: ${typeof v === 'object' ? JSON.stringify(v) : v}\n`;
    }
  }
  return out.trim() || JSON.stringify(args, null, 2);
}

function quickArgPreview(name, args) {
  if (!args || typeof args !== 'object') return '';
  const p = args.path || args.file || args.filename;
  if (p) return p;
  if (args.pattern) return `pattern: "${args.pattern}"`;
  if (args.code) {
    const clean = args.code.replace(/\s+/g, ' ').trim();
    return `py: ${clean.slice(0, 45)}${clean.length > 45 ? '…' : ''}`;
  }
  if (args.query) return `"${args.query}"`;
  if (args.url) return args.url;
  if (args.command || args.cmd) {
    const c = (args.command || args.cmd).trim().split('\n')[0];
    return c.length > 50 ? c.slice(0, 48) + '…' : c;
  }
  const k = Object.keys(args);
  if (k.length) {
    const val = String(args[k[0]]);
    return `${k[0]}: ${val.slice(0, 40)}${val.length > 40 ? '…' : ''}`;
  }
  return '';
}

function toolMeta(name) {
  switch (name) {
    case 'search_knowledge_base': return { icon: '📚', label: 'Knowledge Base', verb: 'Searched knowledge base', running: 'Searching knowledge base', cls: 'kb' };
    case 'web_search': return { icon: '🌐', label: 'Web Search', verb: 'Searched web', running: 'Searching web', cls: 'web' };
    case 'web_fetch': return { icon: '🌐', label: 'Web Fetch', verb: 'Fetched webpage', running: 'Fetching webpage', cls: 'web' };
    case 'search_memory': case 'memory_list': case 'memory_read': case 'memory_write':
    case 'memory_append': case 'memory_delete': return { icon: '🧠', label: 'Memory', verb: 'Accessed memory', running: 'Accessing memory', cls: 'memory' };
    case 'write_file': return { icon: '💾', label: 'write_file', verb: 'Created file', running: 'Creating file', cls: 'write' };
    case 'edit_file': return { icon: '✏️', label: 'edit_file', verb: 'Edited file', running: 'Editing file', cls: 'edit' };
    case 'append_file': return { icon: '➕', label: 'append_file', verb: 'Appended to file', running: 'Appending to file', cls: 'write' };
    case 'insert_at_line': return { icon: '✏️', label: 'insert_at_line', verb: 'Inserted into file', running: 'Inserting into file', cls: 'edit' };
    case 'read_file': return { icon: '📖', label: 'read_file', verb: 'Read file', running: 'Reading file', cls: 'read' };
    case 'run_python': return { icon: '🐍', label: 'Python', verb: 'Ran Python script', running: 'Running Python script', cls: 'run' };
    case 'run_command': case 'run_shell': return { icon: '💻', label: 'Terminal', verb: 'Ran command', running: 'Running command', cls: 'run' };
    case 'list_files': return { icon: '📁', label: 'list_files', verb: 'Listed files', running: 'Listing files', cls: 'list' };
    case 'project_overview': return { icon: '🗺️', label: 'project_overview', verb: 'Surveyed project', running: 'Surveying project', cls: 'list' };
    case 'git_inspect': return { icon: '🌿', label: 'git', verb: 'Inspected git', running: 'Inspecting git', cls: 'list' };
    case 'git_commit': return { icon: '🌿', label: 'git commit', verb: 'Committed', running: 'Committing', cls: 'run' };
    case 'git_branch': return { icon: '🌿', label: 'git branch', verb: 'Changed branch', running: 'Changing branch', cls: 'run' };
    case 'run_tests': return { icon: '🧪', label: 'Tests', verb: 'Ran tests', running: 'Running tests', cls: 'run' };
    case 'find_symbol': return { icon: '🧭', label: 'find_symbol', verb: 'Found definition', running: 'Finding definition', cls: 'grep' };
    case 'find_references': return { icon: '🧭', label: 'find_references', verb: 'Found call sites', running: 'Finding call sites', cls: 'grep' };
    case 'file_outline': return { icon: '🧭', label: 'file_outline', verb: 'Outlined file', running: 'Outlining file', cls: 'list' };
    case 'grep': return { icon: '🔍', label: 'grep', verb: 'Searched files', running: 'Searching files', cls: 'grep' };
    case 'revert': return { icon: '↩️', label: 'revert', verb: 'Reverted file', running: 'Reverting file', cls: 'revert' };
    case 'create_plan': return { icon: '📋', label: 'Plan', verb: 'Created plan', running: 'Creating plan', cls: 'plan' };
    case 'update_plan_item': return { icon: '✔️', label: 'Plan', verb: 'Updated plan', running: 'Updating plan', cls: 'plan' };
    case 'get_plan': return { icon: '📋', label: 'Plan', verb: 'Retrieved plan', running: 'Retrieving plan', cls: 'plan' };
    case 'spawn_agent': return { icon: '🤖', label: 'spawn_agent', verb: 'Delegated task', running: 'Delegating task', cls: 'subagent' };
    case 'spawn_parallel_agents': return { icon: '👥', label: 'spawn_parallel_agents', verb: 'Delegated task (Parallel)', running: 'Delegating task (Parallel)', cls: 'subagent' };
    case 'generate_image': return { icon: '🎨', label: 'generate_image', verb: 'Made image', running: 'Making image', cls: 'media' };
    case 'generate_video': return { icon: '🎬', label: 'generate_video', verb: 'Made video', running: 'Making video', cls: 'media' };
    case 'analyze_image': return { icon: '🖼️', label: 'analyze_image', verb: 'Analyzed image', running: 'Analyzing image', cls: 'media' };
    case 'browser_navigate': return { icon: '🌐', label: name, verb: 'Opened', running: 'Opening', cls: 'web' };
    case 'browser_screenshot': case 'mobile_screenshot': return { icon: '📸', label: name, verb: 'Captured', running: 'Capturing', cls: 'default' };
    case 'browser_console': return { icon: '🧾', label: name, verb: 'Checked console', running: 'Checking console', cls: 'default' };
    default:
      if (/^browser_/.test(name)) return { icon: '🌐', label: name, verb: name.slice(8).replace(/_/g, ' '), cls: 'web' };
      if (/^mobile_/.test(name)) return { icon: '📱', label: name, verb: name.slice(7).replace(/_/g, ' '), cls: 'default' };
      return { icon: '⚡', label: name, verb: name.replace(/_/g, ' '), cls: 'default' };
  }
}

function toggleAllCodex(btn) {
  const container = btn.closest('.agy-agent-container') || btn.closest('.agy-stream-timeline');
  if (!container) return;
  const cards = container.querySelectorAll('.codex-action-card, .codex-thought-card, .codex-file-card, .codex-cmd-card');
  // the label follows what the cards actually are, so it stays right after a card is opened by hand
  const isExpanding = ![...cards].some(c => c.open);
  cards.forEach(c => { c.open = isExpanding; if (window._rememberCardOpen) window._rememberCardOpen(c, isExpanding); });
  btn.textContent = isExpanding ? '⤡ Collapse All' : '⤢ Expand All';
}
const toggleAllSteps = toggleAllCodex;

function copyCodexCode(btn) {
  const container = btn.closest('.codex-action-body') || btn.closest('.codex-cmd-body') || btn.closest('.codex-thought-card') || btn.parentElement;
  if (!container) return;
  let target = btn.parentElement ? btn.parentElement.nextElementSibling : null;
  if (!target || target.tagName !== 'PRE') {
    target = container.querySelector('.codex-code-content') || container.querySelector('.codex-code-block') || container.querySelector('.codex-thought-output') || container.querySelector('.codex-result-block');
  }
  if (!target) return;
  const text = target.innerText || target.textContent;
  navigator.clipboard.writeText(text).then(() => {
    const orig = btn.textContent;
    btn.textContent = '✓ Copied!';
    setTimeout(() => { btn.textContent = orig; }, 1500);
  });
}
const copyToolResult = copyCodexCode;

/* ---------------- structured plan checklist ---------------- */
function planPanelHtml(acts) {
  if (!acts || !acts.length) return '';
  const plans = acts.filter(a => a.type === 'plan' && Array.isArray(a.items) && a.items.length);
  if (!plans.length) return '';
  if (typeof window !== 'undefined' && window.RightDock) {
    window.RightDock.updatePlanFromActs(acts);
  }
  const items = plans[plans.length - 1].items;
  const done = items.filter(i => i.status === 'done').length;
  const failed = items.filter(i => i.status === 'failed').length;
  const total = items.length;
  const pct = total ? Math.round(((done + failed) / total) * 100) : 0;
  const cur = items.find(i => i.status === 'in_progress');
  let h = '<div class="plan-panel">';
  h += `<div class="plan-head"><span class="plan-title">📋 Task Plan</span><span class="plan-progress">${cur ? `step ${cur.ord || items.indexOf(cur) + 1} of ${total} · ` : ''}${done}/${total} done${failed ? ` · ${failed} failed` : ''}</span></div>`;
  h += `<div class="plan-bar"><div class="plan-bar-fill${failed ? ' has-failed' : ''}" style="width:${pct}%"></div></div>`;
  h += '<ol class="plan-items">';
  items.forEach(it => {
    const st = it.status || 'pending';
    const mark = st === 'done' ? '✅' : st === 'failed' ? '❌' : st === 'in_progress' ? '⏳' : '☐';
    h += `<li class="plan-item st-${st}"><span class="plan-mark">${mark}</span><span class="plan-text">${esc(it.text || '')}</span>${it.note ? `<span class="plan-note">${esc(it.note)}</span>` : ''}</li>`;
  });
  h += '</ol></div>';
  return h;
}

/* Unified diff for an Agent Task write (Claude Code / Codex style): old/new
   line numbers, green additions, red removals, dim context. */
// highlighting every diff line is the expensive part of a render, and a diff never changes once recorded
const _diffHtmlCache = new WeakMap();
function agentDiffHtml(diff, path) {
  if (diff && typeof diff === 'object') {
    const hit = _diffHtmlCache.get(diff);
    if (hit) return hit;
    const html = _agentDiffHtmlBuild(diff, path);
    _diffHtmlCache.set(diff, html);
    return html;
  }
  return _agentDiffHtmlBuild(diff, path);
}
function _agentDiffHtmlBuild(diff, path) {
  const lang = (typeof hlLangFor === 'function') ? hlLangFor(path || '') : '';
  const hl = s => (lang && typeof hlCode === 'function') ? hlCode(s, lang) : esc(s);
  let oldNo = 0, newNo = 0;
  let rows = '';
  (diff.hunks || []).forEach(l => {
    if (l.t === '@') {
      const m = /-(\d+)(?:,\d+)? \+(\d+)/.exec(l.s || '');
      if (m) { oldNo = +m[1]; newNo = +m[2]; }
      if (rows) rows += '<div class="agy-diff-sep">⋯</div>';
      return;
    }
    let o = '', n = '', cls = 'ctx', sign = ' ';
    if (l.t === '+') { n = newNo++; cls = 'add'; sign = '+'; }
    else if (l.t === '-') { o = oldNo++; cls = 'del'; sign = '-'; }
    else { o = oldNo++; n = newNo++; }
    rows += `<div class="agy-diff-line ${cls}"><span class="ln">${o}</span><span class="ln">${n}</span><span class="sg">${sign}</span><span class="tx">${hl(l.s || '') || ' '}</span></div>`;
  });
  if (!rows) rows = '<div class="agy-diff-line ctx"><span class="tx">(no textual changes)</span></div>';
  if (diff.truncated) rows += '<div class="agy-diff-sep">… diff truncated — open the file to see everything</div>';
  return `<div class="agy-diff">${rows}</div>`;
}

document.addEventListener('click', (e) => {
  const btn = e.target.closest('[data-ws-open]');
  if (!btn) return;
  e.preventDefault();
  e.stopPropagation();
  const path = btn.dataset.wsOpen;
  if (!path) return;
  if (typeof setWsPanel === 'function') setWsPanel(true);
  if (typeof wsShowFile === 'function') wsShowFile(path);
});

/* Run ended early (step cap / wall-clock / loop): explain why and offer Continue. */
function agentStoppedHtml(acts, canContinue) {
  const s = [...acts].reverse().find(a => a.type === 'stopped');
  if (!s) return '';
  const left = s.plan_total ? ` — ${s.pending} of ${s.plan_total} plan step${s.plan_total !== 1 ? 's' : ''} left` : '';
  const mins = s.elapsed_s ? ` (${Math.floor(s.elapsed_s / 60)}m)` : '';
  let msg;
  if (s.reason === 'loop') msg = 'Stopped: the agent kept repeating the same tool calls.';
  else if (s.reason === 'loop_near_repeat') msg = 'Stopped: kept calling the same tool without making progress.';
  else if (s.reason === 'no_progress') msg = 'Stopped: ' + stopDetailText(s.detail);
  else if (s.reason === 'timeout') msg = `Stopped: ran for ${Math.floor((s.elapsed_s || 0) / 60)} minutes.`;
  else if (s.reason === 'interrupted') msg = `Interrupted: ${s.note || 'the connection ended before the run finished'}. Work done so far is kept.`;
  else if (s.reason === 'cancelled') msg = 'Stopped by you. Work done so far is kept.';
  else if (s.reason === 'failed') msg = `The run failed${s.note ? ': ' + s.note : ''}. Work done so far is kept.`;
  else if (s.reason === 'budget') msg = 'Stopped: this run used its token budget.';
  else msg = `Paused after ${s.steps || 'the maximum'} steps${mins}${left}.`;
  // plan marked complete but the run was cut off: say so rather than "0 left"
  if (s.plan_total && !s.pending && (s.reason === 'max_steps' || s.reason === 'timeout')) {
    msg = `Paused after ${s.steps || 'the maximum'} steps — the plan is marked complete, but the run was cut off. Verify the work before continuing.`;
  }
  // nothing left to resume: don't offer a button that cannot work
  window._agentLastStop = s;   // agentContinue() tells the model why the last run stopped
  const btn = (canContinue && (s.pending > 0 || s.reason === 'loop' || s.reason === 'loop_near_repeat' || s.reason === 'no_progress' ||
                       s.reason === 'interrupted' || s.reason === 'cancelled' || s.reason === 'failed' || s.reason === 'budget'))
    ? `<button type="button" class="btn accent agy-continue-btn" data-click="agent-continue">▶ Continue</button>`
    : '';
  const stepsInfo = s.steps ? `<span class="agy-stop-meta">${s.steps} step${s.steps !== 1 ? 's' : ''}</span>` : '';
  const timeInfo = s.elapsed_s ? `<span class="agy-stop-meta">${Math.floor(s.elapsed_s / 60)}m ${s.elapsed_s % 60}s</span>` : '';
  return `<div class="agy-stopped ${s.reason !== 'max_steps' ? 'loop' : ''}">
    <span class="agy-stop-dot"></span>
    <span class="agy-stop-text">${esc(msg)}</span>
    ${stepsInfo}${timeInfo}${btn}
  </div>`;
}

/* Resume a paused run. Sent as a real turn, but the message is explicit that it
   is a resume so the model treats the tracked plan as authoritative. */
const AGENT_CONTINUE_PROMPT =
  'Continue the previous task. The tracked plan is authoritative: re-check its ' +
  'step statuses, reopen anything you marked done but did not actually finish, ' +
  'and work the remaining steps in order.';

/* 'identical_result:run_python:3' -> the sentence the server's loop guard uses (core/agent_loop/loop_guard.py). */
function stopDetailText(detail) {
  const [rule, tool, n] = String(detail || '').split(':');
  if (rule === 'identical_result') return `\`${tool}\` ran ${n} times with the same arguments and gave the same result.`;
  if (rule === 'error_streak') return `the last ${n} tool calls all failed.`;
  if (rule === 'cycle') return `it kept going back and forth between the same calls (starting with \`${tool}\`).`;
  return 'it stopped making progress.';
}

function agentContinuePrompt() {
  const s = window._agentLastStop;
  if (!s || s.reason !== 'no_progress') return AGENT_CONTINUE_PROMPT;
  return AGENT_CONTINUE_PROMPT + ' The last run stopped because ' + stopDetailText(s.detail) +
    ' Do not repeat those calls: try a different approach, read the actual error, or tell me what you need.';
}

function agentContinue() {
  if (typeof generating !== 'undefined' && generating) return;
  // A resume belongs to the agent: sending it through the chat sender starts a plain chat
  // turn with the chat toolset (no shell, browser or plan tools), which cannot do the work.
  if (typeof agentMode !== 'undefined' && agentMode && typeof runAgentSSE === 'function') {
    runAgentSSE(agentContinuePrompt());
  } else if (typeof send === 'function') {
    send(agentContinuePrompt());
  }
}

function buildChronologicalStream(acts) {
  if (!acts || !acts.length) return [];
  const stream = [];
  const toolMap = new Map();

  acts.forEach(a => {
    if (a.type === 'thought' || a.type === 'reasoning') {
      const text = (a.text || '').trim();
      if (!text) return;
      const last = stream[stream.length - 1];
      if (last && last.type === 'thought' && !last.finalized) {
        last.text += '\n\n' + text;
        if (a.duration_s) last.duration_s = Math.max(last.duration_s || 1, a.duration_s);
      } else {
        stream.push({
          type: 'thought',
          text: text,
          duration_s: a.duration_s || a.secs || 2,
          step: a.step,
          model: a.model || ''
        });
      }
    } else if (a.type === 'tool_call') {
      const last = stream[stream.length - 1];
      if (last && last.type === 'thought') last.finalized = true;

      const toolItem = {
        type: 'tool',
        id: a.id,
        name: a.name,
        args: a.args || {},
        model: a.model || '',
        device: a.device || '',
        result: null,
        ok: true,
        diff: null,
        verify: null,
        progress: a.progress || null
      };
      if (a.id) toolMap.set(a.id, toolItem);
      toolMap.set(a.name, toolItem);
      stream.push(toolItem);
    } else if (a.type === 'tool_result') {
      let match = (a.id && toolMap.get(a.id)) || toolMap.get(a.name);
      if (!match) {
        for (let i = stream.length - 1; i >= 0; i--) {
          if (stream[i].type === 'tool' && stream[i].result === null) {
            match = stream[i];
            break;
          }
        }
      }
      if (match) {
        match.result = a.result;
        match.ok = a.ok !== false;
        if (a.diff) match.diff = a.diff;
        if (a.image) match.image = a.image;
      } else {
        stream.push({
          type: 'tool',
          id: a.id,
          name: a.name,
          args: {},
          result: a.result,
          ok: a.ok !== false,
          diff: a.diff,
          image: a.image
        });
      }
    } else if (a.type === 'verify') {
      const match = (a.id && toolMap.get(a.id)) || toolMap.get(a.name);
      if (match) match.verify = a;
    }
  });

  return stream;
}

function getCardKey(item, idx) {
  if (item.type === 'thought') {
    return 'thought-' + (item.step != null ? item.step : idx);
  }
  return item.id ? ('tool-' + item.id) : ('tool-' + (item.name || 'act') + '-' + idx);
}

function renderThoughtCard(item, isRunning, isOpen = false, cardKey = '', idx = 0) {
  const duration = item.duration_s ? Math.max(1, Math.round(item.duration_s)) : 2;
  const title = isRunning ? `Thinking (${duration}s)...` : `Thought for ${duration}s`;
  const openAttr = isOpen ? ' open' : '';
  const keyAttr = cardKey ? ` data-card-id="${esc(cardKey)}" data-card-idx="${idx}"` : '';
  return `<details class="codex-thought-card"${openAttr}${keyAttr}>
    <summary class="codex-thought-head">
      <span class="agy-tool-badge memory"><span class="agy-badge-icon">🧠</span></span>
      <span class="codex-thought-title">${esc(title)}</span>
      <span class="agy-step-spacer"></span>
      ${isRunning ? '<span class="agy-spinner" title="Thinking..."></span>' : '<span class="agy-status-chip done" title="Done"><svg viewBox="0 0 16 16" width="12" height="12"><path fill="currentColor" d="M13.78 4.22a.75.75 0 0 1 0 1.06l-7.25 7.25a.75.75 0 0 1-1.06 0L2.22 9.28a.751.751 0 0 1 .018-1.042.751.751 0 0 1 1.042-.018L6 10.94l6.72-6.72a.75.75 0 0 1 1.06 0Z"/></svg></span>'}
      <svg class="agy-chevron" viewBox="0 0 16 16" width="14" height="14"><path fill="currentColor" fill-rule="evenodd" d="M4.22 6.22a.75.75 0 0 1 1.06 0L8 8.94l2.72-2.72a.75.75 0 1 1 1.06 1.06l-3.25 3.25a.75.75 0 0 1-1.06 0L4.22 7.28a.75.75 0 0 1 0-1.06Z"/></svg>
    </summary>
    <div class="codex-thought-body">${esc(item.text).replace(/\n/g, '<br>')}</div>
  </details>`;
}

function renderCommandCard(t, isItemRunning, isOpen = true, cardKey = '', idx = 0) {
  const isRunning = t.result === null;
  const isPython = t.name === 'run_python';
  let fullCmd = '';
  if (isPython) {
    fullCmd = (t.args.code || t.args.command || (t.args.file ? `python ${t.args.file}` : '')).trim();
  } else {
    fullCmd = (t.args.command || t.args.cmd || t.args.code || '').trim();
  }
  const lines = fullCmd ? fullCmd.split('\n') : [];
  const shortCmd = lines[0] || t.name;
  const preview = shortCmd.length > 60 ? shortCmd.slice(0, 58) + '…' : shortCmd;
  const verb = isRunning
    ? (isPython ? 'Running Python' : 'Running command')
    : (isPython ? 'Ran Python' : 'Ran command');
  const badgeIcon = isPython ? '🐍' : '&gt;_';
  const badgeCls = isPython ? 'run' : 'run';

  const statusChip = isRunning
    ? '<span class="agy-spinner" title="Executing..."></span>'
    : (t.ok
      ? '<span class="agy-status-chip done" title="Success"><svg viewBox="0 0 16 16" width="12" height="12"><path fill="currentColor" d="M13.78 4.22a.75.75 0 0 1 1.06 0l-7.25 7.25a.75.75 0 0 1-1.06 0L2.22 9.28a.751.751 0 0 1 .018-1.042.751.751 0 0 1 1.042-.018L6 10.94l6.72-6.72a.75.75 0 0 1 1.06 0Z"/></svg></span>'
      : '<span class="agy-status-chip err" title="Execution failed"><svg viewBox="0 0 16 16" width="12" height="12"><path fill="currentColor" d="M8 1a7 7 0 1 0 0 14A7 7 0 0 0 8 1ZM4.97 4.97a.75.75 0 0 1 1.06 0L8 6.94l1.97-1.97a.75.75 0 1 1 1.06 1.06L9.06 8l1.97 1.97a.75.75 0 1 1-1.06 1.06L8 9.06l-1.97 1.97a.75.75 0 0 1-1.06-1.06L6.94 8 4.97 6.03a.75.75 0 0 1 0-1.06Z"/></svg></span>');

  const openAttr = (isOpen !== false) ? ' open' : '';
  const keyAttr = cardKey ? ` data-card-id="${esc(cardKey)}" data-card-idx="${idx}"` : '';
  const lineCountBadge = (isPython && lines.length > 1) ? `<span class="agy-step-lines">${lines.length} lines</span>` : '';

  let bodyContent = '';
  if (isPython) {
    bodyContent = `
      <div class="codex-cmd-out-label" style="display:flex; justify-content:space-between; align-items:center;">
        <span>PYTHON SCRIPT</span>
        <button type="button" class="btn ghost codex-head-btn" data-click="copy-codex-code" style="font-size:10px; padding:2px 8px;">Copy</button>
      </div>
      <pre class="agy-detail-code codex-code-block" style="margin:0 0 6px; max-height:280px; overflow-y:auto; border-radius:0;"><code>${(typeof hlCode === 'function') ? hlCode(fullCmd, 'python') : esc(fullCmd)}</code></pre>
    `;
  } else {
    bodyContent = `<div class="codex-cmd-prompt"><span class="codex-prompt-sym">$</span> ${esc(fullCmd || shortCmd)}</div>`;
  }

  return `<details class="codex-cmd-card"${openAttr}${keyAttr}>
    <summary class="codex-cmd-head">
      <span class="agy-tool-badge ${badgeCls}"><span class="agy-badge-icon">${badgeIcon}</span></span>
      <span class="codex-cmd-title">${verb} <code class="agy-target-code">${esc(preview)}</code></span>
      ${lineCountBadge}
      <span class="agy-step-spacer"></span>
      ${statusChip}
      <svg class="agy-chevron" viewBox="0 0 16 16" width="14" height="14"><path fill="currentColor" fill-rule="evenodd" d="M4.22 6.22a.75.75 0 0 1 1.06 0L8 8.94l2.72-2.72a.75.75 0 1 1 1.06 1.06l-3.25 3.25a.75.75 0 0 1-1.06 0L4.22 7.28a.75.75 0 0 1 0-1.06Z"/></svg>
    </summary>
    <div class="codex-cmd-body">
      ${bodyContent}
      ${!isRunning ? `
        <div class="codex-cmd-out-label" style="display:flex; justify-content:space-between; align-items:center;">
          <span>OUTPUT</span>
          ${t.result ? '<button type="button" class="btn ghost codex-head-btn" data-click="copy-codex-code" style="font-size:10px; padding:2px 8px;">Copy</button>' : ''}
        </div>
        <pre class="codex-cmd-terminal codex-result-block"${!t.ok ? ' style="color:var(--red);"' : ''}><code>${esc(t.result != null ? String(t.result).trim() : '(no output)')}</code></pre>
      ` : `
        <div class="codex-cmd-running"><span class="agy-spinner" style="width:12px; height:12px;"></span> Executing...</div>
      `}
    </div>
  </details>`;
}

function renderFileCard(t, isItemRunning, isOpen = null, cardKey = '', idx = 0) {
  const p = t.args.path || t.args.file || t.args.filename || '';
  const filename = p ? p.split(/[\\/]/).pop() : 'file';
  const diff = (t.diff && ['write_file', 'edit_file', 'append_file', 'insert_at_line'].includes(t.name)) ? t.diff : null;
  const isRunning = t.result === null;
  const isEdit = t.name === 'edit_file' || t.name === 'insert_at_line' || t.name === 'append_file' || (diff && !diff.created);
  const verb = isRunning ? (isEdit ? 'Editing file' : 'Writing file') : (isEdit ? 'Edited file' : 'Created file');
  const icon = isEdit ? '✏️' : '💾';
  const badgeCls = isEdit ? 'edit' : 'write';

  let diffPill = '';
  if (diff) {
    if (diff.added) diffPill += `<span class="codex-diff-pill add">+${diff.added}</span> `;
    if (diff.removed) diffPill += `<span class="codex-diff-pill del">-${diff.removed}</span>`;
  }

  const isPreviewable = p && /\.(html|htm|csv|xlsx|xls|pdf|md|py|js|ts|json|txt|svg|png|jpg|jpeg|webp|pptx)$/i.test(p);
  const previewBtn = diff
    ? `<button type="button" class="btn ghost agy-open-btn agy-preview-pill" data-ws-open="${esc(p)}" title="Open in project panel">↗ Open</button>`
    : isPreviewable
    ? `<button type="button" class="btn ghost codex-head-btn agy-preview-pill" data-preview-path="${esc(p)}" data-preview-title="${esc(filename)}" data-preview-source="ws" title="Preview file"><svg width="12" height="12" viewBox="0 0 16 16"><path fill="currentColor" d="M8 3c-4 0-7 5-7 5s3 5 7 5 7-5 7-5-3-5-7-5zm0 8.5a3.5 3.5 0 1 1 0-7 3.5 3.5 0 0 1 0 7zm0-5.5a2 2 0 1 0 0 4 2 2 0 0 0 0-4z"/></svg> Preview</button>`
    : '';

  const statusChip = isRunning
    ? '<span class="agy-spinner" title="Working..."></span>'
    : (t.ok
      ? '<span class="agy-status-chip done" title="Success"><svg viewBox="0 0 16 16" width="12" height="12"><path fill="currentColor" d="M13.78 4.22a.75.75 0 0 1 1.06 0l-7.25 7.25a.75.75 0 0 1-1.06 0L2.22 9.28a.751.751 0 0 1 .018-1.042.751.751 0 0 1 1.042-.018L6 10.94l6.72-6.72a.75.75 0 0 1 1.06 0Z"/></svg></span>'
      : '<span class="agy-status-chip err" title="File operation failed"><svg viewBox="0 0 16 16" width="12" height="12"><path fill="currentColor" d="M8 1a7 7 0 1 0 0 14A7 7 0 0 0 8 1ZM4.97 4.97a.75.75 0 0 1 1.06 0L8 6.94l1.97-1.97a.75.75 0 1 1 1.06 1.06L9.06 8l1.97 1.97a.75.75 0 1 1-1.06 1.06L8 9.06l-1.97 1.97a.75.75 0 0 1-1.06-1.06L6.94 8 4.97 6.03a.75.75 0 0 1 0-1.06Z"/></svg></span>');

  const openByDefault = false;   // file cards start collapsed (the +N pill and name are enough); a click is remembered
  const open = (isOpen !== null ? isOpen : openByDefault);
  const openAttr = open ? ' open' : '';
  const keyAttr = cardKey ? ` data-card-id="${esc(cardKey)}" data-card-idx="${idx}"` : '';
  // the body (a highlighted diff, often hundreds of lines) is built only when the card is open; a collapsed card
  // gets an empty placeholder that fills on first open (chat.js 'toggle'). Every streamed event re-renders all cards,
  // so building every file's diff each time is what made long file-writing runs crawl.
  const bodyInner = () => `
      ${p ? `<div class="codex-file-subpath">.../${esc(p)}</div>` : ''}
      ${diff ? agentDiffHtml(diff, p) : (t.name === 'write_file' && typeof t.args.content === 'string' ? `<pre class="agy-detail-code"><code>${esc(t.args.content)}</code></pre>` : '')}
      ${t.result !== null && !(diff && t.ok) ? `
        <div style="font-size:10px; font-weight:700; color:var(--dim); margin:6px 0 4px; text-transform:uppercase;">Result</div>
        <pre class="agy-detail-code" style="color:${t.ok ? 'var(--dim)' : 'var(--red)'};"><code>${esc(t.result || '(empty)')}</code></pre>
      ` : ''}
    `;
  let bodyHtml = '';
  let lazyAttr = '';
  if (open || !cardKey) bodyHtml = bodyInner();
  else {
    (window._lazyCardBodies = window._lazyCardBodies || new Map()).set(cardKey, bodyInner);
    lazyAttr = ' data-lazy="1"';
  }
  return `<details class="codex-file-card"${openAttr}${keyAttr}>
    <summary class="codex-file-head">
      <span class="agy-tool-badge ${badgeCls}"><span class="agy-badge-icon">${icon}</span></span>
      <span class="codex-file-title">${verb} <code class="agy-target-code">${esc(filename)}</code></span>
      ${diffPill}
      <span class="agy-step-spacer"></span>
      ${statusChip}
      ${previewBtn}
      <svg class="agy-chevron" viewBox="0 0 16 16" width="14" height="14"><path fill="currentColor" fill-rule="evenodd" d="M4.22 6.22a.75.75 0 0 1 1.06 0L8 8.94l2.72-2.72a.75.75 0 1 1 1.06 1.06l-3.25 3.25a.75.75 0 0 1-1.06 0L4.22 7.28a.75.75 0 0 1 0-1.06Z"/></svg>
    </summary>
    <div class="codex-file-body"${lazyAttr}>${bodyHtml}</div>
  </details>`;
}

function renderGenericToolCard(t, isItemRunning, isOpen = false, cardKey = '', idx = 0) {
  const meta = toolMeta(t.name);
  const isRunning = t.result === null;
  const p = t.args.path || t.args.file || t.args.filename || '';
  const rawTarget = p || quickArgPreview(t.name, t.args);
  const targetEsc = rawTarget ? esc(rawTarget) : '';

  let extra = '';
  if (t.name === 'read_file' && t.args.start_line != null && t.args.end_line != null) {
    extra = `<span class="agy-step-lines">#L${t.args.start_line}-${t.args.end_line}</span>`;
  } else if (t.name === 'grep' && t.result) {
    const matches = (t.result.match(/\n/g) || []).length + 1;
    extra = `<span class="agy-step-count">${matches} result${matches !== 1 ? 's' : ''}</span>`;
  } else if ((t.name === 'web_search' || t.name === 'search_knowledge_base') && t.result) {
    const matches = (t.result.match(/https?:\/\/|source|match|snippet/gi) || []).length;
    if (matches > 0) extra = `<span class="agy-step-count">${matches} sources</span>`;
  }

  const isPreviewable = p && /\.(html|htm|csv|xlsx|xls|pdf|md|py|js|ts|json|txt|svg|png|jpg|jpeg|webp|pptx)$/i.test(p);
  const previewBtn = isPreviewable
    ? `<button type="button" class="btn ghost codex-head-btn agy-preview-pill" data-preview-path="${esc(p)}" data-preview-title="${esc(p)}" data-preview-source="ws" title="Preview file"><svg width="12" height="12" viewBox="0 0 16 16"><path fill="currentColor" d="M8 3c-4 0-7 5-7 5s3 5 7 5 7-5 7-5-3-5-7-5zm0 8.5a3.5 3.5 0 1 1 0-7 3.5 3.5 0 0 1 0 7zm0-5.5a2 2 0 1 0 0 4 2 2 0 0 0 0-4z"/></svg> Preview</button>`
    : '';
  const isMedia = meta.cls === 'media';
  const displayTarget = isMedia ? esc(String(t.args.prompt || '').slice(0, 90)) : targetEsc;
  const verb = isRunning && meta.running ? meta.running : meta.verb;

  const statusChip = isRunning
    ? '<span class="agy-spinner" title="Executing..."></span>'
    : (t.ok
      ? '<span class="agy-status-chip done" title="Success"><svg viewBox="0 0 16 16" width="12" height="12"><path fill="currentColor" d="M13.78 4.22a.75.75 0 0 1 1.06 0l-7.25 7.25a.75.75 0 0 1-1.06 0L2.22 9.28a.751.751 0 0 1 .018-1.042.751.751 0 0 1 1.042-.018L6 10.94l6.72-6.72a.75.75 0 0 1 1.06 0Z"/></svg></span>'
      : '<span class="agy-status-chip err" title="Tool error"><svg viewBox="0 0 16 16" width="12" height="12"><path fill="currentColor" d="M8 1a7 7 0 1 0 0 14A7 7 0 0 0 8 1ZM4.97 4.97a.75.75 0 0 1 1.06 0L8 6.94l1.97-1.97a.75.75 0 1 1 1.06 1.06L9.06 8l1.97 1.97a.75.75 0 1 1-1.06 1.06L8 9.06l-1.97 1.97a.75.75 0 0 1-1.06-1.06L6.94 8 4.97 6.03a.75.75 0 0 1 0-1.06Z"/></svg></span>');

  const openAttr = isOpen ? ' open' : '';
  const keyAttr = cardKey ? ` data-card-id="${esc(cardKey)}" data-card-idx="${idx}"` : '';
  return `<details class="codex-action-card"${openAttr}${keyAttr}>
    <summary class="codex-action-head">
      <span class="agy-tool-badge ${meta.cls || 'default'}"><span class="agy-badge-icon">${meta.icon}</span></span>
      <span class="codex-action-title"><b>${verb}</b>${displayTarget ? ` <code class="agy-target-code">${displayTarget}</code>` : ''}</span>
      ${extra}
      <span class="agy-step-spacer"></span>
      ${statusChip}
      ${previewBtn}
      <svg class="agy-chevron" viewBox="0 0 16 16" width="14" height="14"><path fill="currentColor" fill-rule="evenodd" d="M4.22 6.22a.75.75 0 0 1 1.06 0L8 8.94l2.72-2.72a.75.75 0 1 1 1.06 1.06l-3.25 3.25a.75.75 0 0 1-1.06 0L4.22 7.28a.75.75 0 0 1 0-1.06Z"/></svg>
    </summary>
    <div class="codex-action-body">
      <div class="agy-detail-bar">
        <span>${esc(t.name)} ${p ? '· ' + esc(p) : ''}</span>
        ${t.model ? `<span style="font-family:monospace; opacity:0.8; margin-left:8px;">${esc(t.model)}</span>` : ''}
      </div>
      ${t.args && Object.keys(t.args).length > 0 ? `
        <div class="agy-args-row">
          <button type="button" class="agy-expand-btn" data-click="toggle-tool-args">Show arguments</button>
          <span>(${Object.keys(t.args).length})</span>
        </div>
        <pre class="agy-detail-code agy-args-pre"><code>${esc(formatToolArgs(t.name, t.args))}</code></pre>` : ''}
      ${agentShotHtml(t.image)}
      ${t.result !== null ? `
        <div style="font-size:10px; font-weight:700; color:var(--dim); margin:6px 0 4px; text-transform:uppercase;">Result</div>
        <pre class="agy-detail-code agy-result-code" data-ok="${t.ok ? 'ok' : 'err'}"><code>${esc(t.result || '(empty)')}</code></pre>
      ` : ''}
    </div>
  </details>${isMedia && isRunning ? mediaProgressHtml(t.progress) : ''}`;
}

// browser/mobile screenshot thumbnail: live-run only (stripped before the message is saved)
function agentShotHtml(img) {
  if (!img || !/^data:image\/jpeg;base64,[A-Za-z0-9+/=]+$/.test(img)) return '';
  return `<img class="agent-shot" src="${img}" alt="screenshot" loading="lazy">`;
}

// live step bar under a running generate_image / generate_video card (tool_progress events)
function mediaProgressHtml(pr) {
  const pct = pr && pr.pct != null ? Math.max(0, Math.min(100, Math.round(pr.pct))) : null;
  const secs = pr && pr.elapsed != null ? pr.elapsed : null;
  const clock = secs != null ? `${Math.floor(secs / 60)}:${String(secs % 60).padStart(2, '0')}` : '';
  const text = (pr && pr.text) || 'Starting…';
  return `<div class="codex-media-progress" role="status" aria-live="polite">
    <div class="codex-media-progress-row"><span>${esc(text)}</span>${clock ? `<span class="codex-media-clock">${clock}</span>` : ''}</div>
    <div class="media-bar"${pct != null ? ` role="progressbar" aria-valuenow="${pct}" aria-valuemin="0" aria-valuemax="100"` : ''}>
      <span class="${pct == null ? 'indeterminate' : ''}" style="width:${pct == null ? 30 : pct}%"></span>
    </div>
  </div>`;
}

// live: this message is the run still streaming. Anything else (a finished or reloaded message) cannot have a tool
// still executing, so a call with no result is shown as interrupted instead of "Executing..." forever.
function agentActsHtml(acts, live = true, msg = null) {
  if (!acts || !acts.length) return '';
  const stream = buildChronologicalStream(acts);
  if (!stream.length) return '';
  if (!live) {
    stream.forEach(it => {
      if (it.type === 'tool' && it.result === null) { it.result = 'interrupted: the run ended before this finished'; it.ok = false; }
    });
  }

  const toolOps = stream.filter(s => s.type === 'tool');
  const isAllDone = toolOps.length === 0 || toolOps.every(t => t.result !== null);

  let h = '<div class="agy-agent-container"><div class="agy-stream-timeline" data-acts-idx>';

  if (stream.length > 2) {
    h += `<div class="codex-timeline-toolbar">
      <span class="codex-timeline-count">${toolOps.length} action${toolOps.length !== 1 ? 's' : ''}</span>
      <button type="button" class="btn ghost codex-toggle-all" data-click="toggle-all-codex">⤡ Collapse All</button>
    </div>`;
  }

  const cardOpenMap = (msg && msg._cardOpen) ? msg._cardOpen : null;

  stream.forEach((item, idx) => {
    const isLast = idx === stream.length - 1;
    const isItemRunning = !isAllDone && isLast;
    const cardKey = getCardKey(item, idx);
    let cardOpen = null;
    if (cardOpenMap) {
      if (cardOpenMap[cardKey] !== undefined) cardOpen = cardOpenMap[cardKey];
      else if (cardOpenMap[idx] !== undefined) cardOpen = cardOpenMap[idx];
    }

    if (item.type === 'thought') {
      h += renderThoughtCard(item, isItemRunning, cardOpen !== null ? cardOpen : false, cardKey, idx);
    } else if (item.type === 'tool') {
      const isCmd = ['run_python', 'run_command', 'shell', 'exec', 'terminal'].includes(item.name);
      const isFile = ['edit_file', 'write_file', 'append_file', 'insert_at_line'].includes(item.name);
      if (isCmd) {
        h += renderCommandCard(item, isItemRunning, cardOpen !== null ? cardOpen : true, cardKey, idx);
      } else if (isFile) {
        h += renderFileCard(item, isItemRunning, cardOpen, cardKey, idx);
      } else {
        h += renderGenericToolCard(item, isItemRunning, cardOpen !== null ? cardOpen : false, cardKey, idx);
      }
    }
  });

  if (!isAllDone) {
    h += `<div class="agy-working-bar"><span class="agy-spinner" style="width:12px; height:12px;"></span> Working…</div>`;
  }

  h += '</div></div>';
  return h;
}

/* Thumbs up/down/* Thumbs up/down on agent answers -> POST /agent/feedback. Feeds the usage-based
   router tuner (Settings -> Router); only the category/lane stats are stored, never text. */
function runRatingHtml(runId) {
  if (!runId) return '';
  const cur = localStorage.getItem('runRating:' + runId) || '';
  const b = (v, icon, title) =>
    `<button type="button" class="run-rate-btn${cur === String(v) ? ' on' : ''}" data-run-rate="${esc(runId)}" data-rate="${v}" title="${title}">${icon}</button>`;
  return ` · <span class="run-rate">${b(1, '👍', 'Good answer')}${b(-1, '👎', 'Bad answer')}</span>`;
}

document.addEventListener('click', async (e) => {
  const btn = e.target.closest && e.target.closest('[data-run-rate]');
  if (!btn) return;
  e.preventDefault();
  const runId = btn.dataset.runRate;
  const key = 'runRating:' + runId;
  // clicking the active thumb again clears the rating
  const rating = localStorage.getItem(key) === btn.dataset.rate ? 0 : Number(btn.dataset.rate);
  try {
    const r = await fetch('/agent/feedback', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ run_id: runId, rating }),
    });
    if (!r.ok) throw new Error((await r.json().catch(() => ({}))).error || r.statusText);
    if (rating) localStorage.setItem(key, String(rating)); else localStorage.removeItem(key);
    btn.parentElement.querySelectorAll('[data-run-rate]').forEach(x =>
      x.classList.toggle('on', rating !== 0 && x.dataset.rate === String(rating)));
  } catch (err) {
    if (typeof toast === 'function') toast('Feedback not saved: ' + err.message);
  }
});

/* ---------------- agent action rendering ---------------- */
function toolIcon(name) {
  switch (name) {
    case 'list_files': return '📁';
    case 'read_file': return '📄';
    case 'grep': return '🔍';
    case 'write_file': return '💾';
    case 'edit_file': return '✏️';
    case 'run_python': return '🐍';
    case 'list_diff': return '📊';
    case 'revert': return '↩️';
    case 'analyze_image': return '🖼️';
    case 'search_memory': return '🧠';
    case 'search_knowledge_base': return '🏢';
    case 'create_plan': return '📋';
    case 'update_plan_item': return '✔️';
    case 'get_plan': return '🗒️';
    case 'spawn_agent': return '🤖';
    case 'web_search_images': return '🖼️';
    default: return '🛠️';
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
        } else {
          const match = curStep.tools.slice().reverse().find(t => t.name === a.name && t.result === null);
          if (match) {
            match.result = a.result;
            match.ok = a.ok !== false;
            if (a.diff) match.diff = a.diff;
          } else {
            curStep.tools.push({ id: a.id, name: a.name, args: {}, verify: null, result: a.result, ok: a.ok !== false, diff: a.diff });
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
  if (p) return esc(p);
  if (args.pattern) return `pattern: "${esc(args.pattern)}"`;
  if (args.code) {
    const clean = args.code.replace(/\s+/g, ' ').trim();
    return `py: ${esc(clean.slice(0, 45))}${clean.length > 45 ? '…' : ''}`;
  }
  if (args.query) return `"${esc(args.query)}"`;
  const k = Object.keys(args);
  if (k.length) return `${k[0]}: ${esc(String(args[k[0]]).slice(0, 40))}`;
  return '';
}

function toolMeta(name) {
  switch (name) {
    case 'write_file': return { icon: '📄', label: 'write_file', verb: 'Saved', cls: 'write' };
    case 'edit_file': return { icon: '✏️', label: 'edit_file', verb: 'Edited', cls: 'edit' };
    case 'read_file': return { icon: '📖', label: 'read_file', verb: 'Read', cls: 'read' };
    case 'run_python': return { icon: '⚡', label: 'run_python', verb: 'Executed', cls: 'run' };
    case 'list_files': return { icon: '📁', label: 'list_files', verb: 'Listed', cls: 'list' };
    case 'grep': return { icon: '🔍', label: 'grep', verb: 'Searched', cls: 'grep' };
    case 'revert': return { icon: '↩️', label: 'revert', verb: 'Reverted', cls: 'revert' };
    case 'spawn_agent': return { icon: '🤖', label: 'spawn_agent', verb: 'Delegated', cls: 'subagent' };
    default: return { icon: '🛠️', label: name, verb: 'Done', cls: 'default' };
  }
}

function toggleAllCodex(btn) {
  const container = btn.closest('.codex-container');
  if (!container) return;
  const cards = container.querySelectorAll('.codex-action-card, .codex-thought-card');
  const isExpanding = btn.textContent.includes('Expand');
  cards.forEach(c => { c.open = isExpanding; });
  btn.textContent = isExpanding ? '⤡ Collapse All' : '⤢ Expand All';
}
const toggleAllSteps = toggleAllCodex;

function copyCodexCode(btn) {
  const container = btn.closest('.codex-action-body') || btn.closest('.codex-thought-card') || btn.parentElement;
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
  const items = plans[plans.length - 1].items;
  const done = items.filter(i => i.status === 'done').length;
  const failed = items.filter(i => i.status === 'failed').length;
  const total = items.length;
  const pct = total ? Math.round(((done + failed) / total) * 100) : 0;
  let h = '<div class="plan-panel">';
  h += `<div class="plan-head"><span class="plan-title">📋 Task Plan</span><span class="plan-progress">${done}/${total} done${failed ? ` · ${failed} failed` : ''}</span></div>`;
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
function agentDiffHtml(diff, path) {
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

/* Run ended early (step cap / repeated calls): explain why and offer Continue.
   The next run re-injects the tracked plan server-side, so it resumes the open steps. */
const AGENT_CONTINUE_PROMPT = 'Continue with the remaining plan steps.';

function agentStoppedHtml(acts, canContinue) {
  const s = [...acts].reverse().find(a => a.type === 'stopped');
  if (!s) return '';
  const left = s.plan_total ? ` — ${s.pending} of ${s.plan_total} plan step${s.plan_total !== 1 ? 's' : ''} left` : '';
  const msg = s.reason === 'loop'
    ? 'Stopped: the agent kept repeating the same tool calls.'
    : `Paused after ${s.steps || 'the maximum'} steps${left}.`;
  const btn = canContinue
    ? `<button type="button" class="btn accent agy-continue-btn" data-click="agent-continue">▶ Continue</button>`
    : '';
  return `<div class="agy-stopped ${s.reason === 'loop' ? 'loop' : ''}"><span>${esc(msg)}</span>${btn}</div>`;
}

function agentContinue() {
  if (typeof generating !== 'undefined' && generating) return;
  if (typeof send === 'function') send(AGENT_CONTINUE_PROMPT);
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
        verify: null
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
      } else {
        stream.push({
          type: 'tool',
          id: a.id,
          name: a.name,
          args: {},
          result: a.result,
          ok: a.ok !== false,
          diff: a.diff
        });
      }
    } else if (a.type === 'verify') {
      const match = (a.id && toolMap.get(a.id)) || toolMap.get(a.name);
      if (match) match.verify = a;
    }
  });

  return stream;
}

function renderThoughtCard(item, isRunning) {
  const duration = item.duration_s ? Math.max(1, Math.round(item.duration_s)) : 2;
  const title = isRunning ? `Thinking (${duration}s)...` : `Thought for ${duration}s`;
  return `<details class="codex-thought-card">
    <summary class="codex-thought-head">
      <span class="codex-thought-icon">🧠</span>
      <span class="codex-thought-title">${esc(title)}</span>
      <span class="codex-chevron">▾</span>
    </summary>
    <div class="codex-thought-body">${esc(item.text).replace(/\n/g, '<br>')}</div>
  </details>`;
}

function renderCommandCard(t, isItemRunning) {
  const isRunning = t.result === null;
  let fullCmd = '';
  if (t.name === 'run_python') {
    fullCmd = (t.args.code || t.args.command || (t.args.file ? `python ${t.args.file}` : '')).trim();
  } else {
    fullCmd = (t.args.command || t.args.cmd || t.args.code || '').trim();
  }
  const shortCmd = fullCmd.split('\n')[0] || t.name;
  const preview = shortCmd.length > 60 ? shortCmd.slice(0, 58) + '…' : shortCmd;
  const headTitle = isRunning ? `Running command (${esc(preview)})...` : `Ran command (${esc(preview)})`;

  return `<details class="codex-cmd-card" open>
    <summary class="codex-cmd-head">
      <span class="codex-cmd-icon">&gt;_</span>
      <span class="codex-cmd-title">${headTitle}</span>
      <span class="agy-step-spacer"></span>
      ${isRunning ? '<span class="agy-summary-pulse active" style="width:6px; height:6px;"></span>' : (t.ok ? '' : '<span style="color:var(--red); font-size:11px;">⚠</span>')}
      <span class="codex-chevron">▾</span>
    </summary>
    <div class="codex-cmd-body">
      <div class="codex-cmd-prompt"><span class="codex-prompt-sym">$</span> ${esc(fullCmd || shortCmd)}</div>
      ${!isRunning ? `
        <div class="codex-cmd-out-label">OUTPUT</div>
        <pre class="codex-cmd-terminal"><code>${esc(t.result != null ? String(t.result).trim() : '(no output)')}</code></pre>
      ` : `
        <div class="codex-cmd-running"><span class="agy-summary-pulse active" style="width:6px; height:6px;"></span> Executing...</div>
      `}
    </div>
  </details>`;
}

function renderFileCard(t, isItemRunning) {
  const p = t.args.path || t.args.file || t.args.filename || '';
  const filename = p ? p.split(/[\\/]/).pop() : 'file';
  const diff = (t.diff && (t.name === 'write_file' || t.name === 'edit_file')) ? t.diff : null;
  const isRunning = t.result === null;
  const isEdit = t.name === 'edit_file' || (diff && !diff.created);
  const verb = isRunning ? (isEdit ? 'Editing file' : 'Writing file') : (isEdit ? 'Edited file' : 'Created file');
  const icon = isEdit ? '✏️' : '💾';

  let diffPill = '';
  if (diff) {
    if (diff.added) diffPill += `<span class="codex-diff-pill add">+${diff.added}</span> `;
    if (diff.removed) diffPill += `<span class="codex-diff-pill del">-${diff.removed}</span>`;
  }

  const isPreviewable = p && /\.(html|htm|csv|xlsx|xls|pdf|md|py|js|ts|json|txt|svg|png|jpg|jpeg|webp|pptx)$/i.test(p);
  const previewBtn = diff
    ? `<button type="button" class="btn ghost agy-open-btn" data-ws-open="${esc(p)}" title="Open in project panel">↗ Open</button>`
    : isPreviewable
    ? `<button type="button" class="btn ghost" style="padding:1px 7px; font-size:10px; margin-left:auto; border-radius:4px;" data-preview-path="${esc(p)}" data-preview-title="${esc(filename)}" title="Preview file">👁️ Preview</button>`
    : '';

  const openByDefault = !isRunning;

  return `<details class="codex-file-card"${openByDefault ? ' open' : ''}>
    <summary class="codex-file-head">
      <span class="codex-file-icon">${icon}</span>
      <span class="codex-file-title">${verb} <b>${esc(filename)}</b></span>
      ${diffPill}
      <span class="agy-step-spacer"></span>
      ${isRunning ? '<span class="agy-summary-pulse active" style="width:6px; height:6px;"></span>' : (t.ok ? '' : '<span style="color:var(--red); font-size:11px;">⚠</span>')}
      ${previewBtn}
      <span class="codex-chevron">▾</span>
    </summary>
    <div class="codex-file-body">
      ${p ? `<div class="codex-file-subpath">.../${esc(p)}</div>` : ''}
      ${diff ? agentDiffHtml(diff, p) : (t.name === 'write_file' && typeof t.args.content === 'string' ? `<pre class="agy-detail-code"><code>${esc(t.args.content)}</code></pre>` : '')}
      ${t.result !== null && !(diff && t.ok) ? `
        <div style="font-size:10px; font-weight:700; color:var(--dim); margin:6px 0 4px; text-transform:uppercase;">Result</div>
        <pre class="agy-detail-code" style="color:${t.ok ? 'var(--dim)' : 'var(--red)'};"><code>${esc(t.result || '(empty)')}</code></pre>
      ` : ''}
    </div>
  </details>`;
}

function renderGenericToolCard(t, isItemRunning) {
  const meta = toolMeta(t.name);
  const isRunning = t.result === null;
  const p = t.args.path || t.args.file || t.args.filename || '';
  const label = esc(p || quickArgPreview(t.name, t.args) || t.name);

  let extra = '';
  if (t.name === 'read_file' && t.args.start_line != null && t.args.end_line != null) {
    extra = `<span class="agy-step-lines">#L${t.args.start_line}-${t.args.end_line}</span>`;
  } else if (t.name === 'grep' && t.result) {
    const matches = (t.result.match(/\n/g) || []).length + 1;
    extra = `<span class="agy-step-count">${matches} result${matches !== 1 ? 's' : ''}</span>`;
  } else if (t.name === 'web_search' && t.result) {
    const matches = (t.result.match(/https?:\/\//g) || []).length;
    if (matches > 0) extra = `<span class="agy-step-count">${matches} sources</span>`;
  }

  const isPreviewable = p && /\.(html|htm|csv|xlsx|xls|pdf|md|py|js|ts|json|txt|svg|png|jpg|jpeg|webp|pptx)$/i.test(p);
  const previewBtn = isPreviewable
    ? `<button type="button" class="btn ghost" style="padding:1px 7px; font-size:10px; margin-left:auto; border-radius:4px;" data-preview-path="${esc(p)}" data-preview-title="${esc(p)}" title="Preview file">👁️ Preview</button>`
    : '';

  return `<details class="codex-action-card">
    <summary class="codex-action-head">
      <span class="codex-action-icon">${meta.icon}</span>
      <span class="codex-action-title"><b>${meta.verb}</b> ${label}</span>
      ${extra}
      <span class="agy-step-spacer"></span>
      ${isRunning ? '<span class="agy-summary-pulse active" style="width:6px; height:6px;"></span>' : (t.ok ? '' : '<span style="color:var(--red); font-size:11px;">⚠</span>')}
      ${previewBtn}
      <span class="codex-chevron">▾</span>
    </summary>
    <div class="codex-action-body">
      <div class="agy-detail-bar">
        <span>${esc(t.name)} ${p ? '· ' + esc(p) : ''}</span>
        ${t.model ? `<span style="font-family:monospace; opacity:0.8; margin-left:8px;">${esc(t.model)}</span>` : ''}
      </div>
      ${t.args && Object.keys(t.args).length > 0 ? `<pre class="agy-detail-code"><code>${esc(formatToolArgs(t.name, t.args))}</code></pre>` : ''}
      ${t.result !== null ? `
        <div style="font-size:10px; font-weight:700; color:var(--dim); margin:6px 0 4px; text-transform:uppercase;">Result</div>
        <pre class="agy-detail-code" style="color:${t.ok ? 'var(--dim)' : 'var(--red)'};"><code>${esc(t.result || '(empty)')}</code></pre>
      ` : ''}
    </div>
  </details>`;
}

function agentActsHtml(acts) {
  if (!acts || !acts.length) return '';
  const stream = buildChronologicalStream(acts);
  if (!stream.length) return '';

  const toolOps = stream.filter(s => s.type === 'tool');
  const isAllDone = toolOps.length === 0 || toolOps.every(t => t.result !== null);

  let h = '<div class="agy-agent-container"><div class="agy-stream-timeline">';

  if (stream.length > 2) {
    h += `<div class="codex-timeline-toolbar">
      <span class="codex-timeline-count">${toolOps.length} action${toolOps.length !== 1 ? 's' : ''}</span>
      <button type="button" class="btn ghost codex-toggle-all" data-click="toggle-all-codex">⤡ Collapse All</button>
    </div>`;
  }

  stream.forEach((item, idx) => {
    const isLast = idx === stream.length - 1;
    const isItemRunning = !isAllDone && isLast;
    if (item.type === 'thought') {
      h += renderThoughtCard(item, isItemRunning);
    } else if (item.type === 'tool') {
      const isCmd = ['run_python', 'run_command', 'shell', 'exec', 'terminal'].includes(item.name);
      const isFile = ['edit_file', 'write_file'].includes(item.name);
      if (isCmd) {
        h += renderCommandCard(item, isItemRunning);
      } else if (isFile) {
        h += renderFileCard(item, isItemRunning);
      } else {
        h += renderGenericToolCard(item, isItemRunning);
      }
    }
  });

  if (!isAllDone) {
    h += `<div class="agy-working-bar"><span class="agy-summary-pulse active" style="width:6px; height:6px;"></span> Working…</div>`;
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

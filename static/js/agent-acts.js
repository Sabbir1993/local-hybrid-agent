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
    case 'create_plan': return '📋';
    case 'update_plan_item': return '✔️';
    case 'get_plan': return '🗒️';
    case 'spawn_agent': return '🤖';
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
        } else {
          const match = curStep.tools.slice().reverse().find(t => t.name === a.name && t.result === null);
          if (match) {
            match.result = a.result;
            match.ok = a.ok !== false;
          } else {
            curStep.tools.push({ id: a.id, name: a.name, args: {}, verify: null, result: a.result, ok: a.ok !== false });
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

function agentActsHtml(acts) {
  if (!acts || !acts.length) return '';
  const steps = parseStepsFromActs(acts);
  if (!steps.length) return '';

  const allTools = [];
  const thoughts = [];
  steps.forEach(s => {
    if (s.thought && s.thought.trim()) thoughts.push(s.thought.trim());
    s.tools.forEach(t => allTools.push({
      ...t,
      step: s.step,
      lane: s.lane,
      thought: s.thought ? s.thought.trim() : ''
    }));
  });

  if (allTools.length === 0 && thoughts.length === 0) return '';

  const totalOps = allTools.length;
  const isAllDone = totalOps > 0 && allTools.every(t => t.result !== null);
  const hasFailed = allTools.some(t => t.result !== null && !t.ok);

  // Count files & searches for header summary like: "Exploring 14 files, 3 searches"
  const fileOps = allTools.filter(t => ['read_file', 'write_file', 'edit_file'].includes(t.name));
  const webOps = allTools.filter(t => ['web_search', 'web_fetch'].includes(t.name));
  const searchOps = allTools.filter(t => ['grep', 'list_files'].includes(t.name));
  const otherOps = allTools.filter(t => !['read_file', 'write_file', 'edit_file', 'grep', 'list_files', 'web_search', 'web_fetch'].includes(t.name));

  const summaryParts = [];
  if (webOps.length > 0) summaryParts.push(`${webOps.length} web search${webOps.length !== 1 ? 'es' : ''}`);
  if (fileOps.length > 0) summaryParts.push(`${fileOps.length} file${fileOps.length !== 1 ? 's' : ''}`);
  if (searchOps.length > 0) summaryParts.push(`${searchOps.length} search${searchOps.length !== 1 ? 'es' : ''}`);
  if (otherOps.length > 0) summaryParts.push(`${otherOps.length} action${otherOps.length !== 1 ? 's' : ''}`);
  if (summaryParts.length === 0) summaryParts.push(`${totalOps} step${totalOps !== 1 ? 's' : ''}`);

  const summaryTitle = isAllDone ? `Completed ${summaryParts.join(', ')}` : `Running ${summaryParts.join(', ')}`;

  let h = '<div class="agy-agent-container">';
  h += `<details class="agy-agent-drawer" open>
    <summary class="agy-agent-summary">
      <div class="agy-summary-left">
        <span class="agy-summary-pulse ${hasFailed ? 'err' : (isAllDone ? 'done' : 'active')}"></span>
        <span>${esc(summaryTitle)}</span>
      </div>
      <span class="agy-summary-chevron">▼</span>
    </summary>
    <div class="agy-steps-list">`;

  // Render individual action items in the Antigravity list format
  allTools.forEach(t => {
    let p = t.args.path || t.args.file || t.args.filename;
    if (!p && t.args.raw) {
      const m = t.args.raw.match(/"(?:path|file|filename)"\s*:\s*"([^"]+)"/);
      if (m) p = m[1];
    }

    let verb = 'Analyzed';
    let iconClass = 'file';
    let iconSymbol = '📄';
    let label = esc(p || t.name);
    let extra = '';

    if (t.name === 'read_file') {
      verb = 'Analyzed';
      iconClass = 'python';
      iconSymbol = p && p.endsWith('.py') ? '🐍' : '📄';
      if (t.args.start_line != null && t.args.end_line != null) {
        extra = `<span class="agy-step-lines">#L${t.args.start_line}-${t.args.end_line}</span>`;
      }
    } else if (t.name === 'write_file') {
      verb = 'Created';
      iconClass = 'edit';
      iconSymbol = '💾';
      if (typeof t.args.content === 'string') {
        const lines = t.args.content.split('\n').length;
        extra = `<span class="agy-step-lines">(${lines} lines)</span>`;
      }
    } else if (t.name === 'edit_file') {
      verb = 'Edited';
      iconClass = 'edit';
      iconSymbol = '✏️';
    } else if (t.name === 'grep') {
      verb = 'Searched';
      iconClass = 'search';
      iconSymbol = '🔍';
      label = esc(t.args.query || t.args.pattern || 'pattern');
      if (t.result) {
        const matches = (t.result.match(/\\n/g) || []).length + 1;
        extra = `<span class="agy-step-count">${matches} result${matches !== 1 ? 's' : ''}</span>`;
      }
    } else if (t.name === 'list_files') {
      verb = 'Listed';
      iconClass = 'file';
      iconSymbol = '📁';
      label = esc(t.args.path || 'workspace');
    } else if (t.name === 'run_python') {
      verb = 'Executed';
      iconClass = 'python';
      iconSymbol = '⚡';
      label = esc(t.args.file || (t.args.code ? t.args.code.slice(0, 30) + '…' : 'python code'));
    } else if (t.name === 'create_plan') {
      verb = 'Planned';
      iconClass = 'plan';
      iconSymbol = '📋';
      label = `${(t.args.items && t.args.items.length) || '?'} steps`;
    } else if (t.name === 'update_plan_item') {
      verb = 'Plan update';
      iconClass = 'plan';
      iconSymbol = '✔️';
      label = `step ${esc(String(t.args.item != null ? t.args.item : '?'))} → ${esc(String(t.args.status || ''))}`;
    } else if (t.name === 'get_plan') {
      verb = 'Checked plan';
      iconClass = 'plan';
      iconSymbol = '🗒️';
      label = 'plan status';
    } else if (t.name === 'web_search') {
      verb = 'Searched web';
      iconClass = 'search';
      iconSymbol = '🌐';
      label = esc(t.args.query || t.args.q || 'web query');
      if (t.result) {
        const matches = (t.result.match(/https?:\/\//g) || []).length;
        if (matches > 0) extra = `<span class="agy-step-count">${matches} source${matches !== 1 ? 's' : ''}</span>`;
      }
    } else if (t.name === 'web_fetch') {
      verb = 'Fetched page';
      iconClass = 'file';
      iconSymbol = '🔗';
      label = esc(t.args.url || 'web page');
    } else if (t.name === 'spawn_agent') {
      verb = 'Delegated';
      iconClass = 'subagent';
      iconSymbol = '🤖';
      label = esc((t.args.role ? `${t.args.role} sub-agent: ` : 'sub-agent: ') + (t.args.task || '').slice(0, 50));
    }

    const isRunning = t.result === null;

    const isPreviewable = p && /\.(html|htm|csv|xlsx|xls|pdf|md|py|js|ts|json|txt|svg|png|jpg|jpeg|webp)$/i.test(p);
    const previewBtn = isPreviewable
      ? `<button type="button" class="btn ghost" style="padding:1px 7px; font-size:10px; margin-left:auto; border-radius:4px;" onclick="event.stopPropagation(); openFilePreview('${esc(p).replace(/'/g, "\\'")}', '${esc(p).replace(/'/g, "\\'")}')" title="Preview file">👁️ Preview</button>`
      : '';

    h += `<details class="agy-step-detail">
      <summary class="agy-step-row">
        <span class="agy-step-verb">${verb}</span>
        <span class="agy-step-icon ${iconClass}">${iconSymbol}</span>
        <span class="agy-step-file">${label}</span>
        ${extra}
        <span class="agy-step-spacer"></span>
        ${isRunning ? '<span class="agy-summary-pulse active" style="width:6px; height:6px;"></span>' : (t.ok ? '' : '<span style="color:var(--red); font-size:11px;">⚠</span>')}
      </summary>
      <div class="agy-detail-body">
        <div class="agy-detail-bar">
          <span>${esc(t.name)} ${p ? '· ' + esc(p) : ''}</span>
          ${previewBtn}
          ${t.model ? `<span style="font-family:monospace; opacity:0.8; margin-left:8px;">${esc(t.model)}</span>` : ''}
        </div>`;

    if (t.thought) {
      h += `<div style="font-size:11px; color:var(--dim); margin-bottom:6px; font-style:italic;">💭 ${esc(t.thought)}</div>`;
    }

    if (t.name === 'write_file' && typeof t.args.content === 'string') {
      h += `<pre class="agy-detail-code"><code>${esc(t.args.content)}</code></pre>`;
    } else if (t.name === 'edit_file' && (t.args.old_string || t.args.new_string)) {
      h += `<div class="agy-detail-code">
        <div style="color:#fca5a5;">- ${esc(t.args.old_string || '')}</div>
        <div style="color:#86efac;">+ ${esc(t.args.new_string || '')}</div>
      </div>`;
    } else if (t.args && Object.keys(t.args).length > 0) {
      h += `<pre class="agy-detail-code"><code>${esc(JSON.stringify(t.args, null, 2))}</code></pre>`;
    }

    if (t.result !== null) {
      h += `<div style="font-size:10px; font-weight:700; color:var(--dim); margin:6px 0 4px; text-transform:uppercase;">Result</div>
      <pre class="agy-detail-code" style="color:${t.ok ? 'var(--dim)' : 'var(--red)'};"><code>${esc(t.result || '(empty)')}</code></pre>`;
    }

    h += `</div></details>`;
  });

  if (!isAllDone) {
    h += `<div class="agy-working-bar"><span class="agy-summary-pulse active" style="width:6px; height:6px;"></span> Working…</div>`;
  }

  h += `</div></details></div>`;
  return h;
}

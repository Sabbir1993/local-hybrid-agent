/**
 * The agent's work in a reply, drawn the way a coding assistant shows it: what the agent SAID between steps stays as
 * prose, and each run of tool calls between two such sentences folds into one line ("Searched code, read 2 files,
 * ran a command ›"). Opening that line lists the calls as rows; opening a row shows its arguments and result.
 * Chat mode never calls this: tool calls are not shown there (see renderCompleteChatMessage).
 */

export interface TraceItem {
  id: string;
  name: string;
  args: any;
  result: any;
  failed: boolean;
  running: boolean;
  isThought?: boolean;
  image?: string;
  text?: string;
  duration_s?: number;
  subagents?: Record<string, any>;
}

type Segment = { kind: 'prose'; text: string } | { kind: 'group'; items: TraceItem[] };

/** Text from a tool call shown on one line: escaped, flattened, shortened. Arguments can be whole files. */
export function rowText(v: unknown, max = 140): string {
  const flat = (typeof v === 'string' ? v : v == null || typeof v === 'object' ? '' : String(v)).replace(/\s+/g, ' ').trim();
  const cut = flat.length > max ? flat.slice(0, max) + '…' : flat;
  return cut.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
}

function escText(v: unknown): string {
  return String(v ?? '').replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
}

function escAttr(v: unknown): string {
  return escText(v).replace(/"/g, '&quot;').replace(/'/g, '&#39;');
}

const base = (p: unknown) => String(p || '').split(/[\\/]/).pop() || '';
const pathOf = (a: any) => (a && (a.path || a.file || a.filename || a.file_path)) || '';

// tool name -> kind of work, for the one-line summary of a group
const KIND: Record<string, string> = {
  read_file: 'read', read_file_chunk: 'read', doc_inspect: 'read', file_outline: 'read', read_skill: 'read',
  project_overview: 'read', git_inspect: 'read',
  grep: 'search', list_files: 'search', find_symbol: 'search', find_references: 'search',
  search_knowledge_base: 'kb', kb_find_mentions: 'kb', kb_query: 'kb', kb_sql: 'kb', search_memory: 'kb',
  web_search: 'web', web_search_images: 'web', web_fetch: 'fetch',
  write_file: 'create', write_file_common: 'create', project_write: 'create', doc_create: 'create',
  edit_file: 'edit', append_file: 'edit', insert_at_line: 'edit', project_append: 'edit', doc_edit: 'edit',
  doc_append: 'edit', revert: 'edit',
  run_shell: 'run', run_python: 'run', run_tests: 'run', run_sandbox_python: 'run',
  create_plan: 'plan', update_plan_item: 'plan', get_plan: 'plan',
};

function plural(n: number, one: string, many: string): string {
  return n === 1 ? one : `${n} ${many}`;
}

/** "Searched code, read 2 files, ran a command" for one group of calls. */
export function groupSummary(items: TraceItem[]): string {
  const tools = items.filter((i) => !i.isThought);
  if (!tools.length) {
    const secs = items.reduce((s, i) => s + (i.duration_s || 0), 0) || 1;
    return `Thought for ${secs}s`;
  }
  const files: Record<string, Set<string>> = {};
  const counts: Record<string, number> = {};
  const other: string[] = [];
  tools.forEach((t) => {
    const k = KIND[t.name] || '';
    if (!k) {
      const label = (t.name || 'tool').replace(/_/g, ' ');
      if (!other.includes(label)) other.push(label);
      return;
    }
    counts[k] = (counts[k] || 0) + 1;
    (files[k] = files[k] || new Set()).add(pathOf(t.args) || `#${counts[k]}`);
  });
  const nf = (k: string) => (files[k] ? files[k].size : 0);
  const parts: string[] = [];
  if (counts.search) parts.push('searched code');
  if (counts.kb) parts.push('searched the knowledge base');
  if (counts.web) parts.push('searched the web');
  if (counts.read) parts.push(`read ${plural(nf('read'), 'a file', 'files')}`);
  if (counts.fetch) parts.push(`fetched ${plural(counts.fetch, 'a page', 'pages')}`);
  if (counts.create) parts.push(`created ${plural(nf('create'), 'a file', 'files')}`);
  if (counts.edit) parts.push(`edited ${plural(nf('edit'), 'a file', 'files')}`);
  if (counts.run) parts.push(`ran ${plural(counts.run, 'a command', 'commands')}`);
  if (counts.plan) parts.push('updated the plan');
  other.slice(0, 2).forEach((o) => parts.push(`used ${o}`));
  if (other.length > 2) parts.push(`${other.length - 2} more`);
  const s = parts.join(', ');
  return s.charAt(0).toUpperCase() + s.slice(1);
}

function matchCount(result: unknown): string {
  if (typeof result !== 'string' || !result.trim() || /^\(no (matches|files matched)\)/.test(result.trim())) return '';
  const lines = result.split('\n').filter((l) => l.trim() && !l.startsWith('('));
  const filesHit = new Set(lines.map((l) => l.split(':')[0]));
  return `${lines.length} match${lines.length === 1 ? '' : 'es'} in ${filesHit.size} file${filesHit.size === 1 ? '' : 's'}`;
}

function diffStatsHtml(t: TraceItem): string {
  const a = t.args || {};
  let added = 0;
  let removed = 0;

  if (t.name === 'edit_file' || t.name === 'doc_edit') {
    if (typeof a.old_string === 'string' && typeof a.new_string === 'string') {
      removed = a.old_string ? a.old_string.split('\n').length : 0;
      added = a.new_string ? a.new_string.split('\n').length : 0;
    } else if (typeof a.diff === 'string' || typeof a.patch === 'string') {
      const diffText = String(a.diff || a.patch || '');
      const lines = diffText.split('\n');
      for (const l of lines) {
        if (l.startsWith('+') && !l.startsWith('+++')) added++;
        else if (l.startsWith('-') && !l.startsWith('---')) removed++;
      }
    }
  } else if (t.name === 'write_file' || t.name === 'write_file_common' || t.name === 'project_write' || t.name === 'doc_create') {
    const c = typeof a.content === 'string' ? a.content : '';
    if (c) added = c.split('\n').length;
  } else if (t.name === 'append_file' || t.name === 'project_append' || t.name === 'doc_append') {
    const c = typeof a.content === 'string' ? a.content : '';
    if (c) added = c.split('\n').length;
  } else if (t.name === 'insert_at_line') {
    const txt = typeof a.text === 'string' ? a.text : '';
    if (txt) added = txt.split('\n').length;
  }

  if (added === 0 && removed === 0 && typeof t.result === 'string') {
    const m = t.result.match(/\+(\d+)\s*-\s*(\d+)/) ||
              t.result.match(/(\d+)\s*insertion[s]?.*?(?:,\s*(\d+)\s*deletion[s]?)?/i);
    if (m) {
      added = parseInt(m[1] || '0', 10);
      removed = parseInt(m[2] || '0', 10);
    }
  }

  if (added === 0 && removed === 0) return '';

  let h = '<span class="ct-diff-stat">';
  if (added > 0) h += `<span class="ct-diff-add">+${added}</span>`;
  if (removed > 0) h += `<span class="ct-diff-del">-${removed}</span>`;
  h += '</span>';
  return h;
}

/** One row: verb + target (+ a muted detail). Everything escaped. */
function rowHtml(t: TraceItem): string {
  const a = t.args || {};
  const p = pathOf(a);
  const code = (v: unknown, max = 90) => `<code class="ct-mono">${rowText(v, max)}</code>`;
  const strong = (v: unknown, max = 90) => `<span class="ct-target">${rowText(v, max)}</span>`;
  let verb = '';
  let target = '';
  let note = '';
  switch (t.name) {
    case 'read_file': case 'read_file_chunk': case 'doc_inspect': case 'file_outline':
      verb = 'Read'; target = strong(base(p) || p); break;
    case 'grep':
      verb = 'Searched'; target = code(a.pattern || a.query); note = matchCount(t.result); break;
    case 'list_files':
      verb = 'Listed'; target = code(a.pattern || a.path || '.'); break;
    case 'find_symbol': case 'find_references':
      verb = t.name === 'find_symbol' ? 'Found' : 'Found references to'; target = code(a.name || a.symbol); break;
    case 'write_file': case 'write_file_common': case 'project_write': case 'doc_create':
      verb = 'Created'; target = strong(base(p) || p); break;
    case 'edit_file': case 'doc_edit': case 'insert_at_line':
      verb = 'Edited'; target = strong(base(p) || p); break;
    case 'append_file': case 'project_append': case 'doc_append':
      verb = 'Appended to'; target = strong(base(p) || p); break;
    case 'run_shell': case 'run_tests':
      verb = 'Ran'; target = code(a.description || a.command || a.cmd || t.name, 110); break;
    case 'run_python': case 'run_sandbox_python':
      verb = 'Ran Python'; target = p ? strong(base(p)) : ''; break;
    case 'web_search': case 'web_search_images':
      verb = 'Searched the web for'; target = strong(a.query); break;
    case 'web_fetch':
      verb = 'Fetched'; target = strong(a.title || a.url); break;
    case 'search_knowledge_base': case 'kb_query': case 'kb_sql': case 'kb_find_mentions':
      verb = 'Searched the knowledge base'; target = a.query ? strong(a.query) : ''; break;
    case 'browser_screenshot': case 'mobile_screenshot':
      verb = 'Captured'; target = strong('Screenshot'); note = '📷'; break;
    default: {
      verb = (t.name || 'tool').replace(/_/g, ' ');
      verb = verb.charAt(0).toUpperCase() + verb.slice(1);
      const key = ['path', 'query', 'url', 'command', 'name', 'project', 'pattern', 'text'].find((k) => typeof a[k] === 'string' && a[k]);
      target = key ? strong(key === 'path' ? base(a[key]) : a[key]) : '';
    }
  }
  let h = `<span class="ct-verb">${escText(verb)}</span>`;
  if (target) h += ` ${target}`;
  const diffBadge = diffStatsHtml(t);
  if (diffBadge) h += ` ${diffBadge}`;
  if (note) h += `<span class="ct-note"> · ${escText(note)}</span>`;
  return h;
}

export function parseSubagentsFromText(rawText: unknown): any[] {
  if (!rawText || typeof rawText !== 'string') return [];
  const textWithoutHeader = rawText.replace(/^\[parallel sub-agents[^\n]*\n+/i, '').trim();
  const blocks = textWithoutHeader.split(/(?:^|\n)(?==== Sub-agent #\d+)/g).filter((b) => b.trim());
  const list: any[] = [];

  if (blocks.length > 0) {
    blocks.forEach((blk, idx) => {
      const headMatch = blk.match(/=== Sub-agent #(\d+)(?:\s*\((.*?)\))?\s*===/);
      const subIdx = headMatch ? parseInt(headMatch[1], 10) : idx + 1;
      let role = headMatch && headMatch[2] ? headMatch[2].replace(/^role=/, '').trim() : 'generic';
      let status = 'success';
      let lane = 'default';
      let steps = 0;
      let failure_reason: string | null = null;

      const envMatch = blk.match(/\[(?:sub-agent|Sub-agent|critic-actor)\s*·\s*([^\]]+)\]/);
      if (envMatch) {
        const parts = envMatch[1].split('·').map((s) => s.trim());
        parts.forEach((p) => {
          if (p.startsWith('role=')) role = p.replace('role=', '');
          else if (p.startsWith('lane=')) lane = p.replace('lane=', '');
          else if (p.startsWith('status=')) status = p.replace('status=', '');
          else if (p.includes('msgs')) steps = parseInt(p, 10) || 0;
        });
      } else if (blk.includes('error:')) {
        status = 'error';
      }

      if (status === 'step_limit' || status === 'step_exhausted') {
        failure_reason = 'Reached maximum step limit without producing a final answer. Consider increasing max_steps.';
      } else if (status === 'no_response') {
        failure_reason = 'Sub-agent received no response from the model.';
      } else if (status !== 'success') {
        const errLine = blk.split('\n').find((l) => l.trim().startsWith('error:'));
        failure_reason = errLine ? errLine.trim().replace(/^error:\s*/, '') : 'Sub-agent execution failed.';
      }

      const body = blk
        .replace(/=== Sub-agent #\d+(?:\s*\(.*?\))?\s*===\n?/, '')
        .replace(/\[(?:sub-agent|Sub-agent|critic-actor)\s*·\s*[^\]]+\]\n?/, '')
        .trim();

      list.push({
        id: `sub_${subIdx}`,
        index: subIdx,
        role,
        status,
        lane,
        stepsCount: steps,
        failure_reason,
        final_content: body,
        steps: [],
      });
    });
  } else if (rawText.includes('[sub-agent') || rawText.includes('[critic-actor')) {
    // Single subagent format
    const envMatch = rawText.match(/\[(?:sub-agent|Sub-agent|critic-actor)\s*·\s*([^\]]+)\]/);
    let role = 'generic';
    let status = 'success';
    let lane = 'default';
    let steps = 0;
    let failure_reason: string | null = null;
    if (envMatch) {
      const parts = envMatch[1].split('·').map((s) => s.trim());
      parts.forEach((p) => {
        if (p.startsWith('role=')) role = p.replace('role=', '');
        else if (p.startsWith('lane=')) lane = p.replace('lane=', '');
        else if (p.startsWith('status=')) status = p.replace('status=', '');
        else if (p.includes('msgs')) steps = parseInt(p, 10) || 0;
      });
    }
    if (status === 'step_limit' || status === 'step_exhausted') {
      failure_reason = 'Reached step limit without completing the task.';
    } else if (status !== 'success') {
      failure_reason = 'Sub-agent execution failed.';
    }
    const body = rawText.replace(/\[(?:sub-agent|Sub-agent|critic-actor)\s*·\s*[^\]]+\]\n?/, '').trim();
    list.push({
      id: 'sub_1',
      index: 1,
      role,
      status,
      lane,
      stepsCount: steps,
      failure_reason,
      final_content: body,
      steps: [],
    });
  }

  return list;
}

function renderSubagentViewHtml(t: TraceItem): string {
  let subagents = parseSubagentsFromText(t.result);
  const isRunning = t.running || !t.result;
  const liveMap = t.subagents || {};

  // If subagents could not be parsed from result text (e.g. while actively running or unstructured result), synthesize from live events or arguments
  if (!subagents || !subagents.length) {
    if (Array.isArray(t.args?.agents) && t.args.agents.length > 0) {
      subagents = t.args.agents.map((ag: any, idx: number) => {
        const key = String(idx);
        const live = liveMap[key] || Object.values(liveMap).find((v: any) => v.sub_index === idx || v.role === ag.role) || {};
        const isDone = live.status === 'completed';
        const isFailed = live.status === 'error';
        return {
          index: idx + 1,
          role: live.role || ag.role || 'subagent',
          status: isDone ? 'success' : isFailed ? 'failed' : (isRunning ? 'running' : 'success'),
          lane: ag.lane || 'default',
          stepsCount: live.step || 0,
          max_steps: live.max_steps || ag.max_steps || 10,
          current_tool: live.current_tool,
          current_tool_args: live.current_tool_args,
          elapsed_s: live.elapsed_s,
          duration_s: live.duration_s,
          task: ag.task || '',
          steps: [],
          final_content: isDone ? 'Subagent completed execution.' : '',
          failure_reason: live.error || '',
        };
      });
    } else if (t.args?.task) {
      const key = '0';
      const live = liveMap[key] || Object.values(liveMap)[0] || {};
      const isDone = live.status === 'completed';
      const isFailed = live.status === 'error';
      subagents = [{
        index: 1,
        role: live.role || t.args.role || 'subagent',
        status: isDone ? 'success' : isFailed ? 'failed' : (isRunning ? 'running' : 'success'),
        lane: t.args.lane || 'default',
        stepsCount: live.step || 0,
        max_steps: live.max_steps || t.args.max_steps || 10,
        current_tool: live.current_tool,
        current_tool_args: live.current_tool_args,
        elapsed_s: live.elapsed_s,
        duration_s: live.duration_s,
        task: t.args.task || '',
        steps: [],
        final_content: isDone ? 'Subagent completed execution.' : '',
        failure_reason: live.error || '',
      }];
    } else if (Object.keys(liveMap).length > 0) {
      subagents = Object.values(liveMap).map((live: any, idx: number) => {
        const isDone = live.status === 'completed';
        const isFailed = live.status === 'error';
        return {
          index: (live.sub_index !== undefined ? live.sub_index + 1 : idx + 1),
          role: live.role || 'subagent',
          status: isDone ? 'success' : isFailed ? 'failed' : (isRunning ? 'running' : 'success'),
          stepsCount: live.step || 0,
          max_steps: live.max_steps || 10,
          current_tool: live.current_tool,
          current_tool_args: live.current_tool_args,
          elapsed_s: live.elapsed_s,
          duration_s: live.duration_s,
          task: live.task || '',
          steps: [],
          final_content: isDone ? 'Subagent completed execution.' : '',
          failure_reason: live.error || '',
        };
      });
    }
  }

  if (!subagents || !subagents.length) return '';

  const total = subagents.length;
  const runningCount = subagents.filter((s: any) => s.status === 'running').length;
  const succeeded = subagents.filter((s: any) => s.status === 'success' || s.status === 'completed').length;
  const failed = subagents.filter((s: any) => s.status === 'failed' || s.status === 'error').length;
  const anyFailed = failed > 0;
  const agentSpecs = Array.isArray(t.args?.agents) ? t.args.agents : null;

  let h = '<div class="ct-subagent-dashboard">';

  // 1. Dashboard Summary Header
  h += '<div class="ct-subagent-summary-bar">';
  h += `<div class="ct-subagent-title">👥 Sub-Agents (${total} dispatched)</div>`;
  h += '<div class="ct-subagent-pills">';
  if (runningCount > 0) {
    h += `<span class="ct-pill-running"><span class="ct-running"></span> ${runningCount} Running</span>`;
  }
  if (succeeded > 0) {
    h += `<span class="ct-pill-ok">✓ ${succeeded} Succeeded</span>`;
  }
  if (failed > 0) {
    h += `<span class="ct-pill-fail">✕ ${failed} Failed</span>`;
  }
  h += `<button type="button" class="ct-dock-popout-btn" onclick="window.__openSubagentsDock &amp;&amp; window.__openSubagentsDock()" title="Open in side panel">⤢ Open in Dock</button>`;
  h += '</div></div>';

  // 2. Failure Alert Banner (if any subagent failed)
  if (anyFailed) {
    h += '<div class="ct-subagent-alert-box">';
    subagents.forEach((s: any, i: number) => {
      if (s.status === 'failed' || s.status === 'error') {
        const spec = agentSpecs && agentSpecs[i] ? agentSpecs[i] : null;
        const role = (s.role && s.role !== 'generic') ? s.role : (spec?.role || s.role || 'generic');
        const name = `Sub-agent #${s.index || i + 1} (${role})`;
        const reason = s.failure_reason || s.final_content || 'Failed to complete execution.';
        h += `<div class="ct-alert-row"><span class="ct-alert-badge">✕ Failed</span> <strong>${escText(name)}:</strong> ${escText(reason)}</div>`;
      }
    });
    h += '</div>';
  }

  // 3. Subagent Process Cards
  h += '<div class="ct-subagent-cards">';
  subagents.forEach((s: any, i: number) => {
    const isRunningSub = s.status === 'running';
    const isSuccess = s.status === 'success' || s.status === 'completed';
    const isFail = s.status === 'failed' || s.status === 'error';
    const spec = agentSpecs && agentSpecs[i] ? agentSpecs[i] : null;
    const taskText = s.task || (spec?.task) || (t.args?.task) || '';
    const stepsArr = Array.isArray(s.steps) ? s.steps : [];
    const stepsCount = stepsArr.length || s.stepsCount || 0;
    const roleName = (s.role && s.role !== 'generic') ? s.role : (spec?.role || s.role || 'generic');
    const subIdx = s.index || i + 1;

    // During live execution, keep the cards open so user sees task & live tool progress!
    const cardOpenAttr = isRunningSub ? ' open' : '';
    const cardClass = isFail ? ' failed' : isRunningSub ? ' running' : '';

    h += `<details class="ct-subagent-card${cardClass}"${cardOpenAttr}>`;
    h += '<summary class="ct-subagent-card-sum">';
    if (isRunningSub) {
      h += '<span class="ct-subagent-status-icon running"><span class="ct-running" style="width:10px;height:10px;"></span></span>';
    } else {
      h += `<span class="ct-subagent-status-icon ${isSuccess ? 'ok' : 'fail'}">${isSuccess ? '✓' : '✕'}</span>`;
    }
    h += `<span class="ct-subagent-card-name">Sub-agent #${subIdx} · <span class="ct-role-highlight">${escText(roleName)}</span></span>`;
    h += '<div class="ct-subagent-meta-tags">';
    if (isRunningSub) {
      h += `<span class="ct-meta-tag">Step ${stepsCount}/${s.max_steps || '?'}</span>`;
      if (s.elapsed_s) h += `<span class="ct-meta-tag">${s.elapsed_s}s</span>`;
      h += `<span class="ct-badge-status running">RUNNING</span>`;
    } else {
      h += `<span class="ct-meta-tag">${stepsCount} msgs</span>`;
      if (s.lane && s.lane !== 'default') h += `<span class="ct-meta-tag">${escText(s.lane)}</span>`;
      if (s.duration_s) h += `<span class="ct-meta-tag">${s.duration_s}s</span>`;
      h += `<span class="ct-badge-status ${isSuccess ? 'ok' : 'fail'}">${isSuccess ? 'success' : escText(s.status || 'failed')}</span>`;
    }
    h += '</div>';
    h += '<span class="ct-chev">›</span>';
    h += '</summary>';

    h += '<div class="ct-subagent-card-body">';

    // Assigned Task
    if (taskText) {
      h += '<div class="ct-sub-section"><span class="ct-sub-section-title">Assigned Task</span>';
      h += `<div class="ct-sub-task-box">${escText(taskText)}</div></div>`;
    }

    // Active Tool / Running Status
    if (isRunningSub) {
      h += '<div class="ct-sub-section"><span class="ct-sub-section-title">Active Execution</span>';
      if (s.current_tool) {
        const argSnippet = s.current_tool_args ? (s.current_tool_args.path || s.current_tool_args.file || s.current_tool_args.query || s.current_tool_args.command || '') : '';
        h += `<div class="ct-sub-step-row" style="background:rgba(59,130,246,0.08);border-color:rgba(59,130,246,0.25);">`;
        h += `<div class="ct-sub-step-header"><span class="ct-sub-step-badge" style="color:#60a5fa;">Step ${stepsCount || 1}: Executing Tool</span>`;
        h += `<span class="ct-step-duration"><span class="ct-running" style="width:8px;height:8px;margin-right:4px;"></span>In progress</span></div>`;
        h += `<div style="font-size:12px;font-family:ui-monospace,monospace;color:#93c5fd;margin-top:2px;"><strong>${escText(s.current_tool)}</strong> ${escText(String(argSnippet).slice(0, 80))}</div>`;
        h += '</div>';
      } else {
        h += `<div class="ct-sub-step-thought" style="color:#94a3b8;">Subagent is synthesizing plan and deciding next action...</div>`;
      }
      h += '</div>';
    }

    // Failure Diagnosis
    if (isFail && s.failure_reason) {
      h += `<div class="ct-sub-fail-reason"><strong>Diagnosis:</strong> ${escText(s.failure_reason)}</div>`;
    }

    // Steps Timeline
    if (stepsArr.length > 0) {
      h += `<div class="ct-sub-section"><span class="ct-sub-section-title">Execution Process (${stepsArr.length} steps)</span>`;
      h += '<div class="ct-sub-steps-list">';
      stepsArr.forEach((st: any) => {
        h += '<div class="ct-sub-step-row">';
        h += `<div class="ct-sub-step-header"><span class="ct-sub-step-badge">Step ${st.step}</span>`;
        if (st.duration_s) h += `<span class="ct-step-duration">${st.duration_s}s</span>`;
        h += '</div>';
        if (st.thought && st.thought.trim()) {
          h += `<div class="ct-sub-step-thought">${escText(st.thought.trim())}</div>`;
        }
        if (Array.isArray(st.tools) && st.tools.length > 0) {
          st.tools.forEach((tc: any) => {
            h += `<details class="ct-sub-tool-drawer"><summary class="ct-sub-tool-sum">`;
            h += `<span class="ct-sub-tool-badge">${escText(tc.name)}</span>`;
            const toolArg = tc.args?.path || tc.args?.file || tc.args?.query || tc.args?.pattern || tc.args?.command || '';
            if (toolArg) {
              h += `<span class="ct-sub-tool-target">${escText(String(toolArg).slice(0, 70))}</span>`;
            }
            h += `<span class="${tc.ok ? 'ct-badge-ok-mini' : 'ct-badge-fail-mini'}">${tc.ok ? 'ok' : 'fail'}</span>`;
            h += '<span class="ct-chev">›</span>';
            h += '</summary>';
            h += '<div class="ct-sub-tool-content">';
            if (tc.args && Object.keys(tc.args).length) {
              h += `<div class="ct-label">Arguments</div><pre class="ct-pre"><code>${escText(JSON.stringify(tc.args, null, 2))}</code></pre>`;
            }
            if (tc.result) {
              h += `<div class="ct-label">Result</div><pre class="ct-pre${!tc.ok ? ' ct-pre-fail' : ''}"><code>${escText(tc.result)}</code></pre>`;
            }
            h += '</div></details>';
          });
        }
        h += '</div>';
      });
      h += '</div></div>';
    }

    // Final Output
    const outputContent = s.final_content || '';
    if (outputContent && !isRunningSub) {
      h += '<div class="ct-sub-section"><span class="ct-sub-section-title">Final Output</span>';
      h += `<pre class="ct-sub-output"><code>${escText(outputContent)}</code></pre></div>`;
    }

    h += '</div>';
    h += '</details>';
  });
  h += '</div>';

  // 4. Raw Output Toggle (only when completed)
  if (!isRunning && t.result) {
    const rawText = typeof t.result === 'string' ? t.result : JSON.stringify(t.result, null, 2);
    h += '<details class="ct-raw-toggle"><summary>View Raw Output Text</summary>';
    h += `<pre class="ct-pre${t.failed ? ' ct-pre-fail' : ''}"><code>${escText(rawText)}</code></pre></details>`;
  }

  h += '</div>';
  return h;
}

function drawerHtml(t: TraceItem): string {
  let h = '<div class="ct-drawer">';
  const p = pathOf(t.args);
  if (p) {
    // no Preview button in a step: the finished file has its own card in the answer. A zip keeps its Download link
    const pa = escAttr(p);
    const dl = /\.zip$/i.test(p)
      ? `<a class="file-action-badge" href="/agent/download?path=${encodeURIComponent(p)}" download="${pa}">⬇ Download</a>` : '';
    h += `<div class="ct-drawer-file"><span class="ct-mono">${pa}</span>${dl}</div>`;
  }

  // Render screenshot if captured (browser_screenshot or mobile_screenshot)
  const screenImg = t.image || (typeof t.result === 'string' ? t.result.match(/saved:\s*([^\s()]+\.png)/i)?.[1] : null);
  if (t.image) {
    h += `<div class="ct-screenshot-preview" style="margin: 8px 0 12px 0;">
      <div class="ct-label" style="display:flex; align-items:center; gap:6px;">
        <span>📷 Captured Screenshot</span>
        <span style="font-size:10px; text-transform:none; opacity:0.7;">(click to view in dock)</span>
      </div>
      <div style="cursor: pointer; display: inline-block; max-width: 100%; border-radius: 8px; overflow: hidden; border: 1px solid var(--border);"
           data-preview-path="${escAttr(t.image)}" data-preview-title="Browser Screenshot">
        <img src="${escAttr(t.image)}" alt="Captured Screenshot" style="max-width: 100%; max-height: 380px; display: block; object-fit: contain; background: #0a0a0c;" />
      </div>
    </div>`;
  } else if (screenImg) {
    const rawUrl = `/agent/ws/raw?path=${encodeURIComponent(screenImg)}`;
    h += `<div class="ct-screenshot-preview" style="margin: 8px 0 12px 0;">
      <div class="ct-label" style="display:flex; align-items:center; gap:6px;">
        <span>📷 Captured Screenshot</span>
        <span style="font-size:10px; text-transform:none; opacity:0.7;">(click to view in dock)</span>
      </div>
      <div style="cursor: pointer; display: inline-block; max-width: 100%; border-radius: 8px; overflow: hidden; border: 1px solid var(--border);"
           data-preview-path="${escAttr(screenImg)}" data-preview-source="ws" data-preview-title="Browser Screenshot">
        <img src="${escAttr(rawUrl)}" alt="Captured Screenshot" style="max-width: 100%; max-height: 380px; display: block; object-fit: contain; background: #0a0a0c;" />
      </div>
    </div>`;
  }

  // If this is a subagent tool call, render the rich Subagent Dashboard!
  if (t.name === 'spawn_parallel_agents' || t.name === 'spawn_agent' || t.name === 'spawn_reviewed_coder') {
    const subHtml = renderSubagentViewHtml(t);
    if (subHtml) {
      h += subHtml;
      h += '</div>';
      return h;
    }
  }

  if (t.args && Object.keys(t.args).length) {
    h += `<div class="ct-label">Arguments</div><pre class="ct-pre"><code>${escText(JSON.stringify(t.args, null, 2))}</code></pre>`;
  }
  const raw = typeof t.result === 'string' ? t.result : t.result == null ? '' : JSON.stringify(t.result);
  if (raw && raw !== '""') {
    h += `<div class="ct-label">Result</div><pre class="ct-pre${t.failed ? ' ct-pre-fail' : ''}"><code>${escText(raw)}</code></pre>`;
  }
  return h + '</div>';
}

function thoughtRowHtml(t: TraceItem): string {
  return `<details class="ct-row ct-row-thought"><summary><span class="ct-verb">Thought for ${t.duration_s || 1}s</span>`
    + `<span class="ct-chev">›</span></summary><div class="ct-drawer"><div class="ct-thought">${escText(t.text || '')}</div></div></details>`;
}

function groupHtml(items: TraceItem[], open = false): string {
  const tools = items.filter((i) => !i.isThought);
  const failed = tools.filter((t) => t.failed).length;
  const running = tools.some((t) => t.running);
  let h = `<details class="ct-group"${open ? ' open' : ''}><summary class="ct-group-sum">`;
  h += `<span class="ct-group-text">${escText(groupSummary(items))}</span>`;
  if (failed) h += `<span class="ct-badge-fail">${failed} failed</span>`;
  if (running) h += '<span class="ct-running" aria-label="running"></span>';
  h += '<span class="ct-chev">›</span></summary><div class="ct-list">';
  items.forEach((t) => {
    if (t.isThought) {
      h += thoughtRowHtml(t);
      return;
    }
    h += `<details class="ct-row${t.failed ? ' ct-row-fail' : ''}"><summary>${rowHtml(t)}`;
    if (t.failed) h += '<span class="ct-badge-fail">Failed</span>';
    else if (t.running) h += '<span class="ct-running" aria-label="running"></span>';
    h += `<span class="ct-chev">›</span></summary>${drawerHtml(t)}</details>`;
  });
  return h + '</div></details>';
}

/** Acts (in arrival order) -> prose / group segments. */
export function traceSegments(acts: any[]): Segment[] {
  const segs: Segment[] = [];
  const byId = new Map<string, TraceItem>();
  const group = (): TraceItem[] => {
    const last = segs[segs.length - 1];
    if (last && last.kind === 'group') return last.items;
    const g: Segment = { kind: 'group', items: [] };
    segs.push(g);
    return g.items;
  };
  acts.forEach((a, idx) => {
    if (a.type === 'narration') {
      const text = String(a.text || '').trim();
      if (text) segs.push({ kind: 'prose', text });
    } else if (a.type === 'thought' || a.type === 'reasoning') {
      const text = a.text || a.content || a.thought || a.reasoning || '';
      if (!String(text).trim()) return;
      group().push({ id: a.id || `th-${idx}`, name: 'thought', args: {}, result: null, failed: false, running: false,
        isThought: true, text, duration_s: a.duration_s || a.secs || 1 });
    } else if (a.type === 'tool_call') {
      const it: TraceItem = { id: a.id || `tc-${idx}`, name: a.name || 'tool', args: a.args || {}, result: null,
        failed: false, running: true, subagents: a.subagents };
      group().push(it);
      if (a.id) byId.set(a.id, it);
    } else if (a.type === 'tool_result') {
      const it = byId.get(a.id);
      if (!it) return;
      it.result = a.result;
      it.running = false;
      if (a.subagents) it.subagents = a.subagents;
      if (a.image) it.image = a.image;
      // the server's verdict decides; only older stored steps without `ok` fall back to an "error..." result
      it.failed = a.ok === false || (a.ok === undefined && typeof a.result === 'string' && /^\s*error\b/i.test(a.result));
    }
  });
  return segs;
}

/**
 * HTML for the whole trace. `prose` turns a narration into sanitized markdown (passed in so this file stays free of
 * the markdown setup). `live`: the reply is still streaming, so the newest group stays open.
 */
export function renderAgentTraceHtml(acts: any[], prose: (md: string) => string, live = false, workedFor = '', _collapsed = true): string {
  const segs = traceSegments(acts || []);
  if (!segs.length) return '';
  let h = '<div class="ct-trace">';
  if (workedFor && !live) h += `<div class="ct-worked">${escText(workedFor)}</div>`;
  segs.forEach((s) => {
    if (s.kind === 'prose') {
      h += `<div class="ct-prose">${prose(s.text)}</div>`;
    } else if (s.items.length) {
      // Tool groups default collapsed by default, including during live streaming response
      h += groupHtml(s.items, false);
    }
  });
  return h + '</div>';
}

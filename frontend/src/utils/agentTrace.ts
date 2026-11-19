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
  text?: string;
  duration_s?: number;
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
    default: {
      verb = (t.name || 'tool').replace(/_/g, ' ');
      verb = verb.charAt(0).toUpperCase() + verb.slice(1);
      const key = ['path', 'query', 'url', 'command', 'name', 'project', 'pattern', 'text'].find((k) => typeof a[k] === 'string' && a[k]);
      target = key ? strong(key === 'path' ? base(a[key]) : a[key]) : '';
    }
  }
  let h = `<span class="ct-verb">${escText(verb)}</span>`;
  if (target) h += ` ${target}`;
  if (note) h += `<span class="ct-note"> · ${escText(note)}</span>`;
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

function groupHtml(items: TraceItem[], open: boolean): string {
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
        failed: false, running: true };
      group().push(it);
      if (a.id) byId.set(a.id, it);
    } else if (a.type === 'tool_result') {
      const it = byId.get(a.id);
      if (!it) return;
      it.result = a.result;
      it.running = false;
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
export function renderAgentTraceHtml(acts: any[], prose: (md: string) => string, live = false, workedFor = ''): string {
  const segs = traceSegments(acts || []);
  if (!segs.length) return '';
  const lastGroup = segs.map((s) => s.kind).lastIndexOf('group');
  let h = '<div class="ct-trace">';
  if (workedFor && !live) h += `<div class="ct-worked">${escText(workedFor)}</div>`;
  segs.forEach((s, i) => {
    if (s.kind === 'prose') h += `<div class="ct-prose">${prose(s.text)}</div>`;
    else if (s.items.length) h += groupHtml(s.items, live && i === lastGroup);
  });
  return h + '</div>';
}

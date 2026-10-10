// Pure logic for the A770 VS Code extension: no `vscode` import, so it is unit-tested with plain node
// (tests/js/test_vscode_lib.js). extension.js is the thin layer that wires it to the editor.
'use strict';

const MAX_SELECTION_CHARS = 20000;
const ANSI_RE = /\x1b(?:\[[0-?]*[ -/]*[@-~]|\][^\x07\x1b]*(?:\x07|\x1b\\)?|[@-Z\\-_])/g;
const CTRL_RE = /[\x00-\x08\x0b-\x1f\x7f-\x9f]/g;

// Server/model text is untrusted: strip escape sequences and control characters before it is shown.
function clean(text) {
  return String(text == null ? '' : text).replace(ANSI_RE, '').replace(/\r/g, '\n').replace(CTRL_RE, '');
}

function oneLine(text, limit = 300) {
  const s = clean(text).split(/\s+/).filter(Boolean).join(' ');
  return s.length <= limit ? s : s.slice(0, limit - 1) + '…';
}

// The task text: the question plus where it comes from. The agent reads files itself through the Companion;
// the selection is only what the user is pointing at.
function buildPrompt({ question, file, languageId, selection, startLine, endLine }) {
  const q = String(question || '').trim();
  if (!q) throw new Error('a question is required');
  let out = q;
  if (file) {
    const where = startLine ? ` lines ${startLine}-${endLine || startLine}` : '';
    out += `\n\nContext: the user has \`${file}\`${where} open in the editor.`;
  }
  if (selection && selection.trim()) {
    const cut = selection.length > MAX_SELECTION_CHARS;
    const body = cut ? selection.slice(0, MAX_SELECTION_CHARS) : selection;
    const fence = body.includes('```') ? '~~~~' : '```';
    out += `\n\nSelected code${languageId ? ` (${languageId})` : ''}${cut ? ' (truncated)' : ''}:\n${fence}\n${body}\n${fence}`;
  }
  return out;
}

// Incremental SSE parser for a chunked stream: push(text) returns the events completed so far.
class SseParser {
  constructor() { this.buf = ''; this.seq = null; this.event = null; }
  push(chunk) {
    this.buf += chunk;
    const out = [];
    let nl;
    while ((nl = this.buf.indexOf('\n')) >= 0) {
      const line = this.buf.slice(0, nl).replace(/\r$/, '');
      this.buf = this.buf.slice(nl + 1);
      if (line.startsWith('id: ')) {
        this.seq = /^\d+$/.test(line.slice(4)) ? Number(line.slice(4)) : null;
      } else if (line.startsWith('event: ')) {
        this.event = line.slice(7).trim();
      } else if (line.startsWith('data: ') && this.event) {
        try {
          const data = JSON.parse(line.slice(6));
          out.push({ seq: this.seq, event: this.event, data: data && typeof data === 'object' ? data : {} });
        } catch (e) { /* malformed frame: skipped */ }
        this.event = null; this.seq = null;
      }
    }
    return out;
  }
}

// What to show for one event: { text } for the output channel, plus { card } for an approval question.
class Renderer {
  constructor() { this.textOpen = false; this.finalState = null; }
  feed(event, data) {
    const out = [];
    if (event === 'delta') {
      const c = clean(data.text);
      if (c) { this.textOpen = !c.endsWith('\n'); out.push(c); }
      return out;
    }
    const closeLine = () => { if (this.textOpen) { out.push('\n'); this.textOpen = false; } };
    if (event === 'tool_call') {
      closeLine();
      out.push(`> ${clean(data.name)} ${describeArgs(data.args)}`.trimEnd() + '\n');
    } else if (event === 'tool_result') {
      closeLine();
      out.push(`  ${data.ok ? 'ok ' : 'ERR'} ${oneLine(data.result, 160)}\n`);
    } else if (event === 'done') {
      closeLine();
      this.finalState = clean(data.state || 'completed');
      const why = oneLine(data.reason || data.note || '', 160);
      out.push(`[${this.finalState}]${why ? ' ' + why : ''}\n`);
    } else if (event === 'error') {
      closeLine();
      out.push('error: ' + oneLine(data.message || data.error || JSON.stringify(data), 200) + '\n');
    }
    return out;
  }
}

function describeArgs(args) {
  if (!args || typeof args !== 'object') return '';
  return Object.entries(args).slice(0, 4).map(([k, v]) => {
    if (typeof v === 'string') return v.length <= 60 ? `${k}=${JSON.stringify(oneLine(v, 60))}` : `${k}=<${v.length} chars>`;
    if (typeof v === 'number' || typeof v === 'boolean' || v == null) return `${k}=${v}`;
    return `${k}=<${Array.isArray(v) ? 'list' : 'object'}>`;
  }).join(' ');
}

const CARD_TITLES = { rule: 'Policy check', mcp: 'Connector action', python: 'Run Python code', edit: 'Edit a file',
  media: 'Cloud media (may cost money)', browser_eval: 'Run JavaScript in the agent browser',
  browser_open: 'Open a website', device: 'Device action' };
const RUN_ALLOWED_KINDS = new Set(['rule', 'edit', 'python', 'browser_eval', 'browser_open', 'device']);

function cardMessage(data) {
  const title = CARD_TITLES[data.kind] || 'Run a command';
  const warn = data.tainted ? '\n\nThis run has read outside content (a web page or connector). Approve only if YOU asked for this.' : '';
  return `${title}\n\n${clean(data.cmd).split('\n').slice(0, 14).map((l) => oneLine(l, 200)).join('\n')}${warn}`;
}

// Buttons offered for a card, and the server decision each one means. Saved "always allow" is never offered.
function cardChoices(kind) {
  const choices = [{ label: 'Allow once', decision: 'allow' }];
  if (RUN_ALLOWED_KINDS.has(kind)) choices.push({ label: 'Allow for this run', decision: 'project' });
  return choices;
}

function normalizeBase(raw) {
  const s = String(raw || '').trim().replace(/\/+$/, '');
  const m = /^(https?):\/\/(\[[^\]]+\]|[^\s/:]+)(?::\d+)?(?:\/|$)/.exec(s);
  if (!m) throw new Error('baseUrl must start with http:// or https://');
  // the API token travels in every request: plain http is only acceptable to this machine
  if (m[1] === 'http' && !/^(localhost|127\.0\.0\.1|\[::1\])$/i.test(m[2])) {
    throw new Error('baseUrl must use https unless it points at this machine');
  }
  return s;
}

module.exports = { clean, oneLine, buildPrompt, SseParser, Renderer, describeArgs, cardMessage, cardChoices,
  normalizeBase, MAX_SELECTION_CHARS };

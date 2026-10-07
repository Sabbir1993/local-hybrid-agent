// node tests/js/test_agent_cards.js - D5 card renderers.
// Tool arguments collapse by default (click-to-expand, no inline onclick);
// card chrome carries classes, not inline styles; escaped args never reach the
// DOM unsanitised; diff lines use agy-diff-line classes.
const assert = require('assert');
const fs = require('fs');
const path = require('path');
const vm = require('vm');

const src = fs.readFileSync(path.join(__dirname, '..', '..', 'static', 'js', 'agent-acts.js'), 'utf8');
const esc = s => String(s).replace(/[&<>"]/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c]));
const ctx = { window: {}, document: { addEventListener() {}, querySelectorAll: () => [] }, esc, md: esc, console,
  $: () => null, toast() {}, navigator: {} };
vm.createContext(ctx);
try { vm.runInContext(src, ctx); } catch (e) { console.error('load failed:', e.message); process.exit(1); }

// --- tool args collapse by default, expand on demand ---------------------
const tool = [
  { type: 'step', step: 1 },
  { type: 'tool_call', id: 'a', name: 'read_file', args: { path: 'src/app.js', offset: 1, limit: 50 } },
  { type: 'tool_result', id: 'a', name: 'read_file', ok: true, result: '// line 1\n// line 2\n' },
];
const html = ctx.agentActsHtml(JSON.parse(JSON.stringify(tool)), true);

assert.ok(/agy-expand-btn/.test(html) && /Show arguments/.test(html),
  'tool args render as a collapsed toggle, not an open dump');
assert.ok(/agy-args-pre/.test(html) && !/agy-args-pre open/.test(html),
  'args block exists and starts collapsed');
assert.ok(!/style="color:/.test(html), 'result colour is classed, not inline (D5a)');
assert.ok(/agy-result-code/.test(html), 'result block uses the capped scroll class');
assert.ok(!/onclick=/.test(html), 'no inline onclick handlers (CSP forbids them)');
assert.ok(!/ src\/app\.js/.test(html) || /\u00b7 src\/app\.js/.test(html),
  'path appears once as a labeled preview, not doubled in the dump');

// args never reach the DOM unsanitised: a path with <script> is escaped
const evilActs = [
  { type: 'step', step: 1 },
  { type: 'tool_call', id: 'x', name: 'read_file', args: { path: '"><script>alert(1)</script>' } },
  { type: 'tool_result', id: 'x', name: 'read_file', ok: true, result: 'ok' },
];
const evilHtml = ctx.agentActsHtml(JSON.parse(JSON.stringify(evilActs)), false);
assert.ok(!/<script>alert\(1\)/.test(evilHtml), 'tool args must be HTML-escaped');
assert.ok(/&lt;script&gt;/.test(evilHtml), 'escaped form present');

// --- diff lines use classes, not inline styles ---------------------------
const fileActs = [
  { type: 'step', step: 1 },
  { type: 'tool_call', id: 'f', name: 'edit_file', args: { path: 'a.py', new_string: 'x' } },
  { type: 'tool_result', id: 'f', name: 'edit_file', ok: true,
    diff: { added: 1, removed: 0, hunks: [{ t: '+', s: 'x = 1' }], truncated: false } },
];
const fileHtml = ctx.agentActsHtml(JSON.parse(JSON.stringify(fileActs)), false);
assert.ok(/agy-diff-line/.test(fileHtml), 'diff renders with agy-diff-line classes');
assert.ok(/codex-diff-pill add/.test(fileHtml), 'diff pill shows +N');

console.log('agent cards tests: OK');

// node tests/js/test_vscode_lib.js - the pure logic of the VS Code extension (vscode-extension/lib.js).
const assert = require('assert');
const path = require('path');
const lib = require(path.join(__dirname, '..', '..', 'vscode-extension', 'lib.js'));

// untrusted text never carries terminal/escape control
const evil = 'ok\x1b[2J\x1b]52;c;QUJD\x07 red\x08\x00';
assert.ok(!/[\x00-\x08\x1b\x07]/.test(lib.clean(evil)));
assert.strictEqual(lib.oneLine('a\n\n  b\t c'), 'a b c');
assert.strictEqual(lib.oneLine('x'.repeat(500), 50).length, 50);

// prompt building
assert.throws(() => lib.buildPrompt({ question: '  ' }), /question/);
let p = lib.buildPrompt({ question: 'why slow?', file: 'src/a.py', languageId: 'python', selection: 'x = 1', startLine: 3, endLine: 5 });
assert.ok(p.startsWith('why slow?'));
assert.ok(p.includes('`src/a.py` lines 3-5'));
assert.ok(p.includes('Selected code (python):\n```\nx = 1\n```'));
p = lib.buildPrompt({ question: 'q', selection: 'a ``` b' });
assert.ok(p.includes('~~~~'), 'a selection that holds a fence gets a different one');
p = lib.buildPrompt({ question: 'q', selection: 'z'.repeat(lib.MAX_SELECTION_CHARS + 50) });
assert.ok(p.includes('(truncated)'));
assert.ok(p.length < lib.MAX_SELECTION_CHARS + 200);
assert.strictEqual(lib.buildPrompt({ question: 'only a question' }), 'only a question');

// incremental SSE: frames split across chunks, ids, comments, garbage
const sp = new lib.SseParser();
let got = sp.push(': ping\nid: 4\nevent: del');
assert.deepStrictEqual(got, []);
got = sp.push('ta\ndata: {"text":"hi"}\n\nevent: x\ndata: nope\n\nevent: done\r\ndata: {"state":"completed"}\r\n');
assert.strictEqual(JSON.stringify(got), JSON.stringify([
  { seq: 4, event: 'delta', data: { text: 'hi' } },
  { seq: null, event: 'done', data: { state: 'completed' } },
]));

// rendering a run
const r = new lib.Renderer();
const text = [
  ['delta', { text: 'Looking' }], ['tool_call', { name: 'read_file', args: { path: 'a.py', content: 'z'.repeat(900) } }],
  ['tool_result', { ok: false, result: 'error: nope\x1b[31m' }], ['done', { state: 'stopped', reason: 'step_limit' }],
].flatMap(([e, d]) => r.feed(e, d)).join('');
assert.ok(text.includes('Looking\n> read_file path="a.py" content=<900 chars>\n'));
assert.ok(text.includes('  ERR error: nope\n'));
assert.ok(text.includes('[stopped] step_limit'));
assert.ok(!text.includes('\x1b'));
assert.strictEqual(r.finalState, 'stopped');

// approval cards
assert.ok(lib.cardMessage({ kind: 'rule', cmd: 'x', tainted: true }).includes('outside content'));
assert.ok(lib.cardMessage({ cmd: 'rm -rf b\x1b[2J' }).startsWith('Run a command'));
assert.ok(!lib.cardMessage({ cmd: 'rm -rf b\x1b[2J' }).includes('\x1b'));
assert.deepStrictEqual(lib.cardChoices('shell').map((c) => c.decision), ['allow']);
assert.deepStrictEqual(lib.cardChoices('rule').map((c) => c.decision), ['allow', 'project']);
for (const c of [...lib.cardChoices('shell'), ...lib.cardChoices('edit')]) {
  assert.ok(!['always', 'user'].includes(c.decision), 'saved allow-lists are never offered from the editor');
}

// the token only ever goes over https, or to this machine
assert.strictEqual(lib.normalizeBase('http://127.0.0.1:8000/'), 'http://127.0.0.1:8000');
assert.strictEqual(lib.normalizeBase('http://localhost:8000'), 'http://localhost:8000');
assert.strictEqual(lib.normalizeBase('https://agent.example.test'), 'https://agent.example.test');
for (const bad of ['http://agent.example.test', 'ftp://x', 'agent.example.test', '', 'http://localhost.evil.test']) {
  assert.throws(() => lib.normalizeBase(bad), /baseUrl|https/, bad);
}

console.log('vscode extension lib tests passed');

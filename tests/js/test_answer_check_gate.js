// node tests/js/test_answer_check_gate.js - the "check before showing" gate says what stage it is in and can be skipped
const assert = require('assert');
const fs = require('fs');
const path = require('path');
const vm = require('vm');

const src = fs.readFileSync(path.join(__dirname, '..', '..', 'static', 'js', 'answer-check.js'), 'utf8');
const esc = s => String(s).replace(/[&<>"]/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c]));
let rendered = 0;
const ctx = {
  window: {}, document: { getElementById: () => null, addEventListener() {} }, esc, console, md: esc,
  localStorage: { getItem: k => (k === 'answer_check' ? 'gate' : null), setItem() {} },
  messages: [], scheduleRenderLast() { rendered++; }, fetch: () => Promise.resolve({ ok: false }),
};
vm.createContext(ctx);
vm.runInContext(src, ctx);
const run = c => vm.runInContext(c, ctx);

// begin: gate mode hides the answer and records when it started
const L = {};
assert.strictEqual(ctx.answerCheckBegin(L), 'gate');
assert.strictEqual(L.check.state, 'pending');
assert.ok(L.check.t0 > 0);
assert.strictEqual(ctx.answerCheckHides({ check: L.check }), true);

// stage 1: writing - word count and the Show now button
const writing = ctx.answerCheckGateHtml({ check: L.check, content: 'one two three four' }, 3);
assert.ok(/Writing the answer/.test(writing) && /1\/2/.test(writing));
assert.ok(/4 words/.test(writing), 'shows how much is written');
assert.ok(/data-click="ac-skip" data-arg="3"/.test(writing), 'a Show now button for message 3');
assert.ok(/held back until it has been checked/.test(writing));

// stage 2: checking - names the checker, keeps the start time and any skip
assert.ok(ctx.sseAnswerCheck(L, 'verify_start', { mode: 'gate', checker: 'Executor <b>', source: 'local' }));
assert.strictEqual(L.check.state, 'checking');
assert.ok(L.check.t0 > 0 && L.check.t1 > 0, 'the start time survives the stage change');
const checking = ctx.answerCheckGateHtml({ check: L.check, content: 'x' }, 3);
assert.ok(/Double-checking with <b>Executor &lt;b&gt;<\/b>/.test(checking), 'the checker name is escaped');
assert.ok(/2\/2/.test(checking));

// Show now: reveals the answer, the check carries on and still lands its verdict
ctx.messages.push({ check: L.check, content: 'the draft answer' });
ctx.answerCheckSkip(0);
const m = ctx.messages[0];
assert.strictEqual(m.check.skipped, true);
assert.strictEqual(ctx.answerCheckHides(m), false, 'the answer is visible after Show now');
assert.strictEqual(rendered, 1, 'the last message is redrawn');
L.check = m.check;
ctx.sseAnswerCheck(L, 'verify_result', { verdict: 'pass', issues: [] });
assert.strictEqual(L.check.state, 'done');
assert.strictEqual(L.check.skipped, true, 'a skip is kept when the verdict arrives');

// without an index there is no button (e.g. a non-chat caller)
assert.ok(!/ac-skip/.test(ctx.answerCheckGateHtml({ check: { state: 'pending', t0: Date.now() } })));
// meta: seconds only before any text
assert.ok(/^\d+s$/.test(ctx.answerCheckGateMeta({ check: { state: 'pending', t0: Date.now() }, content: '' })));
console.log('answer check gate tests: OK');

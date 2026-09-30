// node tests/js/test_history_clip.js - older turns are sent shortened, the newest request is not
const assert = require('assert');
const fs = require('fs');
const path = require('path');
const vm = require('vm');

const src = fs.readFileSync(path.join(__dirname, '..', '..', 'static', 'js', 'history-clip.js'), 'utf8');
const ctx = { window: {} };
vm.createContext(ctx);
vm.runInContext(src, ctx);
const clip = h => JSON.parse(JSON.stringify(ctx.clipHistoryForRun(h)));

const file = '--- FILE: game.js ---\n' + 'x'.repeat(12000) + '\n--- END game.js ---';
const hist = [
  { role: 'user', content: 'review this\n\n' + file },
  { role: 'assistant', content: 'A'.repeat(5000) + 'TAIL' },
  { role: 'user', content: 'now fix the bug\n\n' + file },       // the newest request keeps its attachment
];
const out = clip(hist);

assert.ok(out[0].content.length < 200, 'old attachment is replaced by a one-line stub');
assert.ok(out[0].content.startsWith('review this'));
assert.ok(/game\.js/.test(out[0].content) && /omitted/.test(out[0].content));
assert.ok(out[1].content.length < 1700);
assert.ok(out[1].content.endsWith('TAIL'), 'the end of a long answer survives');
assert.ok(/shortened/.test(out[1].content));
assert.strictEqual(out[2].content, hist[2].content, 'the current request is untouched');

// short turns and non-text content pass through unchanged, and the input is not mutated
const small = [{ role: 'user', content: 'hi' }, { role: 'assistant', content: 'hello' }, { role: 'user', content: 'go' }];
assert.deepStrictEqual(clip(small), small);
assert.strictEqual(hist[0].content.length, ('review this\n\n' + file).length);
const multi = [{ role: 'user', content: [{ type: 'text', text: 'a' }] }, { role: 'user', content: 'go' }];
assert.deepStrictEqual(clip(multi), multi);

// images injected as text blocks are dropped from old turns
const img = [{ role: 'user', content: 'look\n--- IMAGE: a.png ---\ndescription...\n--- END a.png ---' }, { role: 'user', content: 'and now?' }];
assert.ok(/image from an earlier turn omitted/.test(clip(img)[0].content));

console.log('history clip tests: OK');

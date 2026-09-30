// node tests/js/test_sampling_config.js - sanitizeSamplingConfig defaults and clamps for the extra llama.cpp params
const assert = require('assert');
const fs = require('fs');
const path = require('path');
const vm = require('vm');

const src = fs.readFileSync(path.join(__dirname, '..', '..', 'static', 'js', 'sampling.js'), 'utf8');
const ctx = { window: { addEventListener() {} }, localStorage: { getItem: () => null }, document: {} };
vm.createContext(ctx);
vm.runInContext(src + '\nthis.sanitize = sanitizeSamplingConfig; this.body = samplingExtraBody;', ctx);
const j = o => JSON.parse(JSON.stringify(o));

const d = j(ctx.sanitize({}));
assert.deepStrictEqual([d.rlast, d.freq, d.seed], [64, 0, -1]);
assert.deepStrictEqual([d.drym, d.dryb, d.dryl, d.dryn], [0, 1.75, 2, 4096]);
assert.deepStrictEqual([d.dynr, d.dyne], [0, 1]);

const c = j(ctx.sanitize({ rlast: 99999, freq: -9, seed: -5, drym: 99, dryb: 0, dryl: 999, dryn: -50, dynr: 9, dyne: 0 }));
assert.deepStrictEqual([c.rlast, c.freq, c.seed], [8192, -2, -1]);
assert.deepStrictEqual([c.drym, c.dryb, c.dryl, c.dryn], [5, 1, 64, -1]);
assert.deepStrictEqual([c.dynr, c.dyne], [2, 0.1]);

const b = j(ctx.body(ctx.sanitize({ seed: 9, drym: 0.5 })));
assert.strictEqual(b.seed, 9);
assert.strictEqual(b.dry_multiplier, 0.5);
assert.strictEqual(b.repeat_last_n, 64);
console.log('ok');

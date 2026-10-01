// node tests/js/test_companion_fsops.js - companion/fsops.js remove + verify (electron faked).
const assert = require('assert');
const fs = require('fs');
const os = require('os');
const path = require('path');
const cp = require('child_process');

const Module = require('module');
const origLoad = Module._load;
Module._load = function (request, ...rest) {
  if (request === 'electron') return { dialog: {}, BrowserWindow: { getFocusedWindow: () => null, getAllWindows: () => [] } };
  return origLoad.call(this, request, ...rest);
};

const fsops = require(path.join(__dirname, '..', '..', 'companion', 'fsops.js'));
const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'a770-fsops-'));

(async () => {
  // remove: a file goes, a missing file is not an error, a folder is refused
  const f = path.join(dir, 'x.txt');
  fs.writeFileSync(f, 'hi');
  assert.deepStrictEqual(fsops.remove({ path: f }), { removed: true });
  assert.ok(!fs.existsSync(f));
  assert.deepStrictEqual(fsops.remove({ path: f }), { removed: false });
  assert.throws(() => fsops.remove({ path: dir }), /not a file/);

  // verify: only JS is checked; the checker is faked so the test needs no Electron binary
  const seen = [];
  cp.execFile = (file, args, opts, cb) => {
    seen.push({ args, run_as_node: opts.env.ELECTRON_RUN_AS_NODE });
    const bad = fs.readFileSync(args[1], 'utf8').includes('((');
    if (!bad) return cb(null, '', '');
    const err = new Error('failed'); err.code = 1;
    cb(err, '', `${args[1]}:1\nfoo((\n  ^\n\nSyntaxError: Unexpected token '}'\n`);
  };
  const good = path.join(dir, 'ok.js');
  fs.writeFileSync(good, 'const a = 1;\n');
  assert.deepStrictEqual(await fsops.verify({ path: good }), { checked: true, ok: true });
  assert.strictEqual(seen[0].args[0], '--check');
  assert.strictEqual(seen[0].run_as_node, '1');

  const bad = path.join(dir, 'bad.js');
  fs.writeFileSync(bad, 'foo((\n');
  const r = await fsops.verify({ path: bad });
  assert.strictEqual(r.checked, true);
  assert.strictEqual(r.ok, false);
  assert.match(r.detail, /SyntaxError/);
  assert.match(r.detail, /^bad\.js:1 /, 'error position is shown without the full path');

  const py = path.join(dir, 'a.py');
  fs.writeFileSync(py, 'print(1)\n');
  assert.deepStrictEqual(await fsops.verify({ path: py }), { checked: false });
  assert.strictEqual(seen.length, 2, 'non-JS files never reach the checker');

  fs.rmSync(dir, { recursive: true, force: true });
  console.log('companion fsops tests passed');
})().catch((e) => { console.error(e); process.exit(1); });

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

  // readMany: incremental source fetch for the symbol index
  const src = path.join(dir, 'src');
  fs.mkdirSync(path.join(src, 'pkg'), { recursive: true });
  for (const skip of ['node_modules', 'dist', 'vendor', '.hidden']) {
    fs.mkdirSync(path.join(src, skip), { recursive: true });
    fs.writeFileSync(path.join(src, skip, 'noise.py'), 'x = 1\n');
  }
  fs.writeFileSync(path.join(src, 'a.py'), 'def a():\n    pass\n');
  fs.writeFileSync(path.join(src, 'pkg', 'b.ts'), 'export const b = 1;\n');
  fs.writeFileSync(path.join(src, 'notes.md'), '# not code\n');
  fs.writeFileSync(path.join(src, 'big.py'), '#'.repeat(5000));
  const opts = { root: src, exts: ['.py', '.ts'], max_file_bytes: 2048 };

  let m = fsops.readMany({ ...opts, known: {} });
  assert.deepStrictEqual(m.all.slice().sort(), ['a.py', 'pkg/b.ts'], 'only code files, no skip dirs, no oversize file');
  assert.strictEqual(m.skipped_large, 1);
  assert.deepStrictEqual(m.files.map((f) => f.rel).sort(), ['a.py', 'pkg/b.ts']);
  assert.strictEqual(m.files.find((f) => f.rel === 'a.py').text, 'def a():\n    pass\n');
  assert.strictEqual(m.more, false);

  // nothing changed: the same call returns no text at all
  const known = Object.fromEntries(m.files.map((f) => [f.rel, f.stamp]));
  m = fsops.readMany({ ...opts, known });
  assert.deepStrictEqual(m.files, []);
  assert.strictEqual(m.all.length, 2);

  // an edit (size changes) comes back; a deletion disappears from `all`
  fs.writeFileSync(path.join(src, 'a.py'), 'def a():\n    return 1\n');
  fs.unlinkSync(path.join(src, 'pkg', 'b.ts'));
  m = fsops.readMany({ ...opts, known });
  assert.deepStrictEqual(m.files.map((f) => f.rel), ['a.py']);
  assert.deepStrictEqual(m.all, ['a.py']);

  // a small text budget splits the work: `more` says call again, and a second call finishes it
  for (let i = 0; i < 4; i++) fs.writeFileSync(path.join(src, `m${i}.py`), 'y'.repeat(40 * 1024));
  const have = {};
  let calls = 0;
  for (;;) {
    m = fsops.readMany({ ...opts, max_file_bytes: 64 * 1024, budget_bytes: 64 * 1024, known: have });
    calls++;
    for (const f of m.files) have[f.rel] = f.stamp;
    if (!m.more) break;
    assert.ok(calls < 20, 'must make progress');
  }
  assert.ok(calls >= 3, 'budget of 64 KB forced batching: ' + calls);
  // a.py, big.py (now under the cap) and m0..m3
  assert.strictEqual(Object.keys(have).length, 6, 'every file arrived across the batches');

  // diagnose: ruff for Python, the project's eslint for JS/TS; tools are faked, nothing is installed for the test
  const realExecFile = cp.execFile;
  let ranWith = null;
  const fakeTool = (stdout, err) => {
    cp.execFile = (file, args, opts, cb) => { ranWith = { file, args, opts }; cb(err || null, stdout, ''); };
  };
  const diag = path.join(dir, 'diag');
  fs.mkdirSync(diag, { recursive: true });
  const pyFile = path.join(diag, 'm.py');
  fs.writeFileSync(pyFile, 'print(undefined_name)\n');

  const exit1 = Object.assign(new Error('exit 1'), { code: 1 });
  fakeTool(JSON.stringify([{ code: 'F821', message: "Undefined name `undefined_name`", location: { row: 1, column: 7 } }]), exit1);
  let d = await fsops.diagnose({ path: pyFile, root: diag });
  assert.deepStrictEqual(d, { checked: true, tool: 'ruff', total: 1,
    issues: [{ line: 1, col: 7, code: 'F821', message: 'Undefined name `undefined_name`' }] });
  assert.strictEqual(ranWith.file, 'ruff');
  assert.ok(ranWith.args.includes('--isolated') && ranWith.args.includes('--no-fix'), 'project config and autofix are off');
  assert.strictEqual(ranWith.args[ranWith.args.indexOf('--select') + 1], 'E9,F63,F7,F82');

  fakeTool('[]');
  d = await fsops.diagnose({ path: pyFile, root: diag });
  assert.deepStrictEqual(d, { checked: true, tool: 'ruff', issues: [], total: 0 });

  fakeTool('', Object.assign(new Error('not found'), { code: 'ENOENT' }));
  d = await fsops.diagnose({ path: pyFile, root: diag });
  assert.strictEqual(d.checked, false, 'a missing ruff is not an error');
  fakeTool('not json', exit1);
  assert.strictEqual((await fsops.diagnose({ path: pyFile, root: diag })).checked, false);

  // many findings are capped, with the true total reported
  fakeTool(JSON.stringify(Array.from({ length: 50 }, (_, i) => ({ code: 'F821', message: 'x', location: { row: i + 1, column: 1 } }))), exit1);
  d = await fsops.diagnose({ path: pyFile, root: diag });
  assert.strictEqual(d.issues.length, 20);
  assert.strictEqual(d.total, 50);

  // eslint: needs the project's own copy and config; errors only
  const jsFile = path.join(diag, 'a.js');
  fs.writeFileSync(jsFile, 'x = 1;\n');
  fakeTool('[]');
  assert.strictEqual((await fsops.diagnose({ path: jsFile, root: diag })).checked, false, 'no eslint in the project');
  fs.mkdirSync(path.join(diag, 'node_modules', 'eslint', 'bin'), { recursive: true });
  fs.writeFileSync(path.join(diag, 'node_modules', 'eslint', 'bin', 'eslint.js'), '');
  fakeTool(JSON.stringify([{ messages: [
    { ruleId: 'no-undef', severity: 2, message: "'x' is not defined.", line: 1, column: 1 },
    { ruleId: 'semi', severity: 1, message: 'warning only', line: 1, column: 6 }] }]));
  d = await fsops.diagnose({ path: jsFile, root: diag });
  assert.deepStrictEqual(d, { checked: true, tool: 'eslint', total: 1,
    issues: [{ line: 1, col: 1, code: 'no-undef', message: "'x' is not defined." }] });
  assert.strictEqual(ranWith.file, process.execPath);
  assert.strictEqual(ranWith.opts.env.ELECTRON_RUN_AS_NODE, '1');
  assert.strictEqual(ranWith.opts.cwd, diag);

  fakeTool('Oops! Something went wrong! couldn\'t find an eslint.config.js');
  assert.strictEqual((await fsops.diagnose({ path: jsFile, root: diag })).checked, false, 'no config: not guessed at');
  const tsFile = path.join(diag, 'a.ts');
  fs.writeFileSync(tsFile, 'const a: number = 1;\n');
  fakeTool(JSON.stringify([{ messages: [{ ruleId: null, fatal: true, severity: 2, message: 'Parsing error', line: 1, column: 8 }] }]));
  assert.strictEqual((await fsops.diagnose({ path: tsFile, root: diag })).checked, false, 'TS without a TS parser is not a file bug');
  fakeTool(JSON.stringify([{ messages: [{ ruleId: null, fatal: true, severity: 2, message: 'Parsing error', line: 1, column: 8 }] }]));
  d = await fsops.diagnose({ path: jsFile, root: diag });
  assert.strictEqual(d.issues[0].code, 'parse', 'a JS parse error is reported');

  assert.strictEqual((await fsops.diagnose({ path: path.join(diag, 'notes.md'), root: diag })).checked, false);
  fs.writeFileSync(path.join(diag, 'notes.md'), '# hi');
  assert.strictEqual((await fsops.diagnose({ path: path.join(diag, 'notes.md'), root: diag })).checked, false);
  cp.execFile = realExecFile;

  fs.rmSync(dir, { recursive: true, force: true });
  console.log('companion fsops tests passed');
})().catch((e) => { console.error(e); process.exit(1); });

// node tests/js/test_companion_excelops.js - companion/excelops.js: what may be opened, how the fixed script is started (path only as
// an argument), the timeout, cleanup, caps. PowerShell/Excel are stubbed; nothing real is launched.
const assert = require('assert');
const fs = require('fs');
const os = require('os');
const path = require('path');
const { EventEmitter } = require('events');

const ex = require(path.join(__dirname, '..', '..', 'companion', 'excelops.js'));
const tmp = fs.mkdtempSync(path.join(os.tmpdir(), 'a770-excelops-test-'));

// a minimal "xlsx": a zip header is all the check looks at
const zip = (extra = '') => Buffer.concat([Buffer.from([0x50, 0x4b, 0x03, 0x04]), Buffer.from('xl/workbook.xml' + extra)]);
const b64 = (b) => b.toString('base64');

// a stub spawn: acts like excel_recalc.ps1, writing out.json next to the book; `behaviour` decides what happens
function fakeSpawn(behaviour, seen) {
  return (cmd, args) => {
    seen.push({ cmd, args });
    const child = new EventEmitter();
    child.stderr = new EventEmitter();
    child.pid = 4242;
    const named = (n) => args[args.indexOf(n) + 1];
    setTimeout(() => {
      if (behaviour === 'ok') {
        fs.writeFileSync(named('-Out'), JSON.stringify({ ok: true, formulas: 2, errors: 1, version: '16.0', cells: { 'A!B2': 8, 'A!B3': '#DIV/0!' } }));
        child.emit('close', 0);
      } else if (behaviour === 'fail') {
        fs.writeFileSync(named('-Out'), JSON.stringify({ ok: false, error: 'Excel could not open the file' }));
        child.emit('close', 2);
      } else if (behaviour === 'silent') {
        child.emit('close', 1);                       // exits without writing a result
      } else if (behaviour === 'huge') {
        fs.writeFileSync(named('-Out'), Buffer.alloc(9 * 1024 * 1024, 120));
        child.emit('close', 0);
      }                                               // 'hang': never closes
    }, 20);
    return child;
  };
}

const rejects = async (p, re, msg) => {
  try { await p; } catch (e) { assert.match(e.message, re, msg); return; }
  assert.fail('expected a refusal: ' + msg);
};

(async () => {
  // what may be opened
  assert.doesNotThrow(() => ex.check(zip(), 'a.xlsx'));
  for (const bad of ['a.xlsm', 'a.xlsb', 'a.xls', 'a.csv', 'noext']) {
    assert.throws(() => ex.check(zip(), bad), /only \.xlsx/, bad);
  }
  assert.throws(() => ex.check(Buffer.from('not a zip at all'), 'a.xlsx'), /not an \.xlsx/);
  assert.throws(() => ex.check(zip('xl/vbaProject.bin'), 'a.xlsx'), /macros/);
  assert.throws(() => ex.check(Buffer.concat([zip(), Buffer.alloc(ex.MAX_BYTES)]), 'a.xlsx'), /larger than/);

  // not Windows: refused before anything is written or started
  const seen0 = [];
  await rejects(ex.recalc({ data_b64: b64(zip()), name: 'a.xlsx' }, { platform: 'linux', spawn: fakeSpawn('ok', seen0), tmpdir: tmp }), /Windows only/, 'platform');
  assert.strictEqual(seen0.length, 0);

  // the normal path
  const seen = [];
  const r = await ex.recalc({ data_b64: b64(zip()), name: 'Sales.xlsx' }, { platform: 'win32', spawn: fakeSpawn('ok', seen), tmpdir: tmp });
  assert.deepStrictEqual(r.cells, { 'A!B2': 8, 'A!B3': '#DIV/0!' });
  assert.strictEqual(r.formulas, 2);
  assert.strictEqual(r.errors, 1);
  assert.strictEqual(r.version, '16.0');
  assert.strictEqual(seen.length, 1);
  assert.strictEqual(seen[0].cmd, 'powershell.exe');
  const a = seen[0].args;
  assert.ok(a.includes('-NoProfile') && a.includes('-NonInteractive'), 'no profile, no prompts');
  const scriptPath = a[a.indexOf('-File') + 1];
  assert.ok(scriptPath.endsWith('excel_recalc.ps1'), 'only the shipped script runs: ' + scriptPath);
  assert.ok(fs.existsSync(path.join(__dirname, '..', '..', 'companion', 'excel_recalc.ps1')), 'the script ships with the app');
  const book = a[a.indexOf('-Path') + 1];
  assert.ok(book.endsWith('book.xlsx'), 'the workbook is a temp copy passed as an argument');
  assert.ok(!a.some((x) => /Excel\.Application|New-Object|Invoke-Expression|-Command/i.test(x)), 'no code text on the command line');
  assert.strictEqual(fs.existsSync(path.dirname(book)), false, 'the temp directory is removed afterwards');

  // Excel reports a failure / exits silently / returns too much
  await rejects(ex.recalc({ data_b64: b64(zip()), name: 'a.xlsx' }, { platform: 'win32', spawn: fakeSpawn('fail', []), tmpdir: tmp }), /could not calculate this workbook: Excel could not open/, 'fail');
  await rejects(ex.recalc({ data_b64: b64(zip()), name: 'a.xlsx' }, { platform: 'win32', spawn: fakeSpawn('silent', []), tmpdir: tmp }), /produced no result/, 'silent');
  await rejects(ex.recalc({ data_b64: b64(zip()), name: 'a.xlsx' }, { platform: 'win32', spawn: fakeSpawn('huge', []), tmpdir: tmp }), /too large/, 'huge');

  // a hang: Excel's own pid (written by the script) is ended with taskkill, the call returns, the temp dir is gone
  const seenH = [];
  const spawnH = (cmd, args) => {
    const child = fakeSpawn('hang', seenH)(cmd, args);
    if (cmd === 'powershell.exe') fs.writeFileSync(args[args.indexOf('-PidFile') + 1], '31337');   // the script recorded its Excel
    return child;
  };
  const t0 = Date.now();
  await rejects(ex.recalc({ data_b64: b64(zip()), name: 'a.xlsx' }, { platform: 'win32', spawn: spawnH, tmpdir: tmp, timeoutMs: 300 }), /did not finish/, 'timeout');
  assert.ok(Date.now() - t0 < 6000, 'returned promptly');
  const kill = seenH.find((s) => s.cmd === 'taskkill');
  assert.ok(kill, 'Excel itself was ended, not just PowerShell');
  assert.deepStrictEqual(kill.args.slice(0, 2), ['/PID', '31337']);
  assert.deepStrictEqual(fs.readdirSync(tmp).filter((n) => n.startsWith('a770-excel-')), [], 'no temp directories left behind');

  // the script itself: arguments only, macros off, never saved, one instance of its own
  const ps = fs.readFileSync(path.join(__dirname, '..', '..', 'companion', 'excel_recalc.ps1'), 'utf8');
  assert.match(ps, /AutomationSecurity = 3/);
  assert.match(ps, /Workbooks\.Open\(\$Path, 0, \$true\)/);
  assert.match(ps, /\.Close\(\$false\)/);
  assert.ok(!/\.Save\(|\.SaveAs\(/i.test(ps), 'the workbook is never saved');
  assert.ok(!/Invoke-Expression|iex |DownloadString|Start-Process/i.test(ps), 'nothing is downloaded or evaluated');

  // the op is registered and gated in main.js
  const main = fs.readFileSync(path.join(__dirname, '..', '..', 'companion', 'main.js'), 'utf8');
  assert.match(main, /"excel\.recalc": async/);
  assert.ok(main.indexOf('confirmAction("calculate a workbook in Excel?"') > 0, 'asks the user on this PC');
  assert.ok(main.indexOf('confirmAction("calculate a workbook in Excel?"') < main.indexOf('excelops().recalc'), 'asks BEFORE opening');
  const pkg = JSON.parse(fs.readFileSync(path.join(__dirname, '..', '..', 'companion', 'package.json'), 'utf8'));
  assert.ok(pkg.build.asarUnpack.includes('excel_recalc.ps1'), 'PowerShell cannot read a script inside app.asar');

  fs.rmSync(tmp, { recursive: true, force: true });
  console.log('companion excelops: ok');
})().catch((e) => { console.error(e); process.exit(1); });

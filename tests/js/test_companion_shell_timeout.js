// node tests/js/test_companion_shell_timeout.js - companion/shellops.js run(): a timeout kills the whole process
// tree (not just the shell), exit codes pass through, and output is bounded.
const assert = require('assert');
const fs = require('fs');
const os = require('os');
const path = require('path');

const shellops = require(path.join(__dirname, '..', '..', 'companion', 'shellops.js'));
const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'a770-shellops-'));
const node = `"${process.execPath}"`;
// quoted executable paths are a cmd.exe idiom (the companion's default Windows shell is PowerShell)
const shell = process.platform === 'win32' ? 'cmd' : undefined;

function alive(pid) {
  try { process.kill(pid, 0); return true; } catch { return false; }
}
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

(async () => {
  // a command that starts a grandchild and then hangs: both must be gone after the timeout
  const hang = path.join(dir, 'hang.js');
  fs.writeFileSync(hang, `
    const { spawn } = require('child_process');
    const kid = spawn(process.execPath, ['-e', 'setInterval(() => {}, 1000)'], { stdio: 'ignore' });
    console.log('KID=' + kid.pid);
    setInterval(() => {}, 1000);
  `);
  const t0 = Date.now();
  const r = await shellops.run({ command: `${node} "${hang}"`, timeout: 1, shell });
  const took = Date.now() - t0;
  assert.ok(took < 8000, `returned promptly after the timeout, took ${took} ms`);
  assert.strictEqual(r.exit_code, 124, 'timeout has its own exit code');
  assert.match(r.stderr, /killed: timeout/);
  const kid = Number((r.stdout.match(/KID=(\d+)/) || [])[1]);
  assert.ok(kid > 0, 'the grandchild reported its pid: ' + r.stdout);
  await sleep(1500);
  assert.strictEqual(alive(kid), false, 'the grandchild was killed with the tree, not left running');

  // exit codes and output pass through
  const ok = await shellops.run({ command: `${node} -e "console.log('out'); console.error('err'); process.exit(3)"`, timeout: 20, shell });
  assert.strictEqual(ok.exit_code, 3);
  assert.match(ok.stdout, /out/);
  assert.match(ok.stderr, /err/);
  assert.strictEqual((await shellops.run({ command: 'echo hi', timeout: 20 })).exit_code, 0);

  // a command that cannot start is an error result, not a thrown exception
  const missing = await shellops.run({ command: 'definitely-not-a-real-command-xyz', timeout: 20 });
  assert.notStrictEqual(missing.exit_code, 0);

  // endless output costs a bounded amount: the tail is kept, the call still returns
  const flood = await shellops.run({
    command: `${node} -e "const b='x'.repeat(65536); for (let i=0;i<64;i++) process.stdout.write(b); process.stdout.write('THE-END')"`,
    timeout: 30, shell,
  });
  assert.strictEqual(flood.exit_code, 0);
  assert.ok(flood.stdout.length <= 20000, 'output is capped: ' + flood.stdout.length);
  assert.ok(flood.stdout.endsWith('THE-END'), 'the end of the output is what is kept');

  // no timeout given: the command simply runs to completion
  assert.strictEqual((await shellops.run({ command: 'echo done' })).exit_code, 0);

  fs.rmSync(dir, { recursive: true, force: true });
  console.log('companion shell timeout tests passed');
})().catch((e) => { console.error(e); process.exit(1); });

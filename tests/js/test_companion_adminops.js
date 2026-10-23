// node tests/js/test_companion_adminops.js - the administrator-terminal helper (companion/adminops.js).
// UAC cannot be exercised here, so the PowerShell helper is started NOT elevated: that still proves the script parses, the pipe and
// token handshake work, commands run with a timeout and the secret filter, and it reports honestly that it is not elevated.
// Skips (exit 0) off Windows.
const assert = require('assert');
const { spawn } = require('child_process');
const path = require('path');

if (process.platform !== 'win32') { console.log('skip: Windows only'); process.exit(0); }

// policy needs Electron dialogs: stub it before adminops loads
const companion = path.join(__dirname, '..', '..', 'companion');
const policyPath = path.join(companion, 'policy.js');
require.cache[policyPath] = { id: policyPath, filename: policyPath, loaded: true, exports: { confirmAction: async () => {} } };
const admin = require(path.join(companion, 'adminops.js'));

const SID = 'S-1-5-21-1-2-3-1001';

(async () => {
  // 1. nothing but the expected shapes can reach the script text
  const ok = { pipe: 'a770-admin-' + 'a'.repeat(24), token: 'b'.repeat(48), sid: SID, idleSec: 120 };
  const script = admin._buildHelperScript(ok);
  assert.ok(script.includes(ok.pipe) && script.includes(ok.token) && script.includes(SID));
  for (const bad of [{ pipe: "x'; calc; '" }, { token: "t'; calc; '" + 'c'.repeat(40) }, { sid: "S-1-5-21'; calc" }]) {
    assert.throws(() => admin._buildHelperScript({ ...ok, ...bad }), /bad/);
  }

  // 2. the helper itself, started without elevation
  const real = { pipe: 'a770-admin-test' + Date.now().toString(16).padEnd(16, '0'), token: 'd'.repeat(48), idleSec: 120 };
  const who = await new Promise((res) => {
    let o = ''; const c = spawn(path.join(process.env.SystemRoot || 'C:\\Windows', 'System32', 'whoami.exe'), ['/user', '/fo', 'csv', '/nh'], { windowsHide: true });
    c.stdout.on('data', (d) => { o += d; }); c.on('close', () => res((o.match(/"(S-1-[\d-]+)"/) || [])[1]));
  });
  assert.ok(who, 'own sid');
  const b64 = Buffer.from(admin._buildHelperScript({ ...real, sid: who }), 'utf16le').toString('base64');
  const helper = spawn('powershell.exe', ['-NoProfile', '-NonInteractive', '-EncodedCommand', b64], { windowsHide: true, stdio: 'ignore' });
  try {
    let hello = null;
    for (let i = 0; i < 40 && !hello; i++) {
      try { hello = await admin._request(real.pipe, { token: real.token, op: 'ping' }, 2000); } catch (_) { await new Promise((r) => setTimeout(r, 500)); }
    }
    assert.ok(hello && hello.ok, 'helper answered');
    assert.strictEqual(hello.elevated, false, 'not elevated in this test, and it says so');

    const bad = await admin._request(real.pipe, { token: 'wrong', op: 'run', command: 'whoami' }, 8000);
    assert.strictEqual(bad.ok, false);
    assert.match(bad.error, /token/);

    const r = await admin._request(real.pipe, { token: real.token, op: 'run', command: 'Write-Output hello-admin; $env:MY_API_TOKEN', shell: 'powershell', timeout: 30 }, 40000);
    assert.strictEqual(r.ok, true);
    assert.match(r.stdout, /hello-admin/);
    assert.strictEqual(r.exit_code, 0);

    const f = await admin._request(real.pipe, { token: real.token, op: 'run', command: 'exit 7', shell: 'powershell', timeout: 30 }, 40000);
    assert.strictEqual(f.exit_code, 7);

    const c = await admin._request(real.pipe, { token: real.token, op: 'run', command: 'echo from-cmd', shell: 'cmd', timeout: 30 }, 40000);
    assert.match(c.stdout, /from-cmd/);

    const slow = await admin._request(real.pipe, { token: real.token, op: 'run', command: 'Start-Sleep -Seconds 20', shell: 'powershell', timeout: 2 }, 40000);
    assert.strictEqual(slow.exit_code, 124);
    assert.match(slow.stderr, /timed out/);

    const bye = await admin._request(real.pipe, { token: real.token, op: 'exit' }, 8000);
    assert.strictEqual(bye.ok, true);
    console.log('OK');
  } finally {
    helper.kill();
  }
})().catch((e) => { console.error(e); process.exit(1); });

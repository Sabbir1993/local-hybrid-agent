// node tests/js/test_companion_browseraudit.js - browser.audit against the real installed
// Edge/Chrome (headless), policy stubbed like test_companion_browserops.js.
// Skips (exit 0) when playwright-core isn't installed in companion/ or no browser is found.
const assert = require('assert');
const http = require('http');
const path = require('path');
const Module = require('module');

const companion = path.join(__dirname, '..', '..', 'companion');
try { require(path.join(companion, 'node_modules', 'playwright-core')); } catch (_) {
  console.log('skip: playwright-core not installed in companion/'); process.exit(0);
}
const policyPath = path.join(companion, 'policy.js');
require.cache[policyPath] = { id: policyPath, filename: policyPath, loaded: true, exports: {
  ensurePath: async () => {}, confirmOrigin: async () => {}, confirmScript: async () => {},
} };
const origResolve = Module._resolveFilename;
Module._resolveFilename = function (req, parent, ...rest) {
  if (req === 'playwright-core') return origResolve.call(this, path.join(companion, 'node_modules', 'playwright-core'), parent, ...rest);
  return origResolve.call(this, req, parent, ...rest);
};
const ops = require(path.join(companion, 'browserops.js'));

const BROKEN = `<!doctype html><title>Broken</title><h1>Broken page</h1>
<img src="/missing.png" id="logo"><img src="/ok.png" alt="">
<button class="submit"></button>
<input id="email" placeholder="email">
<div style="width:3000px;height:10px">wide</div>
<script>console.error('planted console error'); fetch('/api/user');</script>`;
const GOOD = `<!doctype html><title>Good</title><h1>Hello there, a healthy page</h1>
<label for="n">Name</label><input id="n"><button>Save</button>`;

(async () => {
  const srv = http.createServer((req, res) => {
    if (req.url === '/broken') { res.writeHead(200, { 'Content-Type': 'text/html' }); res.end(BROKEN); }
    else if (req.url === '/good') { res.writeHead(200, { 'Content-Type': 'text/html' }); res.end(GOOD); }
    else if (req.url === '/api/user') { res.writeHead(500); res.end(); }
    else { res.writeHead(404); res.end(); }
  });
  await new Promise(r => srv.listen(0, '127.0.0.1', r));
  const base = `http://127.0.0.1:${srv.address().port}`;
  const S = { session: 'audit-test' };
  try {
    try { await ops.navigate({ ...S, url: base + '/broken' }); }
    catch (e) { if (/no Microsoft Edge or Google Chrome/.test(e.message)) { console.log('skip: no browser'); return; } throw e; }
    const d = await ops.audit({ ...S, screenshot: false });
    const kinds = d.issues.map(i => i.kind);
    for (const k of ['console_error', 'http_error', 'broken_image', 'missing_alt', 'unnamed_control', 'unlabelled_field', 'horizontal_overflow']) {
      assert.ok(kinds.includes(k), `expected ${k} in ${kinds}`);
    }
    assert.ok(d.summary.errors >= 2 && d.summary.warnings >= 4, JSON.stringify(d.summary));
    assert.ok(!d.png_b64, 'screenshot:false omits the png');
    // errors sort before warnings
    assert.strictEqual(d.issues[0].severity, 'error');

    await ops.navigate({ ...S, url: base + '/good' });
    const g = await ops.audit({ ...S });
    assert.strictEqual(g.summary.errors, 0, JSON.stringify(g.issues));
    assert.strictEqual(g.issues.length, 0, JSON.stringify(g.issues));
    assert.ok(g.png_b64 && g.png_b64.length > 100, 'png captured by default');

    await assert.rejects(() => ops.audit({ session: 'never-opened' }), /no browser open/);
    console.log('OK');
  } finally {
    await ops.close(S).catch(() => {});
    srv.close();
  }
})().catch(e => { console.error(e); process.exit(1); });

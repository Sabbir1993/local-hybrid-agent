// node tests/js/test_companion_browserops.js - companion/browserops.js against the real
// installed Edge/Chrome (headless), with the Electron-only policy module stubbed.
// Skips (exit 0) when playwright-core isn't installed in companion/ or no browser is found.
const assert = require('assert');
const http = require('http');
const path = require('path');
const Module = require('module');

const companion = path.join(__dirname, '..', '..', 'companion');
let pw;
try { pw = require(path.join(companion, 'node_modules', 'playwright-core')); } catch (_) {
  console.log('skip: playwright-core not installed in companion/'); process.exit(0);
}

// stub policy (it needs Electron dialogs): record what would have been asked
const asked = [];
const policyPath = path.join(companion, 'policy.js');
require.cache[policyPath] = { id: policyPath, filename: policyPath, loaded: true, exports: {
  ensurePath: async (p, why) => { asked.push(['path', p, why]); },
  confirmOrigin: async (o) => { asked.push(['origin', o]); throw new Error(`denied by local user: open ${o}`); },
  confirmScript: async (code) => { asked.push(['script', code]); },
} };
// browserops resolves playwright-core from companion/node_modules
const origResolve = Module._resolveFilename;
Module._resolveFilename = function (req, parent, ...rest) {
  if (req === 'playwright-core') return origResolve.call(this, path.join(companion, 'node_modules', 'playwright-core'), parent, ...rest);
  return origResolve.call(this, req, parent, ...rest);
};
const ops = require(path.join(companion, 'browserops.js'));

const PAGE = `<!doctype html><title>Todo</title><h1>Todos</h1>
<input id="t" placeholder="new todo"><button id="add">Add</button>
<input type="password" id="pw" placeholder="password"><input id="cc" autocomplete="cc-number" placeholder="card">
<ul id="list"></ul><img src="/missing.png">
<script>document.getElementById('add').onclick=()=>{const li=document.createElement('li');li.textContent=document.getElementById('t').value;document.getElementById('list').append(li);};
console.error('boom from page');</script>`;

(async () => {
  const srv = http.createServer((req, res) => {
    if (req.url === '/') { res.writeHead(200, { 'Content-Type': 'text/html' }); res.end(PAGE); }
    else { res.writeHead(404); res.end(); }
  });
  await new Promise(r => srv.listen(0, '127.0.0.1', r));
  const base = `http://127.0.0.1:${srv.address().port}`;
  const S = { session: 'test' };
  try {
    let d;
    try { d = await ops.navigate({ ...S, url: base + '/' }); }
    catch (e) { if (/no Microsoft Edge or Google Chrome/.test(e.message)) { console.log('skip: no browser'); return; } throw e; }
    assert.strictEqual(d.title, 'Todo');
    assert.match(d.snapshot, /heading "Todos"/);
    const ref = /textbox "new todo" \[ref=(e\d+)\]/.exec(d.snapshot)[1];
    const btn = /button "Add" \[ref=(e\d+)\]/.exec(d.snapshot)[1];
    await ops.type({ ...S, ref, text_value: 'buy milk' });
    d = await ops.click({ ...S, ref: btn });
    assert.match(d.snapshot, /listitem.*buy milk/);

    // console + failed request captured
    const c = await ops.console(S);
    assert.ok(c.console.some(x => /boom from page/.test(x.text)), 'console error captured');
    assert.ok(c.network.some(x => /missing\.png/.test(x.url) && x.status === 404), '404 captured');

    // password / card fields and card-like values are refused
    await assert.rejects(ops.type({ ...S, selector: '#pw', text_value: 'hunter2' }), /password/);
    await assert.rejects(ops.type({ ...S, selector: '#cc', text_value: '1234' }), /payment-card/);
    await assert.rejects(ops.type({ ...S, ref, text_value: '4111 1111 1111 1111' }), /card number/);

    // screenshot is a PNG
    const shot = await ops.screenshot(S);
    assert.strictEqual(Buffer.from(shot.png_b64, 'base64').slice(1, 4).toString(), 'PNG');

    // mobile viewport
    d = await ops.navigate({ ...S, url: base + '/', device: 'iphone-14' });
    assert.strictEqual(d.device, 'iphone-14');

    // non-local origins go through the local approval (denied by the stub)
    await assert.rejects(ops.navigate({ ...S, url: 'https://example.com/' }), /denied by local user/);
    assert.ok(asked.some(a => a[0] === 'origin' && a[1] === 'https://example.com'));
    await assert.rejects(ops.navigate({ ...S, url: 'javascript:alert(1)' }), /blocked URL scheme/);

    // page JS is confirmed first
    const ev = await ops.evaluate({ ...S, expression: 'document.title' });
    assert.match(ev.result, /Todo/);
    assert.ok(asked.some(a => a[0] === 'script'));
    console.log('ok - companion browserops');
  } finally {
    await ops.close(S);
    srv.close();
  }
})().catch(e => { console.error(e); process.exit(1); });

// node tests/js/test_session_hold.js - an open stream holds the session, so idle logout never fires mid-run
const assert = require('assert');
const fs = require('fs');
const path = require('path');
const vm = require('vm');

const src = fs.readFileSync(path.join(__dirname, '..', '..', 'static', 'js', 'session.js'), 'utf8');
const timers = [];
const calls = [];
const store = {};
const ctx = {
  window: { fetch: async () => ({ status: 200, ok: true, json: async () => ({}) }) },
  document: { cookie: '', addEventListener() {}, getElementById: () => null, querySelectorAll: () => [], body: { appendChild() {} }, visibilityState: 'visible' },
  localStorage: { getItem: k => store[k] || null, setItem: (k, v) => { store[k] = v; } },
  setInterval: (fn, ms) => { timers.push({ fn, ms }); return timers.length; },
  clearInterval: id => { timers[id - 1] = null; },
  location: { href: '', pathname: '/' }, Headers, console, Date, URLSearchParams, encodeURIComponent, String, Number,
  CustomEvent: function () {},
};
ctx.window.window = ctx.window;
ctx.window.document = ctx.document;
ctx.window.localStorage = ctx.localStorage;
vm.createContext(ctx);
// count server refreshes via the native fetch the script captured
ctx.window.fetch = async (url) => { calls.push(String(url)); return { status: 200, ok: true, json: async () => ({}) }; };
ctx.fetch = ctx.window.fetch;
vm.runInContext(src, ctx);

const hold = ctx.window.holdSession;
assert.strictEqual(typeof hold, 'function');
const before = calls.length;
const release = hold();
assert.ok(calls.length > before, 'holding refreshes the server session straight away');
const t = timers.filter(Boolean).find(x => x.ms === 30000);
assert.ok(t, 'a 30 s refresh timer runs while the stream is open');
const n = calls.length;
t.fn();
assert.ok(calls.length > n, 'the timer refreshes the session');
release(); release();           // releasing twice is harmless
assert.ok(!timers.filter(Boolean).some(x => x.ms === 30000), 'the timer stops when the stream ends');
console.log('session hold tests: OK');

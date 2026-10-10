// node tests/js/test_agent_run_detached.js - the client side of detached runs (static/js/agent-run.js):
// run ids, acknowledging saved results, and reattaching to a run that kept going on the server.
const assert = require('assert');
const fs = require('fs');
const path = require('path');
const vm = require('vm');

const src = fs.readFileSync(path.join(__dirname, '..', '..', 'static', 'js', 'agent-run.js'), 'utf8');
const store = {};
const fetches = [];
const toasts = [];
let runs = [];
const ctx = {
  console, Date, Math, JSON, String, Number, Promise, encodeURIComponent, Array,
  localStorage: { getItem: (k) => (k in store ? store[k] : null), setItem: (k, v) => { store[k] = String(v); } },
  getDeviceHeaders: () => ({ 'X-Device-Id': 'dev1' }),
  toast: (m) => toasts.push(m),
  window: { bgJobs: new Map() },
  curSession: { id: 7 },
  fetch: async (url, opts) => {
    fetches.push({ url: String(url), opts: opts || {} });
    if (String(url).startsWith('/agent/runs')) return { ok: true, json: async () => ({ runs }) };
    return { ok: true, json: async () => ({}) };
  },
};
vm.createContext(ctx);
vm.runInContext(src, ctx);

(async () => {
  // run ids are valid for the server's pattern and different every time
  const a = ctx.newRunId();
  const b = ctx.newRunId();
  assert.match(a, /^[A-Za-z0-9_-]{8,64}$/);
  assert.notStrictEqual(a, b);

  // acknowledging: tells the server and remembers it locally, so a lost ack cannot show a result twice
  assert.strictEqual(ctx.runWasSaved('run-aaa'), false);
  ctx.ackServerRun('run-aaa');
  assert.strictEqual(ctx.runWasSaved('run-aaa'), true);
  const ack = fetches.find((f) => f.url === '/agent/run/run-aaa/ack');
  assert.ok(ack && ack.opts.method === 'POST', 'ack is a POST to the run');
  assert.strictEqual(ack.opts.headers['X-Device-Id'], 'dev1', 'device headers are sent');
  ctx.ackServerRun('');                       // nothing to ack: no request
  assert.strictEqual(fetches.filter((f) => f.url.includes('/ack')).length, 1);

  // the remembered list is bounded
  for (let i = 0; i < 80; i++) ctx.markRunSaved('bulk-' + i);
  assert.ok(JSON.parse(store.a770_saved_runs).length <= 50);
  assert.strictEqual(ctx.runWasSaved('bulk-79'), true);
  assert.strictEqual(ctx.runWasSaved('bulk-0'), false, 'the oldest entries fall off');

  // stopping tells the server, and ids with odd characters are encoded
  ctx.cancelServerRun('run-bbb');
  const cancel = fetches.find((f) => f.url === '/agent/run/run-bbb/cancel');
  assert.ok(cancel && cancel.opts.method === 'POST' && cancel.opts.keepalive === true, 'keepalive so it survives a page unload');
  ctx.cancelServerRun('a/b c');
  assert.ok(fetches.some((f) => f.url === '/agent/run/a%2Fb%20c/cancel'));

  // reattaching: replays the oldest run this browser has not saved yet
  const started = [];
  ctx.runAgentSSE = (text, opts) => { started.push({ text, opts }); };
  runs = [{ id: 'old-saved-1', running: false }, { id: 'live-run-2', running: true }, { id: 'later-run-3', running: false }];
  ctx.markRunSaved('old-saved-1');
  await ctx.reattachDetachedRuns({ id: 7 });
  // objects made inside the vm context have another Object prototype: compare as JSON
  assert.strictEqual(JSON.stringify(started), JSON.stringify([{ text: '', opts: { reattachRunId: 'live-run-2' } }]));
  assert.ok(toasts.some((t) => /kept going/.test(t)), 'the user is told');
  assert.ok(fetches.some((f) => f.url === '/agent/runs?session_id=7'), 'asks for this session only');

  // a finished one is announced differently
  started.length = 0; toasts.length = 0;
  runs = [{ id: 'done-run-9', running: false }];
  await ctx.reattachDetachedRuns({ id: 7 });
  assert.strictEqual(started.length, 1);
  assert.ok(toasts.some((t) => /finished while you were away/.test(t)));

  // nothing to do: runs already saved, nothing listed, a live in-tab job, or the user moved to another session
  started.length = 0;
  runs = [{ id: 'done-run-9', running: false }];
  ctx.markRunSaved('done-run-9');
  await ctx.reattachDetachedRuns({ id: 7 });
  runs = [];
  await ctx.reattachDetachedRuns({ id: 7 });
  runs = [{ id: 'fresh-run-1', running: true }];
  ctx.window.bgJobs.set('7', {});
  await ctx.reattachDetachedRuns({ id: 7 });
  ctx.window.bgJobs.clear();
  ctx.curSession = { id: 8 };
  await ctx.reattachDetachedRuns({ id: 7 });
  assert.strictEqual(started.length, 0);

  // a server error is not fatal
  ctx.curSession = { id: 7 };
  ctx.fetch = async () => ({ ok: false, json: async () => ({}) });
  await ctx.reattachDetachedRuns({ id: 7 });
  ctx.fetch = async () => { throw new Error('offline'); };
  await ctx.reattachDetachedRuns({ id: 7 });
  assert.strictEqual(started.length, 0);

  console.log('agent run detached client tests passed');
})().catch((e) => { console.error(e); process.exit(1); });

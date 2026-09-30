// node tests/js/test_sse_interrupt.js - a run that ends without `done` is an interruption, never "Task completed"
const assert = require('assert');
const fs = require('fs');
const path = require('path');
const vm = require('vm');

const src = fs.readFileSync(path.join(__dirname, '..', '..', 'static', 'js', 'sse-stream.js'), 'utf8');
const ctx = { window: {}, TextDecoder, TextEncoder, console: { warn() {}, log: console.log } };
vm.createContext(ctx);
vm.runInContext(src, ctx);

function fakeResponse(chunks) {
  const enc = new TextEncoder();
  let i = 0;
  return { body: { getReader: () => ({ read: async () => i < chunks.length ? { done: false, value: enc.encode(chunks[i++]) } : { done: true } }) } };
}
const plain = o => JSON.parse(JSON.stringify(o));

(async () => {
  // readSSE reports whether the server ended the stream on purpose
  let r = await ctx.readSSE(fakeResponse(['event: step\ndata: {"step":1}\n\n', 'event: done\ndata: {"state":"completed"}\n\n']), () => {});
  assert.strictEqual(r.terminal, true);
  r = await ctx.readSSE(fakeResponse(['event: tool_call\ndata: {"id":"a","name":"run_python"}\n\n']), () => {});
  assert.strictEqual(r.terminal, false, 'EOF after a tool_call with no done event');
  r = await ctx.readSSE(fakeResponse([': ping\n\n', 'event: error\ndata: {"error":"x"}\n\n']), () => {});
  assert.strictEqual(r.terminal, true);

  // keepalive comments are ignored, not parsed as events
  const seen = [];
  await ctx.readSSE(fakeResponse([': ping\n\n', ': ping\n\n', 'event: run\ndata: {"run_id":"r1"}\n\n']), (ev) => seen.push(ev));
  assert.deepStrictEqual(seen, ['run']);

  // interrupted mid-tool: the open call is closed, the answered one is untouched, and the bubble can offer Continue
  const L = { acts: [
    { type: 'step', step: 1 },
    { type: 'tool_call', id: 'a', name: 'read_file', args: {} },
    { type: 'tool_result', id: 'a', name: 'read_file', ok: true, result: 'fine' },
    { type: 'tool_call', id: 'b', name: 'run_python', args: { code: 'print(1)' } },
  ] };
  ctx.sseInterrupt(L, 'the connection to the server ended before the run finished', 'interrupted');
  const acts = plain(L.acts);
  const results = acts.filter(a => a.type === 'tool_result');
  assert.strictEqual(results.length, 2);
  assert.strictEqual(results[0].result, 'fine');
  assert.strictEqual(results[1].id, 'b');
  assert.strictEqual(results[1].ok, false);
  assert.strictEqual(results[1].interrupted, true);
  assert.ok(/interrupted/.test(results[1].result));
  const stopped = acts.filter(a => a.type === 'stopped');
  assert.strictEqual(stopped.length, 1);
  assert.strictEqual(stopped[0].reason, 'interrupted');
  assert.strictEqual(L.runState, 'interrupted');

  // calling it twice does not stack banners or results
  ctx.sseInterrupt(L, 'again', 'interrupted');
  assert.strictEqual(plain(L.acts).filter(a => a.type === 'stopped').length, 1);
  assert.strictEqual(plain(L.acts).filter(a => a.type === 'tool_result').length, 2);

  // a user Stop is recorded as cancelled
  const L2 = { acts: [{ type: 'tool_call', id: 'c', name: 'run_shell', args: {} }] };
  ctx.sseInterrupt(L2, 'you stopped the run', 'cancelled');
  assert.strictEqual(L2.runState, 'cancelled');
  assert.strictEqual(plain(L2.acts).filter(a => a.type === 'stopped')[0].reason, 'cancelled');

  console.log('sse interrupt tests: OK');
})().catch(e => { console.error(e); process.exit(1); });

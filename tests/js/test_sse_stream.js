// node tests/js/test_sse_stream.js - static/js/sse-stream.js frame parsing
const assert = require('assert');
const fs = require('fs');
const path = require('path');
const vm = require('vm');

const src = fs.readFileSync(path.join(__dirname, '..', '..', 'static', 'js', 'sse-stream.js'), 'utf8');
let active = 0;
const warnings = [];
const ctx = {
  window: { markActive: () => { active++; } },
  TextDecoder, TextEncoder,
  console: { warn: (...a) => warnings.push(a.join(' ')), log: console.log },
};
vm.createContext(ctx);
vm.runInContext(src, ctx);

// A fake fetch Response whose body yields the given chunks (strings or split byte arrays).
function fakeResponse(chunks) {
  const enc = new TextEncoder();
  let i = 0;
  return {
    body: {
      getReader: () => ({
        read: async () => i < chunks.length
          ? { done: false, value: typeof chunks[i] === 'string' ? enc.encode(chunks[i++]) : chunks[i++] }
          : { done: true, value: undefined },
      }),
    },
  };
}

async function collect(chunks) {
  const got = [];
  await ctx.readSSE(fakeResponse(chunks), (ev, d) => got.push([ev, d]));
  return JSON.parse(JSON.stringify(got));   // objects from the vm context have other prototypes
}

(async () => {
  // frames split mid-line and mid-JSON
  let got = await collect(['event: delta\ndata: {"te', 'xt":"he"}\n', '\nevent: delta\ndata: {"text":"llo"}\n\n']);
  assert.deepStrictEqual(got, [['delta', { text: 'he' }], ['delta', { text: 'llo' }]]);

  // CRLF separators
  got = await collect(['event: done\r\ndata: {"ok":true}\r\n\r\n']);
  assert.deepStrictEqual(got, [['done', { ok: true }]]);

  // multi-line data joins with \n
  got = await collect(['event: x\ndata: {"a":\ndata: 1}\n\n']);
  assert.deepStrictEqual(got, [['x', { a: 1 }]]);

  // a malformed frame is skipped, the stream carries on
  got = await collect(['event: delta\ndata: {broken\n\nevent: delta\ndata: {"text":"ok"}\n\n']);
  assert.deepStrictEqual(got, [['delta', { text: 'ok' }]]);
  assert.ok(warnings.some(w => w.includes('malformed')));

  // last frame without the trailing blank line is still delivered
  got = await collect(['event: done\ndata: {"end":1}']);
  assert.deepStrictEqual(got, [['done', { end: 1 }]]);

  // a UTF-8 character split across chunks decodes intact
  const bytes = new TextEncoder().encode('event: delta\ndata: {"text":"৳ টাকা"}\n\n');
  got = await collect([bytes.slice(0, 27), bytes.slice(27)]);
  assert.deepStrictEqual(got, [['delta', { text: '৳ টাকা' }]]);

  // frames without event or data are ignored
  got = await collect([': keepalive\n\nevent: only\n\n']);
  assert.deepStrictEqual(got, []);

  // an exception in the handler ends the read and reaches the caller
  await assert.rejects(
    ctx.readSSE(fakeResponse(['event: error\ndata: {"error":"x"}\n\n']), () => { throw new Error('stop'); }),
    /stop/);

  // splitThink: leading block, bare </think> (forced-open template), literal tag later on
  const plain = o => JSON.parse(JSON.stringify(o));
  assert.deepStrictEqual(plain(ctx.splitThink('<think>r</think>\n\nA')), { reasoning: 'r', content: 'A' });
  assert.deepStrictEqual(plain(ctx.splitThink('plan it\n</think>\n\nA')), { reasoning: 'plan it', content: 'A' });
  assert.deepStrictEqual(plain(ctx.splitThink('no tags')), { reasoning: '', content: 'no tags' });
  const L = { content: 'thinking...\n</think>\nAnswer', reasoning: '' };
  assert.strictEqual(ctx.sseApplyThinkSplit(L), 'thinking...');
  assert.deepStrictEqual(plain(L), { content: 'Answer', reasoning: 'thinking...' });
  const L2 = { content: 'streamed reasoning ', reasoning: 'earlier' };
  assert.strictEqual(ctx.sseDeltaToThought(L2), 'streamed reasoning');
  assert.deepStrictEqual(plain(L2), { content: '', reasoning: 'earlier\n\nstreamed reasoning' });

  assert.ok(active > 0, 'markActive is called per chunk');
  console.log('sse stream tests: OK');
})().catch(e => { console.error(e); process.exit(1); });

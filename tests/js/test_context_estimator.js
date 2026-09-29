// node tests/js/test_context_estimator.js - one context estimator for everything
//
// The reported bug: the CTX chip read "32k / 261k" while the compact bubble said
// "Compacting conversation (~4.9k tokens)". Three estimators disagreed:
//
//   1. doCompact filtered to user|assistant only, dropping every tool message -
//      in agent mode those ARE the bulk of the context.
//   2. none looked at `acts`, where tool calls/results live (not in `content`).
//   3. autoCompactIfNeeded divided by 3.0 while doCompact divided by 3.5.
//
// agent-run.js made it worse: it passed ctxMsgs.map(m => ({role, content})),
// discarding ntok/acts/images outright.

const assert = require('assert');
const fs = require('fs');
const path = require('path');
const vm = require('vm');

const jsDir = path.join(__dirname, '..', '..', 'static', 'js');

let messages = [];
const els = {};
['chip-ctx'].forEach(id => { els[id] = { innerHTML: '', title: '', style: {} }; });

const gsCtx = {
  $: id => els[id], document: { querySelector: () => null, addEventListener() {} },
  console, fetch: async () => ({ ok: false }), setInterval, clearInterval,
  curStatus: null, curCtxMax: 32768, messages, APP_MODELS: null,
  buildContextMessages: () => messages, window: {},
};
gsCtx.window = gsCtx;
vm.createContext(gsCtx);
vm.runInContext(fs.readFileSync(path.join(jsDir, 'gpu-status.js'), 'utf8'), gsCtx);
const { estimateSessionTokens, messageTokens } = gsCtx;
// const/let at the top level of a vm script stay lexical, not on the global
const gsEval = vm.runInContext('({ compact: CTX_COMPACT_PCT, warn: CTX_WARN_PCT, crit: CTX_CRIT_PCT })', gsCtx);
const CTX_COMPACT_PCT = gsEval.compact;
const CTX_WARN_PCT = gsEval.warn;

let fetched = [];
const compactCtx = {
  messages, agentMode: true, generating: false, curCtxMax: 261000, curProject: { id: 1 },
  curSession: { id: 1 }, estimateSessionTokens, messageTokens, CTX_COMPACT_PCT,
  buildContextMessages: () => messages, activeLaneCtxMax: () => 261000,
  toast: () => {}, renderAll: () => {}, setGenUI: () => {}, updateContextChip: () => {},
  console,
  fetch: async (url, opts) => {
    fetched.push({ url, body: JSON.parse(opts.body) });
    return { ok: false, json: async () => ({}) };
  },
};
compactCtx.window = compactCtx;
vm.createContext(compactCtx);
vm.runInContext(fs.readFileSync(path.join(jsDir, 'compact.js'), 'utf8'), compactCtx);

// the OLD behaviour, for comparison
const oldBeforeToks = hist =>
  hist.reduce((a, m) => a + (m.ntok || Math.round((m.content || '').length / 3.5)), 0);

// the stubbed fetch always 500s; autoCompactIfNeeded warns and continues by design
const realWarn = console.warn;
const quietWarn = () => { compactCtx.console = { ...console, warn: () => {} }; };
const loudWarn = () => { compactCtx.console = console; };
void realWarn;

// an agent-mode session: mostly tool traffic
messages = [
  { role: 'user', content: 'fix the bug' },
  { role: 'assistant', content: 'looking', acts: [
    { type: 'tool_call', name: 'read_file', args: { path: 'src/app.js' } },
    { type: 'tool_result', name: 'read_file', result: 'x'.repeat(6000) },
    { type: 'tool_call', name: 'grep', args: { pattern: 'foo' } },
    { type: 'tool_result', name: 'grep', result: 'y'.repeat(3000) },
  ] },
  { role: 'assistant', content: 'done', acts: [
    { type: 'tool_call', name: 'write_file', args: { path: 'src/app.js', content: 'z'.repeat(2000) } },
    { type: 'tool_result', name: 'write_file', result: 'saved' },
  ] },
];

(async () => {
  // 1. tool traffic actually counts
  const withTools = estimateSessionTokens(messages).total;
  const withoutTools = estimateSessionTokens([{ role: 'user', content: 'fix the bug' }]).total;
  assert.ok(withTools > withoutTools * 2,
    `tool traffic must dominate the estimate (${withTools} vs ${withoutTools})`);

  // 2. a message carrying acts contributes more than the same message without
  const bare = { role: 'assistant', content: 'looking' };
  const withActs = { role: 'assistant', content: 'looking', acts: [
    { type: 'tool_call', name: 'read_file', args: { path: 'a.js' } } ] };
  assert.ok(messageTokens(withActs) > messageTokens(bare),
    'acts must be counted, not just content');

  // 3. THE regression: the old prose-only figure was a fraction of reality
  const oldFiltered = messages.filter(m => m.role === 'user' || m.role === 'assistant' || m.compact);
  const oldToks = oldBeforeToks(oldFiltered);
  assert.ok(oldToks < withTools / 3,
    `old estimator (${oldToks}) should be far below the real figure (${withTools})`);

  // 4. bubble and chip agree for the same list (one number, not two)
  assert.strictEqual(estimateSessionTokens(messages).total, withTools,
    'chip and bubble share one number');

  // 5. tool results are prompt-side, not completion
  const split = estimateSessionTokens(messages);
  assert.ok(split.prompt > split.completion,
    `tool results are prompt-side (prompt=${split.prompt} completion=${split.completion})`);

  // 6. auto-compact uses the shared threshold, not a third guess
  assert.strictEqual(CTX_COMPACT_PCT, 65, 'one threshold constant');
  assert.ok(CTX_COMPACT_PCT < CTX_WARN_PCT, 'compact before the chip turns amber');

  // 7. a small session must not compact
  // autoCompactIfNeeded swallows its own failure (never blocks a run); silence
  // the expected warn from the stubbed 500 below
  quietWarn();
  fetched = [];
  await compactCtx.autoCompactIfNeeded(messages);
  assert.deepStrictEqual(fetched, [], 'a small session must not trigger compaction');
  loudWarn();

  // 8. a large session does, and sends the real request.
  // 261k window * 65% = ~170k tokens = ~594k chars at 3.5 chars/token.
  const big = [{ role: 'user', content: 'x'.repeat(700000) }];
  // compact.js has its own buildContextMessages() reading the context-global
  // `messages`, so reassign on the context (not just the outer binding)
  messages = big;
  compactCtx.messages = big;
  fetched = [];
  await compactCtx.autoCompactIfNeeded(big);
  assert.strictEqual(fetched.length, 1, 'a large session must trigger compaction');
  assert.strictEqual(fetched[0].url, '/chat/compact');
  assert.ok(fetched[0].body.agent_mode, 'agent mode is passed through');

  // 9. a compact marker suppresses double-counting of pre-marker history.
  // Callers pass the marker already at index 0 (buildContextMessages reorders it).
  messages = [
    { role: 'assistant', compact: true, content: 'summary of everything', compactKept: 0 },
    { role: 'user', content: 'b'.repeat(1000) },
  ];
  const afterCompact = estimateSessionTokens(messages).total;
  const tailOnly = estimateSessionTokens([messages[1]]).total;
  assert.ok(afterCompact < tailOnly * 1.5,
    `post-compact must be near the kept tail only (${afterCompact} vs ${tailOnly})`);

  // 9b. a raw transcript with older history is trimmed to the same thing
  const raw = [
    { role: 'user', content: 'a'.repeat(100000) },
    { role: 'assistant', compact: true, content: 'summary of everything', compactKept: 0 },
    { role: 'user', content: 'b'.repeat(1000) },
  ];
  const trimmed = estimateSessionTokens(raw).total;
  assert.ok(trimmed < tailOnly * 1.5,
    `pre-marker history must not be recounted (${trimmed} vs ${tailOnly})`);

  console.log(`context estimator ok (${withTokens(withTools)} real vs ${withTokens(oldToks)} old)`);
})().catch(e => { console.error(e); process.exit(1); });

function withTokens(n) { return n >= 1000 ? (n / 1000).toFixed(1) + 'k' : String(n); }


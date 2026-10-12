// node tests/js/test_dock_live.js - live document preview in the right dock (deck / PDF built in pieces)
//
// Covers: sse-stream.js turns doc_create / doc_append results into RightDock.showLiveDoc calls (only common-space
// files that carry a [DOWNLOAD: name] tag, pptx/pdf only, never on failure), and claude-dock.js opens the Preview
// tab ONCE, then only re-renders the body (throttled), following the newest slide.
const assert = require('assert');
const fs = require('fs');
const path = require('path');
const vm = require('vm');

const jsDir = path.join(__dirname, '..', '..', 'static', 'js');
const read = f => fs.readFileSync(path.join(jsDir, f), 'utf8');

// ---------- 1. sse-stream.js: which tool results reach the dock ----------
const calls = [];
const sseCtx = {
  window: { RightDock: { showLiveDoc: (...a) => calls.push(JSON.parse(JSON.stringify(a))) } },
  TextDecoder, TextEncoder, console,
};
vm.createContext(sseCtx);
vm.runInContext(read('sse-stream.js'), sseCtx);

function result(name, res, extra = {}) {
  sseCtx.sseToolResult({ acts: [] }, { id: '1', name, ok: true, result: res, args: { file: 'x.pptx' }, ...extra });
}

result('doc_create', "Created deck-1a2b3c4d.pptx. It has 2 of 6: add the rest with doc_append(file='deck-1a2b3c4d.pptx', ...) [DOWNLOAD: deck-1a2b3c4d.pptx]");
assert.deepStrictEqual(calls.pop(), ['deck-1a2b3c4d.pptx', 'pptx', { done: false }], 'a build in progress is not done');

result('doc_append', 'Added to deck-1a2b3c4d.pptx: now 5 slides of 6. Continue [DOWNLOAD: deck-1a2b3c4d.pptx]');
assert.deepStrictEqual(calls.pop(), ['deck-1a2b3c4d.pptx', 'pptx', { done: false }]);

result('doc_append', 'Added to deck-1a2b3c4d.pptx: now 6 slides of 6. Finished. [DOWNLOAD: deck-1a2b3c4d.pptx]');
assert.deepStrictEqual(calls.pop(), ['deck-1a2b3c4d.pptx', 'pptx', { done: true }]);

result('doc_create', 'Created one-shot.pptx. [DOWNLOAD: one-shot.pptx]');
assert.deepStrictEqual(calls.pop(), ['one-shot.pptx', 'pptx', { done: true }], 'a document made in one call is final');

result('write_file', 'Saved on your device: C:\\proj\\deck.pptx');
assert.strictEqual(calls.length, 0, 'device files have no [DOWNLOAD] tag: no live preview');

result('doc_append', 'doc error: not finished [DOWNLOAD: deck.pptx]', { ok: false });
result('doc_create', 'Created data.xlsx [DOWNLOAD: data.xlsx]');
result('run_shell', '[DOWNLOAD: deck.pptx]');
assert.strictEqual(calls.length, 0, 'failures, other formats and other tools are ignored');

// ---------- 2. claude-dock.js: open once, then refresh in place ----------
const timers = [];
const els = {};
const mkEl = id => {
  const e = {
    id, innerHTML: '', textContent: '', style: {}, classList: { add() {}, remove() {}, toggle() {}, contains: () => false },
    querySelector(sel) {
      if (sel === '.pptx-deck') return this.deck || null;
      if (sel === 'iframe') return this.frame || null;
      return null;
    },
    addEventListener() {}, setAttribute() {}, appendChild() {},
  };
  return e;
};
['dock-view-preview'].forEach(id => { els[id] = mkEl(id); });
const fetched = [];
let renders = 0;
const dockCtx = {
  $: id => els[id] || null,
  esc: s => String(s),
  fetch: async url => { fetched.push(url); return { ok: true, status: 200, json: async () => ({ slides: [1, 2, 3] }) }; },
  renderSlidesPreview: (deck, body) => {
    renders++;
    els['live-doc-body'].deck = { scrollTop: 0, scrollHeight: 1000, clientHeight: 400 };
    els['live-doc-status'] = els['live-doc-status'] || mkEl('live-doc-status');
  },
  document: { addEventListener() {}, getElementById: () => null, querySelectorAll: () => [], createElement: () => mkEl('tmp') },
  setTimeout: (fn, ms) => { timers.push({ fn, ms }); return timers.length; },
  clearTimeout() {},
  Date, console, encodeURIComponent, Promise,
  localStorage: { getItem: () => null, setItem() {} },
};
dockCtx.window = dockCtx;
dockCtx.navigator = {};
vm.createContext(dockCtx);
vm.runInContext(read('claude-dock.js'), dockCtx);
const dock = dockCtx.window.RightDock;
assert.ok(dock && typeof dock.showLiveDoc === 'function', 'RightDock.showLiveDoc exists');

let opened = 0;
dock.previewView = els['dock-view-preview'];
dock.open = () => { opened++; };
els['live-doc-body'] = mkEl('live-doc-body');          // what the skeleton's innerHTML would have created

const flush = () => new Promise(r => setImmediate(r));
(async () => {
  dock.showLiveDoc('deck.pptx', 'pptx', { done: false });
  assert.strictEqual(opened, 1, 'first call opens the Preview tab');
  assert.match(els['dock-view-preview'].innerHTML, /live-doc-body/);
  assert.strictEqual(timers.length, 1);
  timers.shift().fn();
  await flush();
  assert.match(fetched[0], /^\/agent\/slides\?path=deck\.pptx&space=common$/);
  assert.strictEqual(renders, 1);

  dock.showLiveDoc('deck.pptx', 'pptx', { done: false });
  assert.strictEqual(opened, 1, 'later steps never re-open or steal the tab');
  assert.strictEqual(timers.length, 1, 'a refresh is queued');
  assert.ok(timers[0].ms > 0, 'and throttled');
  timers.shift().fn();
  await flush();
  assert.strictEqual(renders, 2);

  dock.showLiveDoc('notes.txt', 'txt');
  assert.strictEqual(timers.length, 0, 'only pptx / pdf are shown live');
  console.log('ok test_dock_live');
})().catch(e => { console.error(e); process.exit(1); });

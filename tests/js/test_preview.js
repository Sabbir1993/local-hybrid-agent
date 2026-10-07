// node tests/js/test_preview.js - which endpoint a preview reads from
//
// The reported bug: 👁️ Preview on an agent tool card showed
// {"error":"file not found: src/shop.js"} for every project file, because the
// card used /agent/raw - which only serves the server-side common space - while
// project files live on the user's own machine behind /agent/ws/raw.
//
// Covers: source routing, the one-shot common-space fallback, and the
// delegation from utils.js (data-preview-source -> opts.source).

const assert = require('assert');
const fs = require('fs');
const path = require('path');
const vm = require('vm');

const jsDir = path.join(__dirname, '..', '..', 'static', 'js');
const previewSrc = fs.readFileSync(path.join(jsDir, 'preview.js'), 'utf8');

let fetched = [];

function el() {
  const e = {
    _text: '', style: {}, dataset: {}, innerHTML: '', textContent: '', title: '',
    querySelectorAll: () => [], appendChild() {}, closest: () => null,
    setAttribute() {}, addEventListener() {},
  };
  Object.defineProperty(e, 'textContent', {
    get() { return this._text; }, set(v) { this._text = v; },
  });
  return e;
}

const els = {};
['preview-modal', 'preview-title', 'preview-icon', 'preview-content', 'preview-controls',
 'preview-dl', 'preview-raw-link'].forEach(id => { els[id] = el(); });

const ctx = {
  $: (id) => els[id],
  esc: (s) => String(s),
  fetch: async (url) => {
    fetched.push(url);
    return { ok: false, status: 404, statusText: 'NF', json: async () => ({ error: 'nope' }) };
  },
  renderCodePreview() {}, renderMarkdownModalPreview() {},
  renderHtmlPreview() {}, renderCsvPreview() {},
  renderMermaidModalPreview() {}, renderExcelPreview() {},
  renderPdfPreview() {}, renderImagePreview() {}, renderSlidesPreview() {},
  console, setTimeout, URL, Math, Date,
};
ctx.window = ctx;
vm.createContext(ctx);
vm.runInContext(previewSrc, ctx);

(async () => {
  // --- default (common space) never touches the workspace endpoint --------
  fetched = []; lastRendered = null;
  await ctx.openFilePreview('report.txt');
  assert.ok(fetched.every(u => u.startsWith('/agent/raw')),
            'a common-space preview must not hit /agent/ws/raw: ' + fetched);

  // --- source:'ws' reads the workspace endpoint ----------------------------
  fetched = [];
  await ctx.openFilePreview('src/shop.js', 'shop.js', null, { source: 'ws' });
  assert.ok(fetched[0].startsWith('/agent/ws/raw'),
            'a workspace preview must read /agent/ws/raw, got ' + fetched[0]);
  assert.ok(fetched[0].includes(encodeURIComponent('src/shop.js')),
            'the path must be encoded: ' + fetched[0]);

  // --- a 404 in workspace space falls back to common space, exactly once ----
  fetched = [];
  await ctx.openFilePreview('src/shop.js', 'shop.js', null, { source: 'ws' });
  const wsHits = fetched.filter(u => u.startsWith('/agent/ws/raw'));
  const commonHits = fetched.filter(u => u.startsWith('/agent/raw'));
  assert.strictEqual(wsHits.length, 1, 'must not retry the workspace endpoint');
  assert.strictEqual(commonHits.length, 1, 'must try common space exactly once');

  // --- directContent short-circuits any fetch -----------------------------
  // note: preview.js defines its own renderers internally, so the real ones run
  // against the stub elements; this test is about WHICH endpoint is requested.
  fetched = [];
  await ctx.openFilePreview('snippet.js', 'snippet.js', 'const a = 1;', { source: 'ws' });
  assert.deepStrictEqual(fetched, [], 'supplied content must not trigger a fetch');

  // --- the path is echoed into the raw link for source:'ws' ---------------
  fetched = [];
  await ctx.openFilePreview('a/b.md', 'b.md', null, { source: 'ws' });
  assert.ok(els['preview-raw-link'].href.startsWith('/agent/ws/raw'),
            'the Raw link must point at the same space: ' + els['preview-raw-link'].href);

  // --- utils.js passes data-preview-source through -------------------------
  const utilsSrc = fs.readFileSync(path.join(jsDir, 'utils.js'), 'utf8');
  assert.ok(/openFilePreview\([\s\S]*?previewSource/.test(utilsSrc),
            'utils.js must forward dataset.previewSource into opts.source');
  assert.ok(/openFilePreview\([\s\S]*?source: 'ws'/.test(
              fs.readFileSync(path.join(jsDir, 'sse-stream.js'), 'utf8')),
            'the save/update HUD action must preview from the workspace');

  // --- agent-acts.js marks tool-card previews as workspace paths -----------
  const acts = fs.readFileSync(path.join(jsDir, 'agent-acts.js'), 'utf8');
  const cards = acts.match(/data-preview-path="\$\{esc\(p\)\}"[^>]*/g) || [];
  assert.ok(cards.length >= 2, 'expected both tool-card preview buttons');
  cards.forEach(c => assert.ok(c.includes('data-preview-source="ws"'),
    'a tool-card preview button must declare its source: ' + c));

  // --- PDF / HTML / image previews: a server-generated file lives in the common space -------------
  // (the reported bug: a PDF the server wrote showed "File Not Found On Disk" from an agent tool card,
  // because the card said source:'ws' and the PDF viewer used that URL with no fallback)
  const realFetch = ctx.fetch;
  const serve = (okPrefix) => async (url) => {
    fetched.push(url);
    const ok = url.startsWith(okPrefix);
    return { ok, status: ok ? 200 : 404, headers: { get: () => 'application/pdf' }, json: async () => ({ error: 'nope' }) };
  };

  ctx.fetch = serve('/agent/raw');                         // only the common space has the file
  fetched = [];
  await ctx.openFilePreview('Report-eacd3c32.pdf', 'Report.pdf', null, { source: 'ws' });
  assert.ok(fetched[0].startsWith('/agent/ws/raw'), 'workspace is tried first: ' + fetched);
  assert.ok(fetched[fetched.length - 1].startsWith('/agent/raw'), 'the PDF viewer ends up on the common space: ' + fetched);
  assert.strictEqual(fetched.filter(u => u.startsWith('/agent/ws/raw')).length, 1, 'workspace is probed only once');
  assert.ok(els['preview-raw-link'].href.startsWith('/agent/raw'), 'the "New Tab" link follows the working URL');

  ctx.fetch = serve('/agent/ws/raw');                      // a real project PDF on the user\'s machine
  fetched = [];
  await ctx.openFilePreview('docs/spec.pdf', 'spec.pdf', null, { source: 'ws' });
  assert.ok(fetched.every(u => u.startsWith('/agent/ws/raw')), 'a workspace PDF never touches the common space: ' + fetched);

  ctx.fetch = serve('/agent/raw');                         // plain common-space preview: no probing at all
  fetched = [];
  await ctx.openFilePreview('Report-eacd3c32.pdf', 'Report.pdf');
  assert.deepStrictEqual(fetched.map(u => u.split('?')[0]), ['/agent/raw'], 'one request, straight to the common space');

  ctx.fetch = serve('/agent/raw');                         // images use the same fallback
  fetched = [];
  let imgUrl = null;
  const realImg = ctx.renderImagePreview;
  ctx.renderImagePreview = (u) => { imgUrl = u; };         // which URL the image viewer is given
  await ctx.openFilePreview('chart-1a2b3c4d.png', 'chart.png', null, { source: 'ws' });
  ctx.renderImagePreview = realImg;
  assert.ok(fetched[0].startsWith('/agent/ws/raw'), 'image: workspace probed first: ' + fetched);
  assert.ok(imgUrl && imgUrl.startsWith('/agent/raw?path='), 'image: the viewer is given the common-space URL after the miss: ' + imgUrl);
  ctx.fetch = realFetch;

  console.log('preview routing ok (' + cards.length + ' tool-card buttons)');
})().catch(e => { console.error(e); process.exit(1); });

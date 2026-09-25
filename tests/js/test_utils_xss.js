// tests/js/test_utils_xss.js - md() / renderMediaPreviewSection() never emit
// model-controlled text into inline JS or unescaped attributes.
// Run: node tests/js/test_utils_xss.js
const fs = require('fs');
const path = require('path');
const vm = require('vm');
const assert = require('assert');

const ctx = {
  document: { addEventListener() {} },
  hlLangFor: () => '', hlCode: (c) => c, console, window: {},
};
vm.createContext(ctx);
vm.runInContext(fs.readFileSync(path.join(__dirname, '../../static/js/utils.js'), 'utf8'), ctx);

const payloads = [
  "![x](https://a.example/p.png');alert(1);//)",
  '![x](https://a.example/"onerror=alert(1)//.png)',
  "[DOWNLOAD: a');alert(1);//.csv]",
  "[f](/agent/download?path=a%27);alert(1);//.csv)",
  '![x](/agent/raw?path=a"onerror=alert(1)//.png)',
];

for (const p of payloads) {
  for (const [name, fn] of [['md', ctx.md], ['media', ctx.renderMediaPreviewSection]]) {
    const html = fn(p);
    assert(!/onclick=/i.test(html), `${name}: inline onclick for ${p}\n${html}`);
    // no attribute may be terminated early: a raw " inside attribute values
    // would show up as an unexpected on*= attribute
    assert(!/\son(error|load|click)=(?!"this\.closest)/i.test(html), `${name}: event handler injected for ${p}\n${html}`);
  }
}

// external images are click-to-load, local ones render directly
const ext = ctx.md('![chart](https://cdn.example/c.png)');
assert(/data-load-src="https:\/\/cdn\.example\/c\.png"/.test(ext), ext);
assert(!/<img/.test(ext), 'external image must not auto-load: ' + ext);
const loc = ctx.md('![chart](/agent/raw?path=c.png)');
assert(/<img src="\/agent\/raw\?path=c\.png"/.test(loc), loc);
// only the image viewer opens it (not the file preview too)
assert(!/data-preview-path/.test(loc), loc);
// one preview: no download chip for a file already shown as an image
const once = ctx.md('![cat](/agent/raw?path=generated/x.png)\n\n[DOWNLOAD: generated/x.png]\n\n[DOWNLOAD: a.pdf]');
assert((once.match(/file-action-badge primary/g) || []).length === 1 && /a\.pdf/.test(once), once);

// esc() tolerates non-strings (API fields may be missing or numeric)
assert.strictEqual(ctx.esc(undefined), '');
assert.strictEqual(ctx.esc(3), '3');

// toast() renders its message as text: filenames/titles/remote errors are untrusted
{
  const made = [];
  const el = () => { const e = { children: [], appendChild(c) { this.children.push(c); }, classList: { add() {}, remove() {} } }; made.push(e); return e; };
  const toastEl = el();
  ctx.$ = (id) => (id === 'toast' ? toastEl : null);
  ctx.document.createElement = el;
  ctx.setTimeout = () => 0; ctx.clearTimeout = () => {};
  try { ctx.toast('<img src=x onerror=alert(1)>.csv'); } catch (e) { /* later DOM calls are not stubbed */ }
  const span = toastEl.children[0];
  assert(span && span.textContent === '<img src=x onerror=alert(1)>.csv', 'toast must use textContent');
  assert(!span.innerHTML, 'toast must not set innerHTML');
}

// login.js safeNext(): only same-origin paths survive
{
  const src = fs.readFileSync(path.join(__dirname, '../../static/js/login.js'), 'utf8');
  const fnSrc = src.match(/function safeNext\(next\) \{[\s\S]*?\n\}/)[0];
  const lctx = { URL, location: { origin: 'https://app.example' } };
  vm.createContext(lctx);
  vm.runInContext(fnSrc, lctx);
  for (const bad of ['javascript:alert(1)', '//evil.example', '/\\evil.example', 'https://evil.example/', '', null])
    assert.strictEqual(lctx.safeNext(bad), '/', `next=${bad}`);
  assert.strictEqual(lctx.safeNext('/settings?tab=db#x'), '/settings?tab=db#x');
}

console.log('utils XSS tests: OK');

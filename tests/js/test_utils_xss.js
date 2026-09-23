// tests/js/test_utils_xss.js - md() / renderMediaPreviewSection() never emit
// model-controlled text into inline JS or unescaped attributes.
// Run: node tests/js/test_utils_xss.js
const fs = require('fs');
const path = require('path');
const vm = require('vm');
const assert = require('assert');

const ctx = {
  document: { addEventListener() {} },
  hlLangFor: () => '', hlCode: (c) => c, console,
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
assert(/data-preview-path="\/agent\/raw\?path=c\.png"/.test(loc), loc);

console.log('utils XSS tests: OK');

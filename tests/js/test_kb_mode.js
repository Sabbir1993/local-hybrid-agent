// node tests/js/test_kb_mode.js - composer switch for the company knowledge base: Auto -> Always -> Off
const assert = require('assert');
const fs = require('fs');
const path = require('path');
const vm = require('vm');

const src = fs.readFileSync(path.join(__dirname, '..', '..', 'static', 'js', 'kb-mode.js'), 'utf8');
const store = {};
let clickHandler = null;
const tag = { textContent: '' };
const btn = {
  dataset: {}, classList: { toggle(c, on) { btn.cls = on ? c : ''; } }, title: '', attrs: {},
  querySelector: () => tag, setAttribute(k, v) { this.attrs[k] = v; }, addEventListener(e, fn) { if (e === 'click') clickHandler = fn; },
};
const toasts = [];
const ctx = {
  document: { getElementById: id => (id === 'btn-kb-toggle' ? btn : null) },
  localStorage: { getItem: k => (k in store ? store[k] : null), setItem: (k, v) => { store[k] = v; } },
  toast: m => toasts.push(m),
};
ctx.window = ctx;
vm.createContext(ctx);
vm.runInContext(src, ctx);

assert.strictEqual(ctx.chatKbMode(), 'auto', 'default is auto');
assert.strictEqual(btn.dataset.mode, 'auto');
assert.strictEqual(tag.textContent, '', 'auto is the quiet state');
clickHandler();
assert.strictEqual(ctx.chatKbMode(), 'on');
assert.strictEqual(btn.cls, 'active');
assert.strictEqual(tag.textContent, 'on');
clickHandler();
assert.strictEqual(ctx.chatKbMode(), 'off');
assert.strictEqual(tag.textContent, 'off');
assert.match(btn.title, /never searched/);
clickHandler();
assert.strictEqual(ctx.chatKbMode(), 'auto');
assert.strictEqual(store.chat_kb_mode, 'auto', 'remembered in the browser');
assert.strictEqual(toasts.length, 3);
store.chat_kb_mode = 'garbage';
assert.strictEqual(ctx.chatKbMode(), 'auto', 'an unknown stored value falls back to auto');
console.log('ok test_kb_mode');

// node tests/js/test_status_refresh.js - pollStatus keeps the model picker and the context ring in step with the
// loaded model even when a conversation is open (no welcome card on the page).
const assert = require('assert');
const fs = require('fs');
const path = require('path');
const vm = require('vm');

const src = fs.readFileSync(path.join(__dirname, '..', '..', 'static', 'js', 'gpu-status.js'), 'utf8');
const calls = { picker: 0, chip: 0, profiles: 0 };
const els = {};
const el = id => els[id] || (els[id] = { id, style: {}, classList: { add() {}, remove() {} }, textContent: '', checked: false });
let status = { pid: 0, model: '', context: { n_ctx: 32768 } };
const ctx = {
  $: id => (id === 'empty-model' ? null : el(id)),           // a chat is open: no welcome card
  fetch: async () => ({ ok: true, json: async () => status }),
  document: { querySelector: () => null, querySelectorAll: () => [] },
  renderModelPicker: () => { calls.picker++; },
  loadProfiles: () => { calls.profiles++; },
  setLoadBtn() {}, fmtUptime: () => '0s', esc: s => s, console, setTimeout, Math, Number, String,
};
ctx.window = ctx;
vm.createContext(ctx);
vm.runInContext(src, ctx);
// the script reads these page globals; updateContextChip is replaced so the test sees the call
vm.runInContext('var curStatus = null; var _chipCalls = 0; updateContextChip = function () { _chipCalls++; };', ctx);

(async () => {
  await ctx.pollStatus();                                       // first poll: model unloaded
  status = { pid: 4242, model: 'E:/m/Qwen.gguf', context: { n_ctx: 65536 }, profile: '' };
  await ctx.pollStatus();                                       // a model finished loading
  assert.ok(calls.picker >= 2, 'model picker re-rendered on every poll, welcome card or not');
  assert.ok(calls.profiles >= 1, 'load / unload transition reloads the profile list');
  assert.strictEqual(vm.runInContext('_chipCalls', ctx), 2, 'context ring refreshed on every poll');
  assert.strictEqual(vm.runInContext('curCtxMax', ctx), 65536, 'window size follows the loaded model');
  console.log('ok test_status_refresh');
})().catch(e => { console.error(e); process.exit(1); });

// node tests/js/test_session_filter.js - the sidebar search icon filters the current session list by title
const assert = require('assert');
const fs = require('fs');
const path = require('path');
const vm = require('vm');

const src = fs.readFileSync(path.join(__dirname, '..', '..', 'static', 'js', 'projects.js'), 'utf8');

function el(id) {
  const e = { id, children: [], dataset: {}, style: {}, hidden: false, value: '', placeholder: '', textContent: '', className: '',
    attrs: {}, listeners: {},
    setAttribute(k, v) { this.attrs[k] = v; }, getAttribute(k) { return this.attrs[k]; },
    addEventListener(t, f) { this.listeners[t] = f; }, focus() { this.focused = true; },
    appendChild(c) { this.children.push(c); return c; }, insertBefore(c) { this.children.push(c); return c; },
    remove() {}, querySelector() { return null; }, querySelectorAll() { return []; } };
  Object.defineProperty(e, 'innerHTML', { get() { return this._html || ''; }, set(v) { this._html = v; this.children = []; } });
  return e;
}
const els = {};
const $ = id => (els[id] = els[id] || el(id));
const sandbox = {
  window: { appMode: 'chat', addEventListener() {}, removeEventListener() {}, fetch: () => Promise.resolve({ json: () => ({}) }) }, document: { readyState: 'complete', addEventListener() {}, createElement: () => el('x'),
    querySelector: () => null, querySelectorAll: () => [] },
  $, console, localStorage: { getItem: () => null, setItem() {} }, setTimeout, clearTimeout, esc: s => String(s),
  toast() {}, agentMode: false, curProject: null, curSession: null, getDeviceHeaders: () => ({}),
  IntersectionObserver: undefined,
};
vm.createContext(sandbox);
try { vm.runInContext(src, sandbox); } catch (e) { console.error('load failed:', e.message); process.exit(1); }

const run = code => vm.runInContext(code, sandbox);
run(`_sess.items = [{id:1,title:'Quarterly report'},{id:2,title:'Fix login bug'},{id:3,title:'Report draft v2'}]; _sess.hasMore = false;`);

const list = $('session-list');
const titles = () => list.children.filter(c => c.className.includes('session-row')).length;

// opening: the bar shows, the input is focused, and the composer is untouched
$('session-filter-bar').hidden = true;      // the markup ships it hidden
const composer = $('input');
composer.value = '';
sandbox.window.toggleSessionFilter();
assert.strictEqual($('session-filter-bar').hidden, false, 'the bar opens');
assert.ok($('session-filter-input').focused, 'the filter input is focused');
assert.strictEqual(composer.value, '', 'no "/" is typed into the composer');
assert.strictEqual($('session-filter-input').placeholder, 'Filter chats…');

// filtering by title, case-insensitive
run(`_sessFilter.text = 'report'`);
run(`applySessionFilter()`).then(() => {
  assert.strictEqual(titles(), 2, 'two titles contain "report"');
  assert.strictEqual($('session-filter-count').textContent, '2/3');
  run(`_sessFilter.text = 'zzz'`);
  return run(`applySessionFilter()`);
}).then(() => {
  assert.strictEqual(titles(), 0);
  assert.ok(/No chats match "zzz"/.test(list.children[0].textContent), 'an empty result says so');

  // the placeholder follows the mode
  sandbox.window.appMode = 'agent';
  run(`renderSessionRows($('session-list'))`);
  assert.strictEqual($('session-filter-input').placeholder, 'Filter agent sessions…');
  sandbox.window.appMode = 'code';
  run(`renderSessionRows($('session-list'))`);
  assert.strictEqual($('session-filter-input').placeholder, 'Filter code sessions…');

  // closing clears the text and shows everything again
  sandbox.window.toggleSessionFilter();
  assert.strictEqual($('session-filter-bar').hidden, true);
  assert.strictEqual(run('_sessFilter.text'), '');
  return new Promise(r => setTimeout(r, 20));
}).then(() => {
  assert.strictEqual(titles(), 3, 'closing the filter restores the full list');
  console.log('session filter tests: OK');
}).catch(e => { console.error(e); process.exit(1); });

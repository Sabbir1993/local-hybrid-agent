// node tests/js/test_usage_report_tabs.js - usage report: one tab per section + the By user tab
const assert = require('assert');
const fs = require('fs');
const path = require('path');
const vm = require('vm');

const src = fs.readFileSync(path.join(__dirname, '..', '..', 'static', 'js', 'usage-report.js'), 'utf8');
const els = {};
const el = id => els[id] || (els[id] = { id, innerHTML: '', style: {}, hidden: false, dataset: {}, classList: { toggle() {} }, setAttribute() {},
  querySelectorAll: () => [], addEventListener() {} });
const fetched = [];
const ctx = {
  $: el, esc: s => String(s == null ? '' : s).replace(/&/g, '&amp;').replace(/</g, '&lt;'),
  document: { querySelectorAll: () => [] }, console, URLSearchParams, Date, Math,
  curStatus: null, toast() {}, navigator: {},
  fetch: async url => { fetched.push(url); return { ok: true, status: 200, json: async () => ({ users: USERS }) }; },
};
ctx.window = ctx;
const B = String.fromCharCode(92);
const USERS = [
  { user_id: 1, name: 'Sabbir H', username: 'sabbir', email: 's@x.com', requests: 100, runs: 7, prompt_tokens: 9000, prompt_cached_tokens: 4500,
    completion_tokens: 1000, total_tokens: 10000, cloud_tokens: 2500, last_active: 1791829000, top_model: 'E:' + B + 'm' + B + 'big-model.gguf' },
  { user_id: 2, name: 'Rahim', username: 'rahim', email: 'r@x.com', requests: 5, runs: 1, prompt_tokens: 900, prompt_cached_tokens: 0,
    completion_tokens: 100, total_tokens: 1000, cloud_tokens: 0, last_active: 1791829100, top_model: 'small' },
  { user_id: null, name: 'Unattributed', requests: 50, runs: 0, prompt_tokens: 400, prompt_cached_tokens: 0, completion_tokens: 0,
    total_tokens: 400, cloud_tokens: 0, last_active: 1791820000, top_model: 'small' },
];
vm.createContext(ctx);
vm.runInContext(src + '\nthis.__t = { repTabsHtml, repCardsHtml, repRenderUsers, repApplyTab, REP, REP_TABS, setUsers: u => { _repUsers = u; }, setTab: t => { REP_TAB = t; } };', ctx);
const T = ctx.__t;

// one tab per section, including By user
const tabs = T.repTabsHtml();
assert.strictEqual(T.REP_TABS.map(t => t[0]).join(','), 'summary,runs,model,day,user');
for (const [id, label] of T.REP_TABS) assert.ok(tabs.includes(`data-tab="${id}"`) && tabs.includes(label), id);
assert.match(tabs, /class="rep-tab on"[^>]*data-tab="summary"/, 'Summary is the first, active tab');

// each section is its own panel; only the active tab's panel is visible
T.setTab('model');
const panels = T.repCardsHtml({});
for (const id of ['runs', 'model', 'day', 'user']) assert.ok(panels.includes(`data-panel="${id}"`), 'panel ' + id);
assert.ok(/data-panel="runs" hidden/.test(panels) && !/data-panel="model" hidden/.test(panels));

// the period buttons belong to Summary only
T.setTab('summary'); T.repApplyTab();
assert.strictEqual(el('rep-days').style.display, 'flex');
T.setTab('user'); T.repApplyTab();
assert.strictEqual(el('rep-days').style.display, 'none');

// By user: rows, share, unattributed, search, sort
T.setUsers(USERS);
T.repRenderUsers();
let html = el('rep-user-out').innerHTML;
assert.ok(html.includes('Sabbir H') && html.includes('Rahim') && html.includes('Unattributed'));
assert.ok(html.indexOf('Sabbir H') < html.indexOf('Rahim'), 'most tokens first');
assert.ok(html.includes('>big-model.gguf</td>'), 'top model cell shows the file name only (full path stays in the tooltip)');
assert.match(html, /88%|87%/, 'Sabbir has ~87.7% of 11,400 tokens');
assert.ok(html.includes('3 users'));
T.REP.user.q = 'rah'; T.repRenderUsers();
html = el('rep-user-out').innerHTML;
assert.ok(html.includes('Rahim') && !html.includes('Sabbir H'), 'search by name');
T.REP.user.q = ''; T.REP.user.sort = 'name'; T.repRenderUsers();
html = el('rep-user-out').innerHTML;
assert.ok(html.indexOf('Rahim') < html.indexOf('Sabbir H'), 'name A-Z');
T.setUsers({ error: 'not allowed' }); T.repRenderUsers();
assert.ok(el('rep-user-out').innerHTML.includes('not allowed'));
console.log('ok test_usage_report_tabs');

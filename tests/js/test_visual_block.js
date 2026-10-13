// tests/js/test_visual_block.js - ```visual fences (static/js/markdown.js parseVisual/renderVisualHtml, wired in md()).
// Node has no DOM; every value is built from escaped strings and whitelisted classes, which is what is asserted here.
const fs = require('fs');
const vm = require('vm');
const ok = (c, m) => { if (!c) { console.error('FAIL', m); process.exitCode = 1; } else console.log('ok', m); };

const ctx = { console, document: { addEventListener() {} }, window: {}, hlLangFor: () => '', hlCode: c => c, module: { exports: {} } };
ctx.exports = ctx.module.exports;
vm.createContext(ctx);
vm.runInContext(fs.readFileSync(__dirname + '/../../static/vendor/marked.umd.js', 'utf8'), ctx);
ctx.marked = ctx.module.exports;
ctx.DOMPurify = { sanitize: html => html };
ctx.module = undefined;
vm.runInContext(fs.readFileSync(__dirname + '/../../static/js/utils.js', 'utf8'), ctx);
vm.runInContext(fs.readFileSync(__dirname + '/../../static/js/markdown.js', 'utf8'), ctx);
const md = (s, o) => ctx.md(s, o);
const count = (h, re) => (h.match(re) || []).length;
const fence = (obj) => 'Intro\n```visual\n' + (typeof obj === 'string' ? obj : JSON.stringify(obj)) + '\n```\nAfter';

const flow = { type: 'flow', title: 'How Caddy works', steps: [
  { title: 'Public internet', text: 'User visits `https://ai.example.com`', icon: 'globe', tone: 'blue', note: 'DNS resolves' },
  { title: 'Caddy', icon: 'shield', tone: 'purple', note: 'Forwards' },
  { title: 'Local app', icon: 'server', tone: 'green' }] };
let h = md(fence(flow));
ok(count(h, /class="vis-card /g) === 3, 'flow: one card per step');
ok(count(h, /class="vis-arrow"/g) === 2, 'flow: an arrow between cards, none after the last');
ok(h.includes('vis-tone-purple') && h.includes('<svg'), 'tone class and inline svg icon are drawn');
ok(h.includes('<code>https://ai.example.com</code>'), 'backtick spans inside card text become <code>');
ok(h.includes('DNS resolves') && h.includes('Intro') && h.includes('After'), 'note caption and surrounding prose are kept');
ok(!h.includes('code-block'), 'a valid block is not shown as a code block');

h = md(fence({ type: 'cards', items: [{ title: 'A' }, { title: 'B', tone: 'red' }] }));
ok(count(h, /class="vis-card /g) === 2 && h.includes('vis-grid') && !h.includes('vis-arrow'), 'cards: a grid, no arrows');

// hostile values never reach the markup
h = md(fence({ type: 'flow', steps: [
  { title: '<img src=x onerror=alert(1)>', text: '<script>alert(1)</script>', icon: '"><script>', tone: 'x" onclick="y',
    note: '<b onmouseover=1>n</b>' }, { title: 'second', style: 'color:red' }] }));
ok(!/<img|<script|<b |onerror=|onclick=|onmouseover=/i.test(h.replace(/&lt;[^&]*&gt;/g, '')), 'model text is escaped, no live tags or handlers');
ok(!h.includes('style='), 'no style attribute is ever written');
ok(!/vis-tone-x/.test(h) && !h.includes('"><script>'), 'unknown icon/tone are dropped, never echoed into classes');

// limits and fallbacks
const many = { type: 'flow', steps: Array.from({ length: 30 }, (_, i) => ({ title: 'S' + i })) };
ok(count(md(fence(many)), /class="vis-card /g) === 12, 'more than 12 steps are cut to 12');
h = md(fence('{not json'));
ok(h.includes('code-block') && !h.includes('vis-card'), 'invalid JSON falls back to a normal code block');
h = md(fence({ type: 'flow', steps: [] }));
ok(h.includes('code-block'), 'empty steps fall back to a code block');
h = md('Intro\n```visual\n{"type":"flow","steps":[{"title":"A"', { streaming: true });
ok(h.includes('vis-pending') && !h.includes('code-block'), 'an unfinished fence while streaming shows a placeholder, not raw JSON');
ok(!md('x\n```visual\n{"type":"flow","steps":[{"title":"A"}]}', { streaming: true }).includes('vis-pending'),
  'a complete block without its closing fence still draws');

// charts: inline SVG from numbers we parsed ourselves
const chart = (o) => md(fence(Object.assign({ type: 'chart' }, o)));
h = chart({ kind: 'line', title: 'Threats', unit: '%', labels: ['2024', '2025', '2026'],
  series: [{ name: 'AI', values: [40, 60, 89] }] });
ok(h.includes('<svg class="vis-chart"') && count(h, /<circle class="vis-dot/g) === 3 && h.includes('<polyline'), 'line chart: svg, polyline, one dot per point');
ok(h.includes('2026') && h.includes('>89<'), 'line chart: labels and value labels are drawn');
h = chart({ kind: 'bar', labels: ['a', 'b', 'c', 'd'], series: [{ name: 'x', values: [1, 2, 3, 4] }, { name: 'y', values: [4, 3, 2, 1], tone: 'amber' }] });
ok(count(h, /<rect class="vis-bar/g) === 8 && h.includes('vis-legend') && h.includes('vis-tone-amber'), 'grouped bars: one rect per value and a legend');
h = chart({ kind: 'hbar', unit: 'B', items: [{ label: 'Zero-day', value: 42 }, { label: 'AI', value: 89 }] });
ok(count(h, /<rect class="vis-bar/g) === 2 && h.includes('Zero-day') && h.includes('89 B'), 'hbar from the short items form');
h = chart({ kind: 'bar', labels: ['<img src=x onerror=1>', 'b'], series: [{ name: '"><script>', tone: 'x" onload="1', values: ['5', 'NaN', null, 'Infinity'] }] });
ok(!/<img|<script|onerror=|onload=/i.test(h) && !h.includes('NaN') && !h.includes('Infinity'), 'chart: hostile labels escaped, non-finite numbers become 0');
ok(!h.includes('style='), 'chart: no style attribute');
ok(md(fence({ type: 'bar', labels: ['a'], series: [{ values: [3] }] })).includes('vis-chart'), 'type "bar" is accepted as a chart kind');
ok(md(fence({ type: 'chart', labels: ['a'] })).includes('code-block'), 'a chart with no series falls back to a code block');
ok(count(chart({ kind: 'line', labels: Array.from({ length: 40 }, (_, i) => 'L' + i), series: [{ values: Array.from({ length: 40 }, (_, i) => i) }] }), /<circle class="vis-dot/g) === 24, 'points are capped at 24');
h = chart({ kind: 'bar', labels: ['a', 'b'], series: [{ values: [-5, 10] }] });
ok(h.includes('<svg') && !h.includes('NaN'), 'negative values chart without NaN coordinates');

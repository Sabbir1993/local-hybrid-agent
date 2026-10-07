// tests/js/test_markdown.js - chat Markdown (static/js/markdown.js: marked + DOMPurify) through md() in utils.js.
// Node has no DOM, so DOMPurify is replaced by a pass-through here: every safety assertion below therefore proves the
// FIRST line of defence (marked's renderer escapes raw HTML, chips are built from escaped values) on its own.
// The real DOMPurify is exercised in a browser by tests/js/markdown_browser_check.html.
const fs = require('fs');
const vm = require('vm');
const ok = (c, m) => { if (!c) { console.error('FAIL', m); process.exitCode = 1; } else console.log('ok', m); };

function load(withLibs) {
  const ctx = { console, document: { addEventListener() {} }, window: {}, hlLangFor: () => '', hlCode: c => c, module: { exports: {} } };
  ctx.exports = ctx.module.exports;
  vm.createContext(ctx);
  if (withLibs) {
    vm.runInContext(fs.readFileSync(__dirname + '/../../static/vendor/marked.umd.js', 'utf8'), ctx);
    ctx.marked = ctx.module.exports;
    ctx.DOMPurify = { sanitize: html => html };
  }
  ctx.module = undefined;
  vm.runInContext(fs.readFileSync(__dirname + '/../../static/js/utils.js', 'utf8'), ctx);
  vm.runInContext(fs.readFileSync(__dirname + '/../../static/js/markdown.js', 'utf8'), ctx);
  return ctx;
}
const ctx = load(true);
const md = (s, o) => ctx.md(s, o);

const BRAC = [
  '> **Data caveat:** No 2025 data was retrievable. Any figure is marked `[ESTIMATE - pre-2025]`; *which model is advantaged* changed.',
  '',
  '---',
  '',
  '## 2. Revenue Model',
  '',
  '| Dimension | IBBL | BRAC Bank |',
  '|---|:--:|--:|',
  '| Non-funded income | Moderate | High `[ESTIMATE ~20-25%]` |',
  '| Shariah constraint | Cannot invest in interest-bearing<br>instruments | None |',
].join('\n');
let h = md(BRAC);
ok(/<table>/.test(h) && /<thead>/.test(h) && (h.match(/<th[ >]/g) || []).length === 3, 'table: header row becomes <th> cells');
ok(h.includes('class="md-table-wrap"'), 'table is wrapped for horizontal scroll');
ok(/<th align="center">IBBL/.test(h) && /<th align="right">BRAC Bank/.test(h), 'column alignment from the separator row');
ok(h.includes('<code>[ESTIMATE ~20-25%]</code>'), 'inline code inside a table cell');
ok(h.includes('<br>instruments'), '<br> inside a cell is kept');
ok(/<blockquote>/.test(h) && h.includes('<strong>Data caveat:</strong>'), 'blockquote with bold inside');
ok(h.includes('<hr>'), '--- becomes a horizontal rule');
ok(h.includes('<em>which model is advantaged</em>'), '*italic*');
ok(/<h2>2\. Revenue Model<\/h2>/.test(h), '## heading is a real heading');
ok(!h.includes('|---|'), 'no raw table syntax left');

// lists
h = md('1. first\n2. second\n   - nested a\n   - nested b\n3. third\n\n- [x] done\n- [ ] todo');
ok(/<ol>[\s\S]*<li>first/.test(h) && /<ul>[\s\S]*nested a/.test(h), 'ordered list with a nested bullet list');
ok(h.includes('md-task') && h.includes('✓') && !h.includes('☐') && !h.includes('☑') && !/<input/.test(h), 'task list: empty boxes are dropped, a done item keeps a muted tick, never an <input>');
ok(/<li>\s*todo/.test(h) || h.includes('todo'), 'an unchecked item is still listed');

// emphasis edge cases
h = md('use snake_case_name and my_var_2 here, a lone * star and 3 * 4 = 12');
ok(!/<em>/.test(h), 'snake_case, a lone * and spaced maths are not italicised');
ok(md('~~gone~~ and **bold**').includes('<del>gone</del>'), 'strikethrough');
ok(md('line one\nline two').includes('line one<br>line two'), 'a single newline stays a line break');

// safety (first line of defence: DOMPurify is a pass-through in this test)
const evil = [
  '| a | b |', '|---|---|', '| <img src=x onerror=alert(1)> | [x](javascript:alert(1)) |', '',
  '> <script>alert(1)</script> "><svg onload=alert(1)>', '',
  '- <b onclick="x()">item</b> [click](javascript:alert(2)) ![p](javascript:alert(3))', '',
  '<iframe src="https://evil.example"></iframe>', '',
  '[ok](https://example.com/a?x=1&y=2" onmouseover="alert(4))',
].join('\n');
h = md(evil);
ok(!/<img[^>]*onerror/i.test(h) && !/<script/i.test(h) && !/<svg/i.test(h) && !/<iframe/i.test(h) && !/<b onclick/i.test(h),
  'raw HTML (img onerror, script, svg, iframe, onclick) is shown as text, never as tags');
ok(!/href="javascript:/i.test(h) && !/src="javascript:/i.test(h), 'javascript: links and images are inert');
ok(!/ onmouseover=/i.test(h.replace(/&quot;|&#39;/g, '')) || /&quot;/.test(h), 'quotes in a URL cannot break out of the attribute');
ok((h.match(/<a [^>]*onmouseover/gi) || []).length === 0, 'no event handler attribute is ever produced');
ok(h.includes('&lt;img'), 'the escaped text is visible instead');

// app syntax still works inside lists and table cells
h = md('- see [DOWNLOAD: report.xlsx]\n- video [VIDEO: generated/a.mp4]\n\n| f | v |\n|---|---|\n| [DOWNLOAD: a b.pdf] | [1](https://news.example.com/x) |');
ok(h.includes('data-preview-path="report.xlsx"') && h.includes('/agent/download?path=report.xlsx'), '[DOWNLOAD:] chip inside a list item');
ok(h.includes('<video') && h.includes('/agent/raw?path=generated%2Fa.mp4'), '[VIDEO:] player inside a list item');
ok(h.includes('data-preview-path="a b.pdf"') && h.includes('citation-pill') && h.includes('news.example.com'), 'chip and citation pill inside a table cell');
h = md('![x](/agent/raw?path=pics/a.png) and ![y](https://evil.example/p.png?leak=1)');
ok(h.includes('class="chat-inline-img"') && h.includes('data-load-src="https://evil.example/p.png?leak=1"') && !/<img[^>]*evil\.example/.test(h),
  'local image auto-loads, external image stays click-to-load');
h = md('![x](/agent/raw?path=pics/a.png)\n\n[DOWNLOAD: pics/a.png]');
ok(!h.includes('data-preview-path'), 'a file already shown inline gets no duplicate download chip');
h = md('run /agent-help and open @notes.md then');
ok(h.includes('token-cmd') && h.includes('token-tag'), '/command and @file highlighting');
h = md('see https://example.com/page and [site](https://example.com "T")');
ok((h.match(/target="_blank" rel="noopener noreferrer"/g) || []).length === 2, 'bare URL and normal link open safely in a new tab');
ok(md('[mail](mailto:a@b.com)').includes('mail (mailto:a@b.com)') && !/<a /.test(md('[mail](mailto:a@b.com)')), 'non-http links are plain text');
ok(!md('x 999 y').includes('undefined'), 'forged placeholder characters cannot index the chip list');

// code fences are still handled by md() itself
h = md('before\n```js\nconst a = 1 < 2;\n```\nafter | not | a table');
ok(h.includes('class="code-block"') && h.includes('1 &lt; 2') && h.includes('after'), 'fenced code block unaffected');
ok(!md('x\n```\ncode only').includes('undefined'), 'unclosed fence while streaming');

// streaming
ok(md('hello **half bold', { streaming: true }).includes('<strong>half bold</strong>'), 'streaming: unclosed bold is closed for display');
ok(md('use `code', { streaming: true }).includes('<code>code</code>'), 'streaming: unclosed code span is closed');
ok(md('hello **half bold').includes('**half bold'), 'a finished message is never "healed"');
h = md('| a | b |\n|---|---|\n| 1 |', { streaming: true });
ok(/<table>/.test(h) && !h.includes('undefined'), 'streaming: a half-received table row still renders');
ok(!/<table>/.test(md('| a | b |', { streaming: true })), 'streaming: a lone header line stays text until the separator arrives');

// cache: finished blocks are not parsed again while the last one grows
let parses = 0;
const realMarked = ctx.marked;
ctx.marked = { ...realMarked, parser: (...a) => { parses++; return realMarked.parser(...a); } };
const base = 'para one\n\npara two\n\npara three\n\n';
md(base + 'gro', { streaming: true });
const first = parses;
md(base + 'growing', { streaming: true });
ok(first === 4 && parses - first === 1, 'streaming re-parses only the growing block (' + first + ' then +' + (parses - first) + ')');
ctx.marked = realMarked;

// size
const big = Array.from({ length: 1500 }, (_, i) => `## S${i}\n\n| a | b |\n|---|---|\n| ${i} | **x** |\n\n- item *${i}*`).join('\n\n');
let t0 = Date.now(); md(big); const cold = Date.now() - t0;
t0 = Date.now(); md(big); const warm = Date.now() - t0;
ok(big.length > 90000 && cold < 3000 && warm < cold, `${Math.round(big.length / 1000)} KB renders (cold ${cold} ms, cached ${warm} ms)`);

// fallback when the libraries are missing
const bare = load(false);
const fb = bare.md('**bold** and a | b');
ok(fb.includes('<strong>bold</strong>'), 'without marked / DOMPurify md() falls back to the old renderer');

// task-list boxes are muted decoration: unselectable, no bullet next to them
const css = fs.readFileSync(__dirname + '/../../static/css/style.css', 'utf8');
const taskRule = (css.match(/\.md-task \{[^}]*\}/) || [''])[0];
ok(/user-select:\s*none/.test(taskRule) && /pointer-events:\s*none/.test(taskRule), 'task box is not selectable or clickable');
ok(/li:has\(> \.md-task\)[^{]*\{[^}]*list-style:\s*none/.test(css), 'a task item has no bullet next to its box');

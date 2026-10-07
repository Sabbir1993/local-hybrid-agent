// tests/js/test_grill_cards.js - the "Decision Options" question cards in static/js/chat.js:
// only the newest message can be answered, state does not leak between chats, long checklists are not forms.
// chat.js is too entangled to load whole, so the card code is cut out between two markers and run with stubs.
const fs = require('fs');
const vm = require('vm');
const ok = (c, m) => { if (!c) { console.error('FAIL', m); process.exitCode = 1; } else console.log('ok', m); };

const src = fs.readFileSync(__dirname + '/../../static/js/chat.js', 'utf8');
const a = src.indexOf('window._grillMeta = window._grillMeta || {};');
const b = src.indexOf('function setGenUI(');
ok(a > 0 && b > a, 'found the question-card code in chat.js');

const sent = [], toasts = [];
const ctx = {
  console, window: {}, messages: [], curSession: { id: 'A' }, agentMode: false,
  $: () => null,
  esc: s => String(s).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;'),
  toast: (m) => toasts.push(m),
  send: (t) => sent.push(t), runAgentSSE: (t) => sent.push(t),
  document: { addEventListener() {} },
};
ctx.window = ctx;
vm.createContext(ctx);
vm.runInContext(src.slice(a, b), ctx);

const Q = '❓ Q1: Which database should we use?\n- [ ] Postgres\n- [ ] MySQL\n- [ ] SQLite\n';
const render = (idx, closed) => ctx.renderInteractiveQuestions(Q, idx, closed);

// the newest message: live card
ctx.messages = [{}, {}, {}];
let h = render(2, false);
ok(h.includes('Submit Decisions') && !h.includes('aria-disabled') && !/\bdisabled\b/.test(h.replace('grill-submit-btn', '')), 'newest message: the card is live');

// an older message: read-only, whatever the page remembers
h = render(0, true);
ok(h.includes('Earlier question (closed)') && h.includes('>Closed<') && h.includes('aria-disabled="true"') && /grill-submit-btn" disabled/.test(h),
  'older message: card is closed, options and button are disabled');
ok(h.includes('class="grill-custom-input"') && /class="grill-custom-input"[^>]* disabled/.test(h), 'older message: the "Other" box is disabled too');

// clicks and submits on an older card do nothing (also if the DOM were re-enabled by hand)
ctx.messages = [{}, {}, {}];
render(0, true);
ctx.toggleGrillOption(0, 0, 1);
ok(!ctx._grillSelected[0] || !ctx._grillSelected[0][0] || ctx._grillSelected[0][0].size === 0, 'toggling an option on an older card is ignored');
ctx.submitGrillAnswers(0);
ok(sent.length === 0 && toasts.some(t => /earlier in the chat/.test(t)), 'submitting an older card sends nothing and explains why');

// the live card still submits, once
render(2, false);
ctx.toggleGrillOption(2, 0, 1);
ctx.submitGrillAnswers(2);
ok(sent.length === 1 && sent[0] === 'Q1: MySQL', 'newest card submits the chosen option');
ctx.submitGrillAnswers(2);
ok(sent.length === 1, 'and only once');

// once something follows it, the same card renders as closed even though it was answered
ctx.messages = [{}, {}, {}, {}];
h = render(2, true);
ok(h.includes('Answers submitted') && h.includes('Answer Submitted'), 'an answered card shows as submitted');

// a long checklist is a document, not a decision (the 74-item report outline)
const list = '**Coverage checklist**\n' + Array.from({ length: 74 }, (_, i) => `${i + 1}) [ ] Item number ${i + 1} to extract`).join('\n');
ok(ctx.renderInteractiveQuestions(list, 2, false) === '', '74 checkbox lines produce no question card');
const mid = '❓ Q1: Pick the sections\n' + Array.from({ length: 12 }, (_, i) => `- [ ] Section ${i + 1}`).join('\n');
ok(ctx.renderInteractiveQuestions(mid, 2, false).includes('Section 12'), '12 options is still a normal question');
const over = '❓ Q1: Pick the sections\n' + Array.from({ length: 13 }, (_, i) => `- [ ] Section ${i + 1}`).join('\n');
ok(ctx.renderInteractiveQuestions(over, 2, false) === '', '13 options is treated as a list');

// state does not leak into another chat
ctx.messages = [{}, {}, {}];
ctx.curSession = { id: 'A' };
render(2, false);
ctx.toggleGrillOption(2, 0, 0);
ctx.submitGrillAnswers(2);
ok(ctx._grillSubmitted.has(2), 'chat A: message 2 answered');
ctx.curSession = { id: 'B' };
h = render(2, false);
ok(h.includes('Submit Decisions') && !h.includes('Answers submitted') && !ctx._grillSubmitted.has(2) && !(ctx._grillSelected[2] && ctx._grillSelected[2][0] && ctx._grillSelected[2][0].size),
  'chat B: message 2 is a fresh, unanswered card with nothing pre-ticked');

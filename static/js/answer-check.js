/* ---------------- answer-check.js (shield toggle + verdict badge) ----------------
 * The 🛡 button next to Send picks how answers are double-checked for this browser:
 *   off   - no check
 *   badge - "Check after": the answer streams as usual, a badge appears afterwards
 *   gate  - "Check before showing": the answer stays hidden until it's checked
 * The server runs the check (core/verifier.py) with the model picked for the
 * "Checking answers" job in Settings -> Models, and streams verify_start /
 * verify_result. A slow or broken checker never blocks the answer. */

const ANSWER_CHECK_STATES = [
  { mode: 'off', icon: '🛡', label: 'Answer check: off', short: 'Off' },
  { mode: 'badge', icon: '🛡✓', label: 'Answer check: check after — the answer streams, then a badge shows the result', short: 'Check after' },
  { mode: 'gate', icon: '🛡⏸', label: 'Answer check: check before showing — the answer appears once it has been checked', short: 'Check before showing' },
];

function getAnswerCheckMode() {
  try {
    const v = localStorage.getItem('answer_check');
    if (v && ANSWER_CHECK_STATES.some(s => s.mode === v)) return v;
  } catch (_) {}
  return window._answerCheckDefault || 'off';
}
window.getAnswerCheckMode = getAnswerCheckMode;

function _paintShield() {
  const b = document.getElementById('btn-answer-check');
  if (!b) return;
  const st = ANSWER_CHECK_STATES.find(s => s.mode === getAnswerCheckMode()) || ANSWER_CHECK_STATES[0];
  b.textContent = st.icon;
  b.title = 'Answer check: a second model double-checks each reply. Now: ' + st.short
    + '. Click to switch (Off → Check after → Check before showing)';
  b.setAttribute('aria-label', st.label);
  b.setAttribute('aria-pressed', st.mode === 'off' ? 'false' : 'true');
  b.classList.toggle('on', st.mode !== 'off');
  b.dataset.mode = st.mode;
}

(function initShield() {
  const b = document.getElementById('btn-answer-check');
  if (!b) return;
  b.addEventListener('click', () => {
    const cur = getAnswerCheckMode();
    const i = ANSWER_CHECK_STATES.findIndex(s => s.mode === cur);
    const next = ANSWER_CHECK_STATES[(i + 1) % ANSWER_CHECK_STATES.length];
    try { localStorage.setItem('answer_check', next.mode); } catch (_) {}
    _paintShield();
    if (typeof toast === 'function') toast('🛡 Answer check: ' + next.short);
  });
  _paintShield();
  // one-time hint after the upgrade: models/jobs are now configurable
  try {
    if (!localStorage.getItem('lanes_intro_chat')) {
      localStorage.setItem('lanes_intro_chat', '1');
      setTimeout(() => {
        if (typeof toast === 'function') toast('New: add more models and choose which one does each job — and turn on 🛡 answer checks.', {
          duration: 12000, actions: [{ label: 'Show me', onClick: () => { location.href = '/settings#sec-lanes'; } }] });
      }, 2500);
    }
  } catch (_) {}
  // saved default from Settings -> Models (used until the button is clicked here)
  fetch('/control/lanes').then(r => r.ok ? r.json() : null).then(d => {
    if (d && d.verification) window._answerCheckDefault = d.verification.mode || 'off';
    _paintShield();
  }).catch(() => {});
})();

/* Called when a request starts: gate mode hides the answer until it's checked. */
function answerCheckBegin(L) {
  const mode = getAnswerCheckMode();
  L.check = mode === 'gate' ? { state: 'pending', mode } : undefined;
  return mode;
}
window.answerCheckBegin = answerCheckBegin;

/* SSE hook. Returns true when the event was an answer-check event. */
function sseAnswerCheck(L, ev, d) {
  if (ev === 'verify_start') {
    L.check = { state: 'checking', mode: d.mode, checker: d.checker, source: d.source };
    L.statusText = 'Double-checking with ' + (d.checker || 'the checker') + '…';
    if (typeof window.setLiveHud === 'function') {
      window.setLiveHud({ phase: 'running', text: L.statusText });
    }
    return true;
  }
  if (ev === 'verify_result') {
    L.check = Object.assign({}, L.check || {}, d, { state: 'done' });
    L.statusText = '';
    return true;
  }
  return false;
}
window.sseAnswerCheck = sseAnswerCheck;

/* The run ended: a gate that never started (answer too short / check disabled
   on the server) must not keep the answer hidden. */
function answerCheckEnd(L) {
  if (L.check && L.check.state !== 'done' && L.check.state !== 'checking') L.check = undefined;
  if (L.check && L.check.state === 'checking') {
    L.check = Object.assign({}, L.check, { state: 'done', verdict: 'unverified', issues: [] });
  }
}
window.answerCheckEnd = answerCheckEnd;

/* True while the answer must stay hidden (gate mode, not checked yet). */
function answerCheckHides(m) {
  return !!(m && m.check && m.check.mode === 'gate' && m.check.state !== 'done');
}
window.answerCheckHides = answerCheckHides;

function answerCheckGateHtml(m) {
  const who = (m.check && m.check.checker) ? esc(m.check.checker) : 'the checker';
  const txt = m.check && m.check.state === 'checking'
    ? `Double-checking with <b>${who}</b>…`
    : 'Writing the answer — it will be checked before it is shown…';
  return `<div class="ac-gate" role="status" aria-live="polite"><span class="ac-shimmer"></span><span>🛡 ${txt}</span></div>`;
}
window.answerCheckGateHtml = answerCheckGateHtml;

/* Verdict badge under a finished answer. */
function answerCheckBadgeHtml(m, idx) {
  const c = m && m.check;
  if (!c || c.state !== 'done' || c.dismissed) return '';
  const who = esc(c.checker || 'the checker');
  const issues = (c.issues || []);
  const list = issues.length
    ? `<ul class="ac-issues">${issues.map(i => `<li><span class="ac-sev ac-sev-${esc(i.severity)}">${i.severity === 'major' ? 'Important' : 'Minor'}</span> ${esc(i.text)}</li>`).join('')}</ul>`
    : '';
  const orig = c.original
    ? `<details class="ac-orig"><summary>Show original answer</summary><div class="ac-orig-body">${typeof md === 'function' ? md(c.original) : esc(c.original)}</div></details>`
    : '';
  if (c.verdict === 'unverified') {
    return `<div class="ac-badge ac-unverified" role="note">🛡 <span>Couldn't check this time${c.note ? ' — ' + esc(c.note) : ''}</span></div>`;
  }
  if (c.verdict === 'pass') {
    const fixed = c.fixed ? ` · <b>Fixed ${c.fixed} issue${c.fixed > 1 ? 's' : ''}</b>` : '';
    const minor = issues.length
      ? `<details class="ac-more"><summary>${issues.length} small note${issues.length > 1 ? 's' : ''}</summary>${list}</details>` : '';
    return `<div class="ac-badge ac-pass" role="note">✅ <span>Checked by ${who}${fixed}</span>${minor}${orig}</div>`;
  }
  return `<div class="ac-badge ac-fail" role="note">
    <details class="ac-more" open><summary>⚠ <span>${issues.length || 'Some'} possible issue${issues.length === 1 ? '' : 's'} found by ${who}</span></summary>
    ${list}
    <div class="ac-actions">
      <button type="button" class="btn ghost ac-btn" data-ac="fix" data-idx="${idx}">Ask to fix</button>
      <button type="button" class="btn ghost ac-btn" data-ac="ignore" data-idx="${idx}">Ignore</button>
    </div></details>${orig}</div>`;
}
window.answerCheckBadgeHtml = answerCheckBadgeHtml;

document.addEventListener('click', (e) => {
  const b = e.target.closest && e.target.closest('.ac-btn');
  if (!b || typeof messages === 'undefined') return;
  const m = messages[Number(b.dataset.idx)];
  if (!m || !m.check) return;
  if (b.dataset.ac === 'ignore') {
    m.check.dismissed = true;
    if (typeof renderAll === 'function') renderAll(); else if (typeof renderLast === 'function') renderLast();
    return;
  }
  if (b.dataset.ac === 'fix') {
    const items = (m.check.issues || []).map(i => '- ' + i.text).join('\n');
    const input = document.getElementById('input');
    if (!input) return;
    input.value = 'Please fix these problems in your last answer:\n' + items;
    input.focus();
    if (typeof submitPrompt === 'function') submitPrompt();
  }
});


/* Company knowledge base switch in the composer: Auto -> Always -> Off.
   Auto = the app decides from the user's own words (core/knowledge_router.should_consult_kb); Always searches on every
   message; Off never does. The mode is sent with every chat / agent request (field `kb`) and remembered in this browser. */
(function () {
  'use strict';
  var KEY = 'chat_kb_mode';
  var MODES = ['auto', 'on', 'off'];
  var LABEL = { auto: 'Auto', on: 'Always', off: 'Off' };
  var HELP = {
    auto: 'the app searches it only when your question is about the company (not for attached files or small talk)',
    on: 'every message is checked against it',
    off: 'it is never searched for you (the model cannot read it)'
  };

  function mode() {
    var m = 'auto';
    try { m = localStorage.getItem(KEY) || 'auto'; } catch (e) { /* private mode: keep auto */ }
    return MODES.indexOf(m) >= 0 ? m : 'auto';
  }

  function paint() {
    var b = document.getElementById('btn-kb-toggle');
    if (!b) return;
    var m = mode();
    b.dataset.mode = m;
    b.classList.toggle('active', m === 'on');
    var tag = b.querySelector('.comp-kb-tag');
    if (tag) tag.textContent = m === 'auto' ? '' : (m === 'on' ? 'on' : 'off');
    var t = 'Company knowledge base: ' + LABEL[m] + ' - ' + HELP[m] + '. Click to change.';
    b.title = t;
    b.setAttribute('aria-label', 'Company knowledge base: ' + LABEL[m]);
  }

  function next() {
    var m = MODES[(MODES.indexOf(mode()) + 1) % MODES.length];
    try { localStorage.setItem(KEY, m); } catch (e) { /* not remembered */ }
    paint();
    if (typeof window.toast === 'function') window.toast('Company knowledge base: ' + LABEL[m] + ' - ' + HELP[m]);
  }

  window.chatKbMode = mode;
  var b = document.getElementById('btn-kb-toggle');
  if (b) b.addEventListener('click', next);
  paint();
})();

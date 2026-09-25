/* reasoning effort chip (None / Low / Medium / High / Extra) + Deep research switch
   Only the level name is sent; the server maps it per model (core/reasoning.py).
   Deep research (chatDeepMode, theme.js) is chat-only and independent of the level. */
const EFFORT_LEVELS = ['none', 'low', 'medium', 'high', 'extra'];
const EFFORT_LABELS = { none: 'None', low: 'Low', medium: 'Medium', high: 'High', extra: 'Extra' };
let chatEffort = 'low';
try {
  const saved = localStorage.getItem('chat_effort');
  if (EFFORT_LEVELS.includes(saved)) chatEffort = saved;
} catch (e) {}

function _effortModelMode() {
  const sel = $('profile');
  const opt = (sel && sel.selectedIndex >= 0) ? sel.options[sel.selectedIndex] : null;
  return (opt && opt.dataset.reasoningMode) || 'none';
}

// Level for request payloads; undefined when the selected model can't reason
function getReasoningEffort() {
  return _effortModelMode() === 'none' ? undefined : chatEffort;
}

function updateEffortUI() {
  const wrap = $('effort-wrap');
  if (!wrap) return;
  const mode = _effortModelMode();
  const canReason = mode !== 'none';
  const deepShown = !agentMode;               // Deep research only applies to chat
  const deepOn = deepShown && !!chatDeepMode;
  // nothing to choose: non-reasoning model in agent mode
  wrap.style.display = (canReason || deepShown) ? 'inline-flex' : 'none';
  document.querySelectorAll('#effort-menu .effort-item').forEach(it => {
    const on = it.dataset.level === chatEffort;
    it.style.display = canReason ? '' : 'none';
    it.classList.toggle('active', on);
    it.setAttribute('aria-checked', on ? 'true' : 'false');
  });
  const sep = $('effort-sep');
  if (sep) sep.style.display = (canReason && deepShown) ? '' : 'none';
  const deep = $('effort-deep');
  if (deep) {
    deep.style.display = deepShown ? '' : 'none';
    deep.classList.toggle('on', deepOn);
    deep.setAttribute('aria-checked', deepOn ? 'true' : 'false');
  }
  const parts = [];
  if (canReason) parts.push(EFFORT_LABELS[chatEffort]);
  if (deepOn) parts.push('Deep');
  const label = $('effort-label');
  if (label) label.textContent = parts.join(' · ') || 'Standard';
  const btn = $('effort-btn');
  if (btn) {
    btn.classList.toggle('deep', deepOn);
    btn.title = (canReason ? 'Reasoning effort: ' + EFFORT_LABELS[chatEffort] +
      (mode === 'toggle' ? ' (this model always thinks: None/Low keep it short)' : '') : 'No reasoning for this model') +
      (deepShown ? ' · Deep research ' + (deepOn ? 'ON' : 'OFF') : '');
  }
}

function initEffortChip() {
  const btn = $('effort-btn');
  const menu = $('effort-menu');
  if (!btn || !menu || btn._effortBound) return;
  btn._effortBound = true;
  const close = () => { menu.classList.remove('open'); btn.setAttribute('aria-expanded', 'false'); };
  btn.addEventListener('click', e => {
    e.stopPropagation();
    const open = !menu.classList.contains('open');
    menu.classList.toggle('open', open);
    btn.setAttribute('aria-expanded', open ? 'true' : 'false');
  });
  menu.addEventListener('click', e => {
    if (e.target.closest('#effort-deep')) {
      // switch row: keep the menu open so the level can be changed too
      chatDeepMode = !chatDeepMode;
      try { localStorage.setItem('chat_deep_mode', chatDeepMode ? '1' : '0'); } catch (err) {}
      updateEffortUI();
      return;
    }
    const it = e.target.closest('.effort-item');
    if (!it || !EFFORT_LEVELS.includes(it.dataset.level)) return;
    chatEffort = it.dataset.level;
    try { localStorage.setItem('chat_effort', chatEffort); } catch (err) {}
    updateEffortUI();
    close();
  });
  document.addEventListener('click', e => {
    if (menu.classList.contains('open') && !menu.contains(e.target) && !btn.contains(e.target)) close();
  });
  document.addEventListener('keydown', e => { if (e.key === 'Escape') close(); });
  updateEffortUI();
}

initEffortChip();

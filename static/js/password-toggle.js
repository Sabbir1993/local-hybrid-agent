/* Eye button on every password field: click to show / hide what was typed.
   Works for fields in the static pages and for the ones scripts add later (modals, settings panels): a
   MutationObserver enhances each <input type="password"> once. Loaded on the login page too, so it has no dependencies. */
(function () {
  'use strict';

  const EYE = '<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" ' +
    'stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M2 12s3.6-7 10-7 10 7 10 7-3.6 7-10 7-10-7-10-7Z"/>' +
    '<circle cx="12" cy="12" r="3"/></svg>';
  const EYE_OFF = '<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" ' +
    'stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M9.9 5.2A10.6 10.6 0 0 1 12 5c6.4 0 10 7 10 7a17.6 ' +
    '17.6 0 0 1-3.2 4.2M6.6 6.6C3.7 8.4 2 12 2 12s3.6 7 10 7c1.6 0 3.1-.4 4.4-1"/><path d="M9.9 9.9a3 3 0 0 0 4.2 4.2"/><path d="m2 2 20 20"/></svg>';

  function addStyle() {
    if (document.getElementById('pw-toggle-style')) return;
    const st = document.createElement('style');
    st.id = 'pw-toggle-style';
    st.textContent =
      '.pw-wrap{position:relative;max-width:100%}' +
      '.pw-eye{position:absolute;right:6px;top:50%;transform:translateY(-50%);width:26px;height:26px;margin:0;padding:0;display:inline-flex;' +
      'align-items:center;justify-content:center;background:transparent;border:none;border-radius:6px;color:var(--dim,#8b949e);cursor:pointer;' +
      'box-shadow:none;line-height:1}' +
      '.pw-eye:hover{color:var(--text,#e6edf3);background:rgba(128,128,128,.16)}' +
      '.pw-eye:focus-visible{outline:2px solid var(--accent,#58a6ff);outline-offset:1px}' +
      '.pw-wrap>input{padding-right:36px!important}';
    document.head.appendChild(st);
  }

  function enhance(input) {
    if (input.dataset.pwToggle || (input.closest && input.closest('.pw-wrap'))) return;
    const parent = input.parentNode;
    if (!parent) return;
    input.dataset.pwToggle = '1';
    const cs = window.getComputedStyle(input);
    const wrap = document.createElement('span');
    wrap.className = 'pw-wrap';
    // the wrapper takes the input's place in the layout: same display / width / flex, the input then fills it
    const pcs = window.getComputedStyle(parent);
    const inFlex = /flex|grid/.test(pcs.display);
    const avail = parent.clientWidth - (parseFloat(pcs.paddingLeft) || 0) - (parseFloat(pcs.paddingRight) || 0);
    const full = /%$/.test(cs.width) || cs.display === 'block' || (input.offsetWidth > 0 && input.offsetWidth >= avail - 4);
    wrap.style.display = (full || inFlex) ? 'block' : 'inline-block';
    if (inFlex) { wrap.style.flex = cs.flex; wrap.style.minWidth = '0'; }
    if (input.style.width && !/%$/.test(input.style.width)) wrap.style.width = input.style.width;
    if (full || input.style.width) input.style.width = '100%';
    if (cs.margin && cs.margin !== '0px') { wrap.style.margin = cs.margin; input.style.margin = '0'; }   // so the eye centres on the field, not its margin
    parent.insertBefore(wrap, input);
    wrap.appendChild(input);

    const btn = document.createElement('button');
    btn.type = 'button';
    btn.className = 'pw-eye';
    btn.innerHTML = EYE;
    btn.setAttribute('aria-label', 'Show password');
    btn.setAttribute('aria-pressed', 'false');
    btn.title = 'Show password';
    btn.tabIndex = 0;
    wrap.appendChild(btn);

    const set = show => {
      input.type = show ? 'text' : 'password';
      btn.innerHTML = show ? EYE_OFF : EYE;
      btn.setAttribute('aria-pressed', String(show));
      btn.setAttribute('aria-label', show ? 'Hide password' : 'Show password');
      btn.title = show ? 'Hide password' : 'Show password';
    };
    btn.addEventListener('mousedown', e => e.preventDefault());          // keep focus (and the caret) in the field
    btn.addEventListener('click', () => { set(input.type === 'password'); input.focus(); });
    // never leave a password readable after the form goes away or is submitted
    if (input.form) input.form.addEventListener('submit', () => set(false));
    input.addEventListener('blur', e => { if (input.type === 'text' && e.relatedTarget !== btn) set(false); });
  }

  function scan(root) {
    (root || document).querySelectorAll('input[type="password"]').forEach(enhance);
  }

  let queued = false;
  function schedule() {
    if (queued) return;
    queued = true;
    requestAnimationFrame(() => { queued = false; scan(); });
  }

  function start() {
    addStyle();
    scan();
    new MutationObserver(schedule).observe(document.body, { childList: true, subtree: true });
  }
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', start); else start();
  window.enhancePasswordFields = scan;
})();

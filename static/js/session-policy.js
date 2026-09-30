/* session-policy.js - admin panel for idle sign-out (settings.html #sec-session).
 * GET/PUT /auth/session-policy {enabled, minutes}; needs users.manage. */
'use strict';

async function loadSessionPolicyPanel() {
  const box = document.getElementById('session-policy-content');
  if (!box) return;
  let p;
  try {
    const r = await fetch('/auth/session-policy');
    if (!r.ok) { box.innerHTML = '<div class="mon-empty">You do not have permission to change session security.</div>'; return; }
    p = await r.json();
  } catch (e) {
    box.innerHTML = '<div class="mon-empty">Could not load the session policy.</div>';
    return;
  }
  box.innerHTML = `
    <div style="padding:10px 4px;display:flex;flex-direction:column;gap:14px;max-width:520px">
      <label style="display:flex;align-items:center;gap:8px;font-size:13px">
        <input type="checkbox" id="sp-enabled" ${p.enabled ? 'checked' : ''}>
        <span>Sign users out after inactivity</span>
      </label>
      <label style="display:flex;flex-direction:column;gap:4px;font-size:13px">
        <span>Idle time (minutes)</span>
        <input type="number" id="sp-minutes" min="${p.min}" max="${p.max}" value="${p.minutes}" style="width:120px">
        <span class="dim" style="font-size:11px">${p.min}-${p.max} minutes. Typing, clicking and scrolling count as activity, and so does an open chat or agent run.</span>
      </label>
      <div id="sp-warn" class="dim" style="font-size:12px;line-height:1.5"></div>
      <div style="display:flex;align-items:center;gap:10px">
        <button type="button" class="btn accent" id="sp-save" style="width:auto;padding:6px 14px">Save</button>
        <span id="sp-msg" class="dim" style="font-size:12px"></span>
      </div>
    </div>`;
  const en = box.querySelector('#sp-enabled'), mins = box.querySelector('#sp-minutes');
  const warn = box.querySelector('#sp-warn'), msg = box.querySelector('#sp-msg');
  const refresh = () => {
    mins.disabled = !en.checked;
    const m = Number(mins.value) || 0;
    warn.textContent = !en.checked
      ? 'Idle sign-out is off: a session stays signed in until the 7-day limit or until the user logs out. PCI DSS 8.2.8 expects re-authentication after 15 minutes idle, so keep it on unless this server is on a trusted, access-controlled network. The change is recorded in the audit log.'
      : m > 15
        ? 'More than 15 minutes is longer than PCI DSS 8.2.8 allows. The change is recorded in the audit log.'
        : '';
  };
  en.addEventListener('change', refresh);
  mins.addEventListener('input', refresh);
  refresh();
  box.querySelector('#sp-save').addEventListener('click', async () => {
    msg.textContent = 'Saving…';
    try {
      const r = await fetch('/auth/session-policy', {
        method: 'PUT', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ enabled: en.checked, minutes: Math.round(Number(mins.value)) }),
      });
      const d = await r.json().catch(() => ({}));
      msg.textContent = r.ok ? 'Saved. Users pick it up when they reload the page.' : (d.detail || 'Could not save.');
    } catch (e) {
      msg.textContent = 'Could not save.';
    }
  });
}
window.loadSessionPolicyPanel = loadSessionPolicyPanel;

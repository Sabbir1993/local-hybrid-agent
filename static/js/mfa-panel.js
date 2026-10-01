/* mfa-panel.js - personal two-factor settings + admin enforcement toggle
 * (settings.html #sec-mfa). Personal section needs no permission; the policy
 * toggle appears only for users.manage holders (GET /auth/mfa/policy 200).
 * A 403 mfa_step_up_required on a write offers an inline code box that calls
 * POST /auth/mfa/step-up and retries once. */
'use strict';

async function mfaFetch(url, opts) {
  const r = await fetch(url, opts);
  const d = await r.json().catch(() => ({}));
  return { r, d };
}

async function withStepUp(box, msgEl, fn) {
  const first = await fn();
  if (first.r.status !== 403 || first.d.detail !== 'mfa_step_up_required') return first;
  msgEl.innerHTML = 'This change needs a fresh second-factor check. ' +
    '<input type="text" id="mfa-step-code" inputmode="numeric" placeholder="123456" style="width:120px; display:inline-block; margin:0 6px;"> ' +
    '<button type="button" class="btn" id="mfa-step-go" style="width:auto;padding:6px 14px;height:34px;">Verify</button>';
  const ok = await new Promise((resolve) => {
    box.querySelector('#mfa-step-go').addEventListener('click', async () => {
      const code = box.querySelector('#mfa-step-code').value.trim();
      const s = await mfaFetch('/auth/mfa/step-up', {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ code }),
      });
      resolve(s.r.ok);
    }, { once: true });
  });
  if (!ok) {
    msgEl.textContent = 'Verification failed — nothing was changed.';
    return first;
  }
  msgEl.textContent = '';
  return fn();
}

async function loadMfaPanel() {
  const box = document.getElementById('mfa-content');
  if (!box) return;
  let st;
  try {
    const { r, d } = await mfaFetch('/auth/mfa/status');
    if (!r.ok) throw new Error();
    st = d;
  } catch (e) {
    box.innerHTML = '<div class="mon-empty">Could not load two-factor settings.</div>';
    return;
  }
  let policy = null;
  try {
    const { r, d } = await mfaFetch('/auth/mfa/policy');
    if (r.ok) policy = d;
  } catch (e) { /* not an admin: personal section only */ }

  box.innerHTML = `
    <div style="padding:10px 4px;display:flex;flex-direction:column;gap:16px;max-width:560px">
      <div>
        <div style="font-size:13px;margin-bottom:6px">Status: <b>${st.enabled ? 'On' : 'Off'}</b>${st.enabled ? ` <span class="dim">(${st.backup_remaining} backup codes left)</span>` : ''}${st.enforced ? ' <span class="dim">(required by your organization)</span>' : ''}</div>
        <div id="mfa-personal"></div>
      </div>
      ${policy !== null ? `
      <div style="border-top:1px solid var(--border);padding-top:12px">
        <label style="display:flex;align-items:center;gap:8px;font-size:13px">
          <input type="checkbox" id="mfa-required" ${policy.required ? 'checked' : ''}>
          <span>Require two-factor authentication for everyone</span>
        </label>
        <div class="dim" style="font-size:11px;margin-top:4px">Accounts without it enroll at their next sign-in (nothing breaks mid-session). Recorded in the audit log.</div>
        <div style="display:flex;align-items:center;gap:10px;margin-top:8px">
          <button type="button" class="btn accent" id="mfa-policy-save" style="width:auto;padding:6px 14px">Save</button>
          <span id="mfa-policy-msg" class="dim" style="font-size:12px"></span>
        </div>
      </div>` : ''}
    </div>`;

  const personal = box.querySelector('#mfa-personal');
  if (!st.enabled) {
    personal.innerHTML = `
      <div class="dim" style="font-size:12px;margin-bottom:8px">Use any authenticator app (Google/Microsoft Authenticator, 1Password, …). You will enter a 6-digit code at sign-in, plus 8 single-use backup codes for emergencies.</div>
      <label style="display:flex;flex-direction:column;gap:4px;font-size:11px;font-weight:600;color:var(--dim);margin-bottom:4px">Current password
        <input type="password" id="mfa-enroll-pw" autocomplete="current-password" style="width:240px;"></label>
      <div style="display:flex;align-items:center;gap:10px;margin-top:8px">
        <button type="button" class="btn accent" id="mfa-enroll-go" style="width:auto;padding:6px 14px">Start enrollment</button>
        <span id="mfa-msg" class="dim" style="font-size:12px"></span>
      </div>
      <div id="mfa-enroll-show" style="margin-top:10px"></div>`;
    const msg = personal.querySelector('#mfa-msg');
    personal.querySelector('#mfa-enroll-go').addEventListener('click', async () => {
      msg.textContent = 'Starting…';
      const { r, d } = await mfaFetch('/auth/mfa/enroll', {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ password: personal.querySelector('#mfa-enroll-pw').value }),
      });
      if (!r.ok) { msg.textContent = d.detail || 'Could not start enrollment.'; return; }
      msg.textContent = '';
      const show = personal.querySelector('#mfa-enroll-show');
      show.innerHTML = `
        <div class="dim" style="font-size:12px">Type this secret into your authenticator app (or copy the setup URI):</div>
        <code style="display:block;word-break:break-all;font-size:12px;margin:4px 0;">${d.secret}</code>
        <code style="display:block;word-break:break-all;font-size:10px;margin:4px 0;">${d.otpauth_uri}</code>
        <div class="dim" style="font-size:12px">Backup codes — save these now, each works once:</div>
        <code style="display:block;word-break:break-all;font-size:12px;margin:4px 0;">${d.backup_codes.join('  ')}</code>
        <label style="display:flex;flex-direction:column;gap:4px;font-size:11px;font-weight:600;color:var(--dim);margin-top:10px;margin-bottom:4px">Code from the app
          <input type="text" id="mfa-confirm-code" inputmode="numeric" placeholder="123456" style="width:140px;"></label>
        <div style="display:flex;align-items:center;gap:10px;margin-top:8px">
          <button type="button" class="btn accent" id="mfa-confirm-go" style="width:auto;padding:6px 14px">Confirm &amp; enable</button>
          <span id="mfa-confirm-msg" class="dim" style="font-size:12px"></span>
        </div>`;
      show.querySelector('#mfa-confirm-go').addEventListener('click', async () => {
        const cmsg = show.querySelector('#mfa-confirm-msg');
        const { r: r2, d: d2 } = await mfaFetch('/auth/mfa/confirm', {
          method: 'POST', headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ code: show.querySelector('#mfa-confirm-code').value.trim() }),
        });
        if (!r2.ok) { cmsg.textContent = d2.detail || 'Code rejected.'; return; }
        loadMfaPanel();
      });
    });
  } else {
    personal.innerHTML = `
      <label style="display:flex;flex-direction:column;gap:4px;font-size:11px;font-weight:600;color:var(--dim);margin-bottom:4px">Current password (to turn it off)
        <input type="password" id="mfa-disable-pw" autocomplete="current-password" style="width:240px;"></label>
      <div style="display:flex;align-items:center;gap:10px;margin-top:8px">
        <button type="button" class="btn" id="mfa-disable-go" style="width:auto;padding:6px 14px">Turn off two-factor</button>
        <span id="mfa-msg" class="dim" style="font-size:12px"></span>
      </div>`;
    const msg = personal.querySelector('#mfa-msg');
    personal.querySelector('#mfa-disable-go').addEventListener('click', async () => {
      const password = personal.querySelector('#mfa-disable-pw').value;
      const { r, d } = await withStepUp(box, msg, () => mfaFetch('/auth/mfa/disable', {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ password }),
      }));
      if (!r.ok) { if (msg.textContent === '') msg.textContent = d.detail || 'Could not turn off.'; return; }
      loadMfaPanel();
    });
  }

  const saveBtn = box.querySelector('#mfa-policy-save');
  if (saveBtn) {
    const pmsg = box.querySelector('#mfa-policy-msg');
    saveBtn.addEventListener('click', async () => {
      pmsg.textContent = 'Saving…';
      const required = box.querySelector('#mfa-required').checked;
      const { r, d } = await withStepUp(box, pmsg, () => mfaFetch('/auth/mfa/policy', {
        method: 'PUT', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ required }),
      }));
      pmsg.textContent = r.ok ? 'Saved.' : (pmsg.textContent || d.detail || 'Could not save.');
    });
  }
}
window.loadMfaPanel = loadMfaPanel;

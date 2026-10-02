/* ---------------- login.js (login.html) ---------------- */
// Same-origin paths only: next=javascript:... would run in this origin, next=//evil is a phish.
function safeNext(next) {
  if (!next || next[0] !== '/' || next[1] === '/' || next[1] === '\\') return '/';
  try {
    const u = new URL(next, location.origin);
    return u.origin === location.origin ? u.pathname + u.search + u.hash : '/';
  } catch (e) { return '/'; }
}

function goNext() {
  location.href = safeNext(new URLSearchParams(location.search).get('next'));
}

async function postJSON(url, body) {
  const resp = await fetch(url, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    credentials: 'same-origin',
    body: JSON.stringify(body),
  });
  const data = await resp.json().catch(() => ({}));
  return { resp, data };
}

function showStep(which, text) {
  document.getElementById('step-password').style.display = which === 'password' ? '' : 'none';
  document.getElementById('step-mfa').style.display = which === 'mfa' ? '' : 'none';
  document.getElementById('step-enroll').style.display = which === 'enroll' ? '' : 'none';
  document.getElementById('login-form').style.display = which === 'password' ? '' : 'none';
  document.getElementById('mfa-form').style.display = which === 'mfa' ? '' : 'none';
  document.getElementById('enroll-form').style.display = which === 'enroll' ? '' : 'none';
  document.querySelector('#login-box p.sub').textContent = text;
}

let mfaTicket = null;

document.getElementById('login-form').addEventListener('submit', async (e) => {
  e.preventDefault();
  const errBox = document.getElementById('login-err');
  errBox.textContent = '';
  const username = document.getElementById('username').value.trim();
  const password = document.getElementById('password').value;
  try {
    const { resp, data } = await postJSON('/auth/login', { username, password });
    if (!resp.ok) {
      errBox.textContent = data.detail || 'Sign in failed';
      return;
    }
    if (data.mfa_required) {
      mfaTicket = data.ticket;
      if (data.enroll_required) {
        // enforced MFA, nothing enrolled yet: bootstrap over the ticket
        const en = await postJSON('/auth/mfa/ticket/enroll', { ticket: mfaTicket });
        if (!en.resp.ok) {
          errBox.textContent = en.data.detail || 'Could not start MFA enrollment';
          return;
        }
        const qrImg = document.getElementById('enroll-qr');
        if (qrImg && en.data.otpauth_uri) {
          qrImg.src = `https://api.qrserver.com/v1/create-qr-code/?size=160x160&data=${encodeURIComponent(en.data.otpauth_uri)}`;
          qrImg.style.display = 'block';
        }
        document.getElementById('enroll-secret').textContent = en.data.secret;
        document.getElementById('enroll-uri').textContent = en.data.otpauth_uri;
        document.getElementById('enroll-codes').textContent = (en.data.backup_codes || []).join('  ');
        showStep('enroll', 'Your organization requires two-factor authentication — scan or type the secret into your authenticator app, save the backup codes, then enter a code below.');
      } else {
        showStep('mfa', 'Enter the 6-digit code from your authenticator app (a backup code works too).');
        document.getElementById('mfa-code').focus();
      }
      return;
    }
    goNext();
  } catch (err) {
    errBox.textContent = 'Network error — is the server running?';
  }
});

async function submitCode(url, code, errBox) {
  errBox.textContent = '';
  try {
    const { resp, data } = await postJSON(url, { ticket: mfaTicket, code });
    if (!resp.ok) {
      errBox.textContent = data.detail || 'Code rejected';
      if (resp.status === 401 && /sign in again/.test(data.detail || '')) {
        // ticket burned or expired: back to the password step
        mfaTicket = null;
        showStep('password', 'Sign in to continue');
      }
      return;
    }
    goNext();
  } catch (err) {
    errBox.textContent = 'Network error — is the server running?';
  }
}

document.getElementById('mfa-form').addEventListener('submit', (e) => {
  e.preventDefault();
  submitCode('/auth/mfa/verify', document.getElementById('mfa-code').value.trim(),
    document.getElementById('login-err'));
});

document.getElementById('enroll-form').addEventListener('submit', (e) => {
  e.preventDefault();
  submitCode('/auth/mfa/ticket/confirm', document.getElementById('enroll-code').value.trim(),
    document.getElementById('login-err'));
});

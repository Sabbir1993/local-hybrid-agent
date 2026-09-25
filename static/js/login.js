/* ---------------- login.js (login.html) ---------------- */
// Same-origin paths only: next=javascript:... would run in this origin, next=//evil is a phish.
function safeNext(next) {
  if (!next || next[0] !== '/' || next[1] === '/' || next[1] === '\\') return '/';
  try {
    const u = new URL(next, location.origin);
    return u.origin === location.origin ? u.pathname + u.search + u.hash : '/';
  } catch (e) { return '/'; }
}

document.getElementById('login-form').addEventListener('submit', async (e) => {
  e.preventDefault();
  const errBox = document.getElementById('login-err');
  errBox.textContent = '';
  const username = document.getElementById('username').value.trim();
  const password = document.getElementById('password').value;
  try {
    const resp = await fetch('/auth/login', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      credentials: 'same-origin',
      body: JSON.stringify({ username, password }),
    });
    if (!resp.ok) {
      const data = await resp.json().catch(() => ({}));
      errBox.textContent = data.detail || 'Sign in failed';
      return;
    }
    location.href = safeNext(new URLSearchParams(location.search).get('next'));
  } catch (err) {
    errBox.textContent = 'Network error — is the server running?';
  }
});

/* ---------------- session.js ----------------
 * Loads once per page (ui.html and settings.html both include it):
 *   - fetches /auth/me, exposes window.__user / window.__perms
 *   - patches window.fetch so every existing JS file's fetch() calls
 *     automatically get the CSRF header on mutating requests and get
 *     redirected to /login on a 401, with zero per-file edits.
 */
(function () {
  function readCookie(name) {
    const m = document.cookie.match(new RegExp('(?:^|; )' + name + '=([^;]*)'));
    return m ? decodeURIComponent(m[1]) : null;
  }

  window.__perms = new Set();
  window.__user = null;

  const nativeFetch = window.fetch.bind(window);
  window.fetch = async function (input, init) {
    init = init || {};
    const method = (init.method || 'GET').toUpperCase();
    if (method !== 'GET' && method !== 'HEAD') {
      const csrf = readCookie('a770_csrf');
      if (csrf) {
        init.headers = new Headers(init.headers || {});
        init.headers.set('X-CSRF-Token', csrf);
      }
    }
    if (init.credentials === undefined) init.credentials = 'same-origin';
    const resp = await nativeFetch(input, init);
    if (resp.status === 401 && !String(input).includes('/auth/')) {
      location.href = '/login?next=' + encodeURIComponent(location.pathname);
    }
    return resp;
  };

  function buildUserMenu(user) {
    const trigger = document.getElementById('btn-user-menu');
    if (!trigger) return;

    const dd = document.createElement('div');
    dd.className = 'user-menu-dropdown';
    dd.innerHTML = `
      <div class="user-menu-email"></div>
      <button type="button" class="user-menu-item" data-action="change-pwd">🔑 Change Password</button>
      <div class="user-menu-pwd-form" style="display:none">
        <input type="password" placeholder="Current password" data-field="current" autocomplete="current-password">
        <input type="password" placeholder="New password" data-field="new" autocomplete="new-password">
        <input type="password" placeholder="Confirm new password" data-field="confirm" autocomplete="new-password">
        <div class="user-menu-msg"></div>
        <div class="row">
          <button type="button" class="btn ghost" data-action="pwd-cancel">Cancel</button>
          <button type="button" class="btn accent" data-action="pwd-save">Save</button>
        </div>
      </div>
      <button type="button" class="user-menu-item danger" data-action="logout">🚪 Logout</button>
    `;
    trigger.parentElement.style.position = 'relative';
    trigger.parentElement.classList.add('user-menu');
    trigger.parentElement.appendChild(dd);

    const emailEl = dd.querySelector('.user-menu-email');
    const label = user.display_name || user.username;
    emailEl.textContent = label;
    if (user.is_super_admin) {
      emailEl.innerHTML = label + '<br><span class="user-menu-badge">super admin</span>';
    }

    const pwdForm = dd.querySelector('.user-menu-pwd-form');
    const pwdMsg = dd.querySelector('.user-menu-msg');
    const resetPwdForm = () => {
      pwdForm.style.display = 'none';
      pwdMsg.textContent = '';
      pwdMsg.className = 'user-menu-msg';
      dd.querySelectorAll('.user-menu-pwd-form input').forEach(i => { i.value = ''; });
    };

    const closeMenu = () => { dd.classList.remove('open'); resetPwdForm(); };

    trigger.addEventListener('click', (e) => {
      e.stopPropagation();
      dd.classList.toggle('open');
    });
    document.addEventListener('click', (e) => {
      if (dd.classList.contains('open') && !dd.contains(e.target) && e.target !== trigger) closeMenu();
    });

    dd.addEventListener('click', (e) => {
      const action = e.target.getAttribute('data-action');
      if (!action) return;
      if (action === 'change-pwd') {
        pwdForm.style.display = pwdForm.style.display === 'none' ? 'flex' : 'none';
      } else if (action === 'pwd-cancel') {
        resetPwdForm();
      } else if (action === 'pwd-save') {
        const current = dd.querySelector('[data-field="current"]').value;
        const next = dd.querySelector('[data-field="new"]').value;
        const confirm_ = dd.querySelector('[data-field="confirm"]').value;
        if (!current || !next) {
          pwdMsg.textContent = 'Fill in both fields.';
          pwdMsg.className = 'user-menu-msg err';
          return;
        }
        if (next !== confirm_) {
          pwdMsg.textContent = 'New passwords do not match.';
          pwdMsg.className = 'user-menu-msg err';
          return;
        }
        pwdMsg.textContent = 'Saving…';
        pwdMsg.className = 'user-menu-msg';
        fetch('/auth/change_password', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ current_password: current, new_password: next }),
        }).then(async (r) => {
          const d = await r.json().catch(() => ({}));
          if (!r.ok) throw new Error(d.detail || 'failed');
          pwdMsg.textContent = 'Password changed.';
          pwdMsg.className = 'user-menu-msg ok';
          setTimeout(closeMenu, 1200);
        }).catch((err) => {
          pwdMsg.textContent = err.message;
          pwdMsg.className = 'user-menu-msg err';
        });
      } else if (action === 'logout') {
        fetch('/auth/logout', { method: 'POST' }).catch(() => {}).finally(() => {
          location.href = '/login';
        });
      }
    });
  }

  function applyPermGating(data) {
    const perms = new Set(data.permissions || []);
    const isSuper = !!(data.user && data.user.is_super_admin);
    document.querySelectorAll('[data-perm]').forEach(el => {
      const need = el.getAttribute('data-perm');
      if (!isSuper && !perms.has(need)) el.remove();
    });
  }

  window.__sessionReady = fetch('/auth/me').then(async (resp) => {
    if (!resp.ok) return null;
    const data = await resp.json();
    window.__user = data.user;
    window.__perms = new Set(data.permissions || []);
    applyPermGating(data);
    const who = document.getElementById('whoami');
    if (who && data.user) {
      who.textContent = data.user.display_name || data.user.username;
      who.title = 'Signed in as ' + (data.user.display_name || data.user.username)
        + (data.user.is_super_admin ? ' (super admin)' : '');
    }
    if (data.user) buildUserMenu(data.user);
    document.dispatchEvent(new CustomEvent('session-ready', { detail: data }));
    return data;
  }).catch(() => null);

  window.hasPerm = function (key) {
    return window.__perms.has(key);
  };
})();

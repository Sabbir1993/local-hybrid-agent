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

  // Idle logout (PCI DSS 8.2.8). Background polls send X-A770-Background so they
  // don't slide the server-side idle expiry; real input does (via touchServer).
  let idleMs = 15 * 60 * 1000;
  let lastActive = Date.now();
  let lastTouch = Date.now();
  let idleWarnEl = null;
  let loggingOut = false;

  const nativeFetch = window.fetch.bind(window);
  window.fetch = async function (input, init) {
    init = init || {};
    if (init.background) {
      init.headers = new Headers(init.headers || {});
      init.headers.set('X-A770-Background', '1');
      delete init.background;
    } else {
      lastTouch = Date.now();
    }
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
      emailEl.innerHTML = esc(label) + '<br><span class="user-menu-badge">super admin</span>';
    }

    const pwdForm = dd.querySelector('.user-menu-pwd-form');
    const pwdMsg = dd.querySelector('.user-menu-msg');
    if (user.must_change_password) {
      // first sign-in / admin reset: open the form straight away (PCI DSS 8.3.5)
      setTimeout(() => {
        dd.classList.add('open');
        pwdForm.style.display = 'flex';
        pwdMsg.textContent = 'Set a new password (12+ characters, letters and digits) to continue.';
        pwdMsg.className = 'user-menu-msg err';
      }, 0);
    }
    const resetPwdForm = () => {
      pwdForm.style.display = 'none';
      pwdMsg.textContent = '';
      pwdMsg.className = 'user-menu-msg';
      dd.querySelectorAll('.user-menu-pwd-form input').forEach(i => { i.value = ''; });
    };

    const closeMenu = () => {
      if (user.must_change_password) return;   // the server refuses everything else until it's done
      dd.classList.remove('open'); resetPwdForm();
    };

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
        if (user.must_change_password) return;
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
          if (user.must_change_password) { setTimeout(() => location.reload(), 800); return; }
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

  function idleLogout() {
    if (loggingOut) return;
    loggingOut = true;
    fetch('/auth/logout', { method: 'POST' }).catch(() => {}).finally(() => {
      location.href = '/login?next=' + encodeURIComponent(location.pathname);
    });
  }

  function hideIdleWarning() {
    if (idleWarnEl) { idleWarnEl.remove(); idleWarnEl = null; }
  }

  function showIdleWarning() {
    if (idleWarnEl) return;
    idleWarnEl = document.createElement('div');
    idleWarnEl.className = 'idle-warning';
    idleWarnEl.style.cssText = 'position:fixed;right:16px;bottom:16px;z-index:10000;padding:12px 14px;'
      + 'border-radius:8px;background:var(--panel,#222);color:var(--text,#eee);'
      + 'border:1px solid var(--border,#555);box-shadow:0 4px 16px rgba(0,0,0,.35);font-size:13px';
    const msg = document.createElement('div');
    msg.textContent = 'You will be signed out in 1 minute due to inactivity.';
    const btn = document.createElement('button');
    btn.type = 'button';
    btn.className = 'btn accent';
    btn.style.marginTop = '8px';
    btn.textContent = 'Stay signed in';
    btn.addEventListener('click', () => markActive(true));
    idleWarnEl.append(msg, btn);
    document.body.appendChild(idleWarnEl);
  }

  // Any real input (or a streaming job) counts as activity. The server session is
  // refreshed at most once a minute while active, so typing a long prompt without
  // sending anything doesn't let it lapse.
  function markActive(force) {
    const now = Date.now();
    // tabs share one session: publish activity so an idle tab doesn't sign out an active one
    if (now - lastActive > 5000) { try { localStorage.setItem('a770_last_active', String(now)); } catch (_) {} }
    lastActive = now;
    hideIdleWarning();
    if (force === true || Date.now() - lastTouch > 60 * 1000) {
      lastTouch = Date.now();
      nativeFetch('/auth/me', { credentials: 'same-origin' }).then(r => {
        if (r.status === 401) idleLogout();
      }).catch(() => {});
    }
  }
  window.markActive = markActive;

  function startIdleWatch() {
    ['keydown', 'pointerdown', 'wheel', 'touchstart'].forEach(ev =>
      document.addEventListener(ev, markActive, { passive: true, capture: true }));
    document.addEventListener('visibilitychange', () => {
      if (document.visibilityState === 'visible') checkIdle();
    });
    setInterval(checkIdle, 10 * 1000);
  }

  function checkIdle() {
    const shared = Number(localStorage.getItem('a770_last_active') || 0);
    if (shared > lastActive) { lastActive = shared; hideIdleWarning(); }
    const idle = Date.now() - lastActive;
    if (idle >= idleMs) idleLogout();
    else if (idle >= idleMs - 60 * 1000) showIdleWarning();
  }

  window.__sessionReady = fetch('/auth/me').then(async (resp) => {
    if (!resp.ok) return null;
    const data = await resp.json();
    if (data.idle_seconds) idleMs = data.idle_seconds * 1000;
    if (data.user) startIdleWatch();
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

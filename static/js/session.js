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

  function esc(s) {
    return String(s ?? '').replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;').replace(/'/g, '&#39;');
  }

  function navigateSettings(tab) {
    if (typeof window.openSettingsModal === 'function') {
      window.openSettingsModal(tab);
    } else if (typeof window.switchSettingsTab === 'function') {
      window.switchSettingsTab(tab);
    } else {
      location.href = tab ? `/settings#${tab}` : '/settings';
    }
  }

  function buildUserMenu(user) {
    const trigger = document.getElementById('btn-user-menu');
    if (!trigger) return;

    // Clean up any previously attached dropdown
    const existing = trigger.parentElement ? trigger.parentElement.querySelector('.user-menu-dropdown') : null;
    if (existing) existing.remove();

    const dd = document.createElement('div');
    dd.className = 'user-menu-dropdown';

    const label = user.display_name || user.username || 'User';
    const isSuper = !!user.is_super_admin;
    const canManageUsers = isSuper || (window.__perms && window.__perms.has('users.manage'));

    dd.innerHTML = `
      <div class="user-menu-email">
        <div style="font-weight:600; font-size:13px; color:var(--text);">${esc(label)}</div>
        ${isSuper ? '<span class="user-menu-badge">super admin</span>' : ''}
        ${user.username && user.display_name && user.display_name !== user.username ? `<div class="dim" style="font-size:11px; margin-top:2px;">@${esc(user.username)}</div>` : ''}
      </div>

      <button type="button" class="user-menu-item" data-action="theme" title="Quick cycle color theme">
        <svg class="um-ic" width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><circle cx="13.5" cy="6.5" r=".5" fill="currentColor"/><circle cx="17.5" cy="10.5" r=".5" fill="currentColor"/><circle cx="8.5" cy="7.5" r=".5" fill="currentColor"/><circle cx="6.5" cy="12.5" r=".5" fill="currentColor"/><path d="M12 2C6.5 2 2 6.5 2 12s4.5 10 10 10c.926 0 1.648-.746 1.648-1.688 0-.437-.18-.835-.437-1.125-.29-.289-.438-.652-.438-1.125a1.64 1.64 0 0 1 1.668-1.668h1.996c3.051 0 5.563-2.512 5.563-5.563C22 6.5 17.5 2 12 2z"/></svg>
        <span>Theme</span>
      </button>

      <button type="button" class="user-menu-item" data-action="docs" title="Connect external agents, view OpenAI API compatibility &amp; examples">
        <svg class="um-ic" width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M4 19.5v-15A2.5 2.5 0 0 1 6.5 2H20v20H6.5a2.5 2.5 0 0 1-2.5-2.5Z"/><path d="M6 6h10"/><path d="M6 10h10"/></svg>
        <span>Docs &amp; Guide</span>
      </button>

      <button type="button" class="user-menu-item" data-action="nav-settings" title="System Settings and Preferences">
        <svg class="um-ic" width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M12.22 2h-.44a2 2 0 0 0-2 2v.18a2 2 0 0 1-1 1.73l-.43.25a2 2 0 0 1-2 0l-.15-.08a2 2 0 0 0-2.73.73l-.22.38a2 2 0 0 0 .73 2.73l.15.1a2 2 0 0 1 1 1.72v.51a2 2 0 0 1-1 1.74l-.15.09a2 2 0 0 0-.73 2.73l.22.38a2 2 0 0 0 2.73.73l.15-.08a2 2 0 0 1 2 0l.43.25a2 2 0 0 1 1 1.73V20a2 2 0 0 0 2 2h.44a2 2 0 0 0 2-2v-.18a2 2 0 0 1 1-1.73l.43-.25a2 2 0 0 1 2 0l.15.08a2 2 0 0 0 2.73-.73l.22-.39a2 2 0 0 0-.73-2.73l-.15-.08a2 2 0 0 1-1-1.74v-.5a2 2 0 0 1 1-1.74l.15-.09a2 2 0 0 0 .73-2.73l-.22-.38a2 2 0 0 0-2.73-.73l-.15.08a2 2 0 0 1-2 0l-.43-.25a2 2 0 0 1-1-1.73V4a2 2 0 0 0-2-2z"/><circle cx="12" cy="12" r="3"/></svg>
        <span>Settings</span>
      </button>

      <button type="button" class="user-menu-item" data-action="nav-mfa" title="Configure Two-Factor Authentication (TOTP)">
        <svg class="um-ic" width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><rect width="18" height="11" x="3" y="11" rx="2" ry="2"/><path d="M7 11V7a5 5 0 0 1 10 0v4"/></svg>
        <span>Two-Factor Auth (2FA)</span>
      </button>

      ${canManageUsers ? `
      <button type="button" class="user-menu-item" data-action="nav-users" title="Manage Users and RBAC Roles">
        <svg class="um-ic" width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M16 21v-2a4 4 0 0 0-4-4H6a4 4 0 0 0-4 4v2"/><circle cx="9" cy="7" r="4"/><path d="M22 21v-2a4 4 0 0 0-3-3.87"/><path d="M16 3.13a4 4 0 0 1 0 7.75"/></svg>
        <span>Users &amp; Roles</span>
      </button>
      <button type="button" class="user-menu-item" data-action="nav-session" title="Manage Session Expiry &amp; Security Policy">
        <svg class="um-ic" width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><circle cx="12" cy="12" r="10"/><polyline points="12 6 12 12 16 14"/></svg>
        <span>Session Security</span>
      </button>` : ''}

      <div class="user-menu-sep"></div>

      <button type="button" class="user-menu-item" data-action="change-pwd">
        <svg class="um-ic" width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><circle cx="7.5" cy="15.5" r="4.5"/><path d="m10.7 12.3 9.8-9.8M17 6l3 3M14 9l2 2"/></svg>
        <span>Change password</span>
      </button>
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
      <div class="user-menu-sep"></div>
      <button type="button" class="user-menu-item danger" data-action="logout">
        <svg class="um-ic" width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M9 21H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h4"/><path d="m16 17 5-5-5-5"/><path d="M21 12H9"/></svg>
        <span>Log out</span>
      </button>
    `;

    trigger.parentElement.style.position = 'relative';
    trigger.parentElement.classList.add('user-menu');
    trigger.parentElement.appendChild(dd);

    const pwdForm = dd.querySelector('.user-menu-pwd-form');
    const pwdMsg = dd.querySelector('.user-menu-msg');
    if (user.must_change_password) {
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
      if (user.must_change_password) return;
      dd.classList.remove('open');
      resetPwdForm();
    };

    trigger.onclick = (e) => {
      e.stopPropagation();
      dd.classList.toggle('open');
    };

    document.addEventListener('click', (e) => {
      if (dd.classList.contains('open') && !dd.contains(e.target) && !trigger.contains(e.target)) {
        closeMenu();
      }
    });

    dd.addEventListener('click', (e) => {
      const item = e.target.closest('[data-action]');
      if (!item) return;
      const action = item.getAttribute('data-action');

      if (action === 'theme') {
        if (typeof cycleTheme === 'function') cycleTheme();
      } else if (action === 'docs') {
        closeMenu();
        if (typeof window.openDocsModal === 'function') {
          window.openDocsModal();
        } else {
          const dm = document.getElementById('docs-modal');
          if (dm) { dm.hidden = false; dm.style.display = 'flex'; }
        }
      } else if (action === 'nav-settings') {
        closeMenu();
        navigateSettings('sec-theme');
      } else if (action === 'nav-mfa') {
        closeMenu();
        navigateSettings('sec-mfa');
      } else if (action === 'nav-users') {
        closeMenu();
        navigateSettings('sec-users');
      } else if (action === 'nav-session') {
        closeMenu();
        navigateSettings('sec-session');
      } else if (action === 'change-pwd') {
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

  // A running job is not idle: a long agent step can go minutes without a byte on the stream, so a stream
  // holds the session while it is open (markActive on a timer, which also refreshes the server session).
  let holds = 0, holdTimer = null;
  window.holdSession = function () {
    if (++holds === 1) {
      markActive(true);
      holdTimer = setInterval(() => markActive(true), 30 * 1000);
    }
    let released = false;
    return function release() {
      if (released) return;
      released = true;
      if (--holds === 0) { clearInterval(holdTimer); holdTimer = null; markActive(false); }
    };
  };

  function checkIdle() {
    if (holds > 0) return;
    const shared = Number(localStorage.getItem('a770_last_active') || 0);
    if (shared > lastActive) { lastActive = shared; hideIdleWarning(); }
    const idle = Date.now() - lastActive;
    if (idle >= idleMs) idleLogout();
    else if (idle >= idleMs - 60 * 1000) showIdleWarning();
  }

  const earlyTrigger = document.getElementById('btn-user-menu');
  if (earlyTrigger) {
    earlyTrigger.onclick = (e) => {
      e.stopPropagation();
      const dd = document.querySelector('.user-menu-dropdown');
      if (dd) {
        dd.classList.toggle('open');
      } else {
        navigateSettings('sec-mfa');
      }
    };
  }

  window.__sessionReady = fetch('/auth/me').then(async (resp) => {
    if (!resp.ok) return null;
    const data = await resp.json();
    if (data.idle_seconds) idleMs = data.idle_seconds * 1000;
    // admin can turn idle sign-out off (Settings > Session security); the server then keeps the session to its absolute cap
    if (data.user && data.idle_enabled !== false) startIdleWatch();
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

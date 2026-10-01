/* ---------------- users & roles panel (settings.html only) ---------------- */
let _rbacRoles = [];
let _rbacPerms = [];
let _rbacModules = [];
let _rbacTab = 'users';

const _RB_INP = 'background:var(--panel2); color:var(--text); border:1px solid var(--border); border-radius:5px; padding:5px 8px; font-size:11.5px;';

async function loadUsersPanel() {
  const box = $('users-content');
  if (!box) return;
  box.innerHTML = '<div class="mon-empty">Loading…</div>';
  try {
    const [usersRes, rolesRes, permsRes] = await Promise.all([
      fetch('/admin/users'), fetch('/admin/roles'), fetch('/admin/permissions'),
    ]);
    if (!usersRes.ok || !rolesRes.ok) {
      box.innerHTML = '<div class="mon-empty">You do not have access to user management.</div>';
      return;
    }
    const users = (await usersRes.json()).users || [];
    _rbacRoles = (await rolesRes.json()).roles || [];
    const pj = permsRes.ok ? await permsRes.json() : {};
    _rbacPerms = pj.permissions || [];
    _rbacModules = pj.modules || [];
    renderUsersPanel(box, users);
    loadApiTokens(box.querySelector('#ru-pane-tokens'), users);
  } catch (e) {
    box.innerHTML = '<div class="mon-empty">Failed to load: ' + esc(e.message) + '</div>';
  }
}

function _rbacShowTab(box, tab) {
  _rbacTab = tab;
  box.querySelectorAll('.ru-tab').forEach(b => {
    const on = b.dataset.tab === tab;
    b.classList.toggle('on', on);
    b.setAttribute('aria-selected', on ? 'true' : 'false');
  });
  box.querySelectorAll('.ru-pane').forEach(p => { p.style.display = p.dataset.pane === tab ? '' : 'none'; });
}

function renderUsersPanel(box, users) {
  const roleOpts = _rbacRoles.map(r => `<option value="${esc(r.name)}">${esc(r.name)}</option>`).join('');
  const tabBtn = (id, label) => `<button type="button" role="tab" class="ru-tab btn ghost" data-tab="${id}"
    style="width:auto; margin:0; padding:6px 16px; font-size:12px; border-radius:6px 6px 0 0;">${label}</button>`;
  box.innerHTML = `
    <div role="tablist" class="ru-tabs" style="display:flex; gap:4px; border-bottom:1px solid var(--border); margin-bottom:12px;">
      ${tabBtn('users', 'Users')}${tabBtn('roles', 'Roles')}${tabBtn('tokens', 'Tokens')}
    </div>
    <div class="ru-pane" data-pane="users">
      <div class="cap-item" style="margin-bottom:10px;">
        <b>Create user</b>
        <div class="ru-create">
          <input type="text" id="ru-username" placeholder="username" autocomplete="off">
          <input type="password" id="ru-password" placeholder="password (8+ characters, letters and digits)" autocomplete="new-password">
          <select id="ru-role" aria-label="Role for the new user">${roleOpts}</select>
          <button class="ru-btn primary" id="ru-create" type="button">+ Create</button>
        </div>
      </div>
      <div id="ru-list" style="display:flex; flex-direction:column; gap:4px;">
        ${users.map(u => userRow(u)).join('') || '<div class="dim" style="font-size:11px;">No users yet.</div>'}
      </div>
    </div>
    <div class="ru-pane" data-pane="roles"></div>
    <div class="ru-pane" data-pane="tokens" id="ru-pane-tokens"></div>`;

  box.querySelectorAll('.ru-tab').forEach(b => { b.onclick = () => _rbacShowTab(box, b.dataset.tab); });
  renderRolesPane(box);
  _rbacShowTab(box, _rbacTab);
  _rbacWire(box);
}

function _rbacWire(box) {
  $('ru-create').onclick = async () => {
    const username = $('ru-username').value.trim();
    const password = $('ru-password').value;
    const role = $('ru-role').value;
    if (!username || !password) { toast('Username and password required', true); return; }
    try {
      const r = await fetch('/admin/users', {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ username, password, roles: role ? [role] : ['user'] }),
      });
      const j = await r.json().catch(() => ({}));
      if (!r.ok) throw new Error(j.detail || ('HTTP ' + r.status));
      toast(`User '${username}' created ✓`);
      loadUsersPanel();
    } catch (e) { toast('Create failed: ' + e.message, true); }
  };

  box.querySelectorAll('.ru-toggle-active').forEach(btn => {
    btn.onclick = async () => {
      const id = parseInt(btn.dataset.id);
      const active = btn.dataset.active === '1';
      try {
        await fetch(`/admin/users/${id}`, {
          method: 'PATCH', headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ is_active: !active }),
        });
        loadUsersPanel();
      } catch (e) { toast('Update failed', true); }
    };
  });

  box.querySelectorAll('.ru-delete').forEach(btn => {
    btn.onclick = async () => {
      if (!confirm('Delete this user? This cannot be undone.')) return;
      try {
        const r = await fetch(`/admin/users/${btn.dataset.id}`, { method: 'DELETE' });
        if (!r.ok) throw new Error('HTTP ' + r.status);
        loadUsersPanel();
      } catch (e) { toast('Delete failed: ' + e.message, true); }
    };
  });

  box.querySelectorAll('.ru-role-select').forEach(sel => {
    sel.onchange = async () => {
      try {
        await fetch(`/admin/users/${sel.dataset.id}`, {
          method: 'PATCH', headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ roles: [sel.value] }),
        });
        toast('Role updated ✓');
      } catch (e) { toast('Update failed', true); }
    };
  });
}

function userRow(u) {
  const roleSel = _rbacRoles.map(r =>
    `<option value="${esc(r.name)}" ${u.roles.includes(r.name) ? 'selected' : ''}>${esc(r.name)}</option>`).join('');
  const state = u.is_active ? '' : '<span class="ru-state">disabled</span>';
  const actions = u.is_super_admin
    ? '<span class="ru-role-fixed dim">all permissions</span>'
    : `<select class="ru-role-select" data-id="${u.id}" aria-label="Role for ${esc(u.username)}">${roleSel}</select>
       <button type="button" class="ru-btn ru-toggle-active" data-id="${u.id}" data-active="${u.is_active ? '1' : '0'}">${u.is_active ? 'Disable' : 'Enable'}</button>
       <button type="button" class="ru-btn ru-delete" data-id="${u.id}">Delete</button>`;
  return `<div class="ru-row${u.is_active ? '' : ' off'}">
    <span class="ru-name" title="${esc(u.username)}">${esc(u.username)}${u.is_super_admin ? '<span class="ru-badge">super admin</span>' : ''}${state}</span>
    <span class="ru-actions">${actions}</span>
  </div>`;
}

/* ---------------- roles: list on the left, the chosen role's permissions on the right ---------------- */
let _rbacRoleId = null;

const _RL_KIND = {
  read: { label: 'View', hint: 'Can look but not change anything' },
  write: { label: 'Change', hint: 'Can edit settings or data' },
  action: { label: 'Use', hint: 'Can start something or use a feature' },
};

/* Older servers send bare keys: group by the part before the first dot and make the key readable. */
function _rlPerms() {
  return _rbacPerms.map(p => p.module ? p : Object.assign({}, p, {
    module: (p.key.split('.')[0] || 'other'),
    kind: /\.view$/.test(p.key) ? 'read' : /\.(manage|configure|input_guard)$/.test(p.key) ? 'write' : 'action',
    title: p.key.replace(/[._]/g, ' ').replace(/^./, c => c.toUpperCase()),
    help: p.description || '',
  }));
}

function _rlModules(perms) {
  if (_rbacModules.length) return _rbacModules;
  return [...new Set(perms.map(p => p.module))].map(id => ({ id, label: id.replace(/^./, c => c.toUpperCase()) }));
}

function renderRolesPane(box) {
  const pane = box.querySelector('[data-pane="roles"]');
  if (!pane) return;
  if (!_rbacRoles.some(r => r.id === _rbacRoleId)) _rbacRoleId = (_rbacRoles[0] || {}).id;
  const role = _rbacRoles.find(r => r.id === _rbacRoleId);
  const perms = _rlPerms();
  const granted = new Set((role && role.permissions) || []);

  const rail = _rbacRoles.map(r => {
    const on = (r.permissions || []).length;
    return `<button type="button" class="rl-role${r.id === _rbacRoleId ? ' on' : ''}" data-role-pick="${r.id}">
      <span class="rl-role-name">${esc(r.name)}</span>
      <span class="rl-role-meta">${r.is_builtin ? 'Built in' : 'Custom'} · ${on} of ${perms.length} allowed</span>
    </button>`;
  }).join('');

  const sections = _rlModules(perms).map(m => {
    const inMod = perms.filter(p => p.module === m.id);
    if (!inMod.length) return '';
    const n = inMod.filter(p => granted.has(p.key)).length;
    const all = n === inMod.length;
    const rows = inMod.map(p => {
      const k = _RL_KIND[p.kind] || _RL_KIND.action;
      return `<label class="rl-perm">
        <input type="checkbox" class="rp-perm" data-role="${role.id}" data-perm="${esc(p.key)}" ${granted.has(p.key) ? 'checked' : ''}>
        <span class="rl-switch" aria-hidden="true"></span>
        <span class="rl-perm-text">
          <span class="rl-perm-title">${esc(p.title || p.key)}<span class="rl-tag rl-${esc(p.kind || 'action')}" title="${esc(k.hint)}">${k.label}</span></span>
          <span class="rl-perm-help">${esc(p.help || p.description || '')}</span>
        </span>
      </label>`;
    }).join('');
    return `<section class="rl-mod">
      <div class="rl-mod-head">
        <h4>${esc(m.label)}</h4>
        <span class="rl-count">${n} of ${inMod.length} allowed</span>
        <button type="button" class="rl-link" data-mod-toggle="${esc(m.id)}" data-all="${all ? '1' : '0'}">${all ? 'Clear all' : 'Allow all'}</button>
      </div>${rows}</section>`;
  }).join('');

  pane.innerHTML = `
    <div class="rl-layout">
      <nav class="rl-rail" aria-label="Roles">
        ${rail}
        <div class="rl-add">
          <input type="text" id="new-role-name" placeholder="New role name" maxlength="40">
          <button type="button" class="ru-btn" id="new-role-add">Add</button>
        </div>
      </nav>
      <div class="rl-detail">
        ${role ? `<div class="rl-detail-head">
          <h3>${esc(role.name)}</h3>
          <p>${role.is_builtin ? 'A built-in role. ' : ''}Switch on what people with this role may do. Changes save as you click.</p>
        </div>${sections}` : '<div class="dim">No roles yet.</div>'}
      </div>
    </div>`;

  pane.querySelectorAll('[data-role-pick]').forEach(b => {
    b.onclick = () => { _rbacRoleId = parseInt(b.dataset.rolePick); renderRolesPane(box); };
  });

  const patch = async (keys, on) => {
    const r = await fetch(`/admin/roles/${role.id}/permissions`, {
      method: 'PATCH', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(on ? { grant: keys } : { revoke: keys }),
    });
    if (!r.ok) throw new Error('HTTP ' + r.status);
    const set = new Set(role.permissions || []);
    keys.forEach(k => (on ? set.add(k) : set.delete(k)));
    role.permissions = [...set];
  };

  pane.querySelectorAll('.rp-perm').forEach(cb => {
    cb.onchange = async () => {
      try { await patch([cb.dataset.perm], cb.checked); renderRolesPane(box); }
      catch (e) { toast('Could not save that change', true); cb.checked = !cb.checked; }
    };
  });

  pane.querySelectorAll('[data-mod-toggle]').forEach(b => {
    b.onclick = async () => {
      const keys = perms.filter(p => p.module === b.dataset.modToggle).map(p => p.key);
      const on = b.dataset.all !== '1';
      try { await patch(keys, on); renderRolesPane(box); }
      catch (e) { toast('Could not save that change', true); }
    };
  });

  const add = pane.querySelector('#new-role-add');
  if (add) add.onclick = async () => {
    const name = pane.querySelector('#new-role-name').value.trim();
    if (!name) return;
    try {
      const r = await fetch('/admin/roles', {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ name }),
      });
      if (!r.ok) { const j = await r.json().catch(() => ({})); throw new Error(j.detail || 'failed'); }
      toast(`Role '${name}' added`);
      loadUsersPanel();
    } catch (e) { toast('Could not add the role: ' + e.message, true); }
  };
}


/* ---------------- API tokens (admin-issued, users.manage) ---------------- */
const _TOK_INP = 'background:var(--bg-input); color:var(--text); border:1px solid var(--border); border-radius:7px; padding:7px 10px; font-size:12px; box-sizing:border-box;';

async function loadApiTokens(box, users) {
  const sec = document.createElement('div');
  sec.className = 'cap-item';
  if (!box) return;
  box.appendChild(sec);
  let data;
  try {
    const r = await fetch('/admin/api-tokens');
    if (!r.ok) { sec.textContent = 'You do not have access to API tokens.'; return; }
    data = await r.json();
  } catch (e) { sec.textContent = 'Could not load tokens.'; return; }
  const fmt = t => t ? new Date(t * 1000).toLocaleString() : '—';
  const now = Date.now() / 1000;
  const userOpts = users.filter(u => u.is_active).map(u =>
    `<option value="${esc(u.id)}">${esc(u.display_name || u.username)}</option>`).join('');
  const permBoxes = (data.grantable || []).map(p =>
    `<label style="font-size:11px; display:inline-flex; gap:4px; align-items:center; margin-right:10px;">
       <input type="checkbox" class="tok-perm" value="${esc(p)}"${p === 'chat.use' ? ' checked' : ''}> ${esc(p)}</label>`).join('');
  const rows = (data.tokens || []).map(t => {
    const state = t.revoked_at ? 'revoked' : (t.expires_at <= now ? 'expired' : 'active');
    return `<div style="display:flex; gap:8px; align-items:center; font-size:11px; padding:4px 0; border-bottom:1px solid var(--border);">
      <code>${esc(t.prefix)}…</code><b>${esc(t.name)}</b>
      <span class="dim">user: ${esc(t.username || t.user_id)}</span>
      <span class="dim">expires ${esc(fmt(t.expires_at))}</span>
      <span class="dim">last used ${esc(fmt(t.last_used_at))}</span>
      <span class="dim" title="${esc((t.permissions || []).join(', '))}">${esc((t.permissions || []).length)} perm(s)</span>
      <span style="margin-left:auto;">${esc(state)}</span>
      ${state === 'active' ? `<button class="btn ghost tok-revoke" data-id="${esc(t.id)}" style="width:auto; margin:0; padding:2px 8px; font-size:11px;">Revoke</button>` : ''}
    </div>`;
  }).join('') || '<div class="dim" style="font-size:11px;">No API tokens yet.</div>';
  sec.innerHTML = `
    <b>API tokens</b>
    <div class="dim" style="font-size:11px; margin:4px 0 8px;">For scripts and the OpenAPI docs at <a href="/docs" target="_blank" rel="noopener">/docs</a>. Send as <code>Authorization: Bearer &lt;token&gt;</code>. A token acts as its user, limited to the permissions picked here; user, role and database administration can never be granted.</div>
    <div style="display:flex; gap:6px; flex-wrap:wrap;">
      <input type="text" id="tok-name" placeholder="token name (e.g. reporting-script)" maxlength="80" style="flex:1; min-width:160px; ${_TOK_INP}">
      <select id="tok-user" style="${_TOK_INP}">${userOpts}</select>
      <select id="tok-days" style="${_TOK_INP}">
        <option value="7">7 days</option><option value="30" selected>30 days</option><option value="90">90 days</option>
      </select>
      <button class="btn accent" id="tok-create" style="width:auto; margin:0; padding:5px 12px; font-size:11.5px;">+ Issue token</button>
    </div>
    <div style="margin:6px 0;">${permBoxes}</div>
    <div id="tok-new" style="display:none; margin:6px 0; padding:8px; border:1px solid var(--accent); border-radius:6px; font-size:11.5px;"></div>
    <div>${rows}</div>`;

  sec.querySelector('#tok-create').onclick = async () => {
    const name = sec.querySelector('#tok-name').value.trim();
    const permissions = [...sec.querySelectorAll('.tok-perm:checked')].map(c => c.value);
    if (!name) { toast('Give the token a name', true); return; }
    try {
      const r = await fetch('/admin/api-tokens', {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ name, permissions,
                               user_id: parseInt(sec.querySelector('#tok-user').value),
                               expires_days: parseInt(sec.querySelector('#tok-days').value) }),
      });
      const j = await r.json().catch(() => ({}));
      if (!r.ok) throw new Error(j.detail || ('HTTP ' + r.status));
      const out = sec.querySelector('#tok-new');
      out.style.display = 'block';
      out.textContent = '';
      const msg = document.createElement('div');
      msg.textContent = 'Copy this token now — it will not be shown again:';
      const code = document.createElement('code');
      code.style.cssText = 'display:block; margin:6px 0; word-break:break-all; user-select:all;';
      code.textContent = j.token;
      const copy = document.createElement('button');
      copy.className = 'btn ghost';
      copy.style.cssText = 'width:auto; margin:0; padding:2px 10px; font-size:11px;';
      copy.textContent = 'Copy';
      copy.onclick = () => navigator.clipboard.writeText(j.token).then(() => toast('Copied ✓'));
      out.append(msg, code, copy);
      toast('Token issued ✓');
    } catch (e) { toast('Issue failed: ' + e.message, true); }
  };
  sec.querySelectorAll('.tok-revoke').forEach(btn => {
    btn.onclick = async () => {
      if (!confirm('Revoke this API token? Scripts using it will stop working.')) return;
      const r = await fetch('/admin/api-tokens/' + parseInt(btn.dataset.id), { method: 'DELETE' });
      if (r.ok) { toast('Token revoked'); loadUsersPanel(); } else toast('Revoke failed', true);
    };
  });
}

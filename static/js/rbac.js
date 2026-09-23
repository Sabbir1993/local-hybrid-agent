/* ---------------- users & roles panel (settings.html only) ---------------- */
let _rbacRoles = [];
let _rbacPerms = [];

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
    _rbacPerms = permsRes.ok ? ((await permsRes.json()).permissions || []) : [];
    renderUsersPanel(box, users);
  } catch (e) {
    box.innerHTML = '<div class="mon-empty">Failed to load: ' + esc(e.message) + '</div>';
  }
}

function renderUsersPanel(box, users) {
  const roleOpts = _rbacRoles.map(r => `<option value="${esc(r.name)}">${esc(r.name)}</option>`).join('');
  box.innerHTML = `
    <div class="cap-item" style="margin-bottom:10px;">
      <b>Create user</b>
      <div style="display:flex; gap:6px; flex-wrap:wrap; margin-top:6px;">
        <input type="text" id="ru-username" placeholder="username" style="flex:1; min-width:120px; background:var(--panel2); color:var(--text); border:1px solid var(--border); border-radius:5px; padding:5px 8px; font-size:11.5px;">
        <input type="password" id="ru-password" placeholder="password" style="flex:1; min-width:120px; background:var(--panel2); color:var(--text); border:1px solid var(--border); border-radius:5px; padding:5px 8px; font-size:11.5px;">
        <select id="ru-role" style="background:var(--panel2); color:var(--text); border:1px solid var(--border); border-radius:5px; padding:5px 8px; font-size:11.5px;">${roleOpts}</select>
        <button class="btn accent" id="ru-create" style="width:auto; margin:0; padding:5px 12px; font-size:11.5px;">+ Create</button>
      </div>
    </div>
    <div id="ru-list" style="display:flex; flex-direction:column; gap:4px; margin-bottom:12px;">
      ${users.map(u => userRow(u)).join('') || '<div class="dim" style="font-size:11px;">No users yet.</div>'}
    </div>
    <div class="cap-item">
      <b>Role permissions</b>
      <div id="rp-roles" style="display:flex; flex-direction:column; gap:8px; margin-top:8px;">
        ${_rbacRoles.map(r => roleBlock(r)).join('')}
      </div>
      <div style="display:flex; gap:6px; margin-top:8px;">
        <input type="text" id="new-role-name" placeholder="new role name" style="flex:1; background:var(--panel2); color:var(--text); border:1px solid var(--border); border-radius:5px; padding:5px 8px; font-size:11.5px;">
        <button class="btn ghost" id="new-role-add" style="width:auto; margin:0; padding:5px 10px; font-size:11.5px;">+ Add role</button>
      </div>
    </div>`;

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

  if ($('new-role-add')) $('new-role-add').onclick = async () => {
    const name = $('new-role-name').value.trim();
    if (!name) return;
    try {
      const r = await fetch('/admin/roles', {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ name }),
      });
      if (!r.ok) { const j = await r.json().catch(() => ({})); throw new Error(j.detail || 'failed'); }
      toast(`Role '${name}' created ✓`);
      loadUsersPanel();
    } catch (e) { toast('Add role failed: ' + e.message, true); }
  };

  box.querySelectorAll('.rp-perm').forEach(cb => {
    cb.onchange = async () => {
      const roleId = parseInt(cb.dataset.role);
      const key = cb.dataset.perm;
      const body = cb.checked ? { grant: [key] } : { revoke: [key] };
      try {
        await fetch(`/admin/roles/${roleId}/permissions`, {
          method: 'PATCH', headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify(body),
        });
      } catch (e) { toast('Permission update failed', true); cb.checked = !cb.checked; }
    };
  });
}

function userRow(u) {
  const roleSel = _rbacRoles.map(r =>
    `<option value="${esc(r.name)}" ${u.roles.includes(r.name) ? 'selected' : ''}>${esc(r.name)}</option>`).join('');
  return `<div class="cap-item" style="display:flex; align-items:center; gap:8px; flex-wrap:wrap;">
    <span style="flex:1; min-width:100px;"><b>${esc(u.username)}</b>${u.is_super_admin ? ' <span class="dim">(super admin)</span>' : ''}
      ${!u.is_active ? ' <span style="color:var(--red);">disabled</span>' : ''}</span>
    ${u.is_super_admin ? '' : `<select class="ru-role-select" data-id="${u.id}" style="background:var(--panel2); color:var(--text); border:1px solid var(--border); border-radius:5px; padding:3px 6px; font-size:10.5px;">${roleSel}</select>
    <button class="btn ghost ru-toggle-active" data-id="${u.id}" data-active="${u.is_active ? '1' : '0'}" style="width:auto; margin:0; padding:2px 8px; font-size:10.5px;">${u.is_active ? 'Disable' : 'Enable'}</button>
    <button class="btn ghost ru-delete" data-id="${u.id}" style="width:auto; margin:0; padding:2px 8px; font-size:10.5px; color:var(--red);">Delete</button>`}
  </div>`;
}

function roleBlock(role) {
  const perms = new Set(role.permissions || []);
  return `<div class="cap-item">
    <b>${esc(role.name)}</b>${role.is_builtin ? ' <span class="dim">(builtin)</span>' : ''}
    <div style="display:grid; grid-template-columns:1fr 1fr; gap:4px; margin-top:5px;">
      ${_rbacPerms.map(p => `
        <label style="display:flex; align-items:center; gap:5px; font-size:10.5px; cursor:pointer;">
          <input type="checkbox" class="rp-perm" data-role="${role.id}" data-perm="${esc(p.key)}" ${perms.has(p.key) ? 'checked' : ''}>
          <span title="${esc(p.description || '')}">${esc(p.key)}</span>
        </label>`).join('')}
    </div>
  </div>`;
}


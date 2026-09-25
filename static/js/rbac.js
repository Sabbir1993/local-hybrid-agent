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
    loadApiTokens(box, users);
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


/* ---------------- API tokens (admin-issued, users.manage) ---------------- */
const _TOK_INP = 'background:var(--panel2); color:var(--text); border:1px solid var(--border); border-radius:5px; padding:5px 8px; font-size:11.5px;';

async function loadApiTokens(box, users) {
  const sec = document.createElement('div');
  sec.className = 'cap-item';
  sec.style.marginTop = '12px';
  box.appendChild(sec);
  let data;
  try {
    const r = await fetch('/admin/api-tokens');
    if (!r.ok) { sec.remove(); return; }
    data = await r.json();
  } catch (e) { sec.remove(); return; }
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

/* ---------------- settings-page.js (settings.html page controller) ----------------
 * Moved out of an inline <script> so the CSP can forbid inline script. */

// --- Tab Navigation & View Management ---
function switchSettingsTab(tabId, updateHash = true) {
  if (!tabId) return;
  const panel = document.getElementById(tabId);
  const btn = document.querySelector(`.settings-tab-btn[data-tab="${tabId}"]`);
  if (!panel || !btn) return;

  document.querySelectorAll('.settings-tab-btn').forEach(b => b.classList.remove('active'));
  document.querySelectorAll('.settings-panel').forEach(p => p.classList.remove('active'));

  btn.classList.add('active');
  panel.classList.add('active');

  localStorage.setItem('a770_active_settings_tab', tabId);
  if (updateHash) {
    try {
      history.replaceState(null, '', '#' + tabId);
    } catch (_) {}
  }

  // Trigger special actions
  if (tabId === 'sec-gpus' && typeof triggerGpuQuery === 'function') {
    triggerGpuQuery();
  }
}
window.switchSettingsTab = switchSettingsTab;

function cleanupEmptyNavGroups() {
  document.querySelectorAll('.settings-group-label').forEach(label => {
    let next = label.nextElementSibling;
    let hasVisible = false;
    while (next && !next.classList.contains('settings-group-label')) {
      if (next.classList.contains('settings-tab-btn') && next.offsetParent !== null && next.style.display !== 'none') {
        hasVisible = true;
        break;
      }
      next = next.nextElementSibling;
    }
    label.style.display = hasVisible ? '' : 'none';
  });
}

// Bind tab click events
document.querySelectorAll('.settings-tab-btn').forEach(btn => {
  btn.onclick = () => {
    const tabId = btn.getAttribute('data-tab');
    switchSettingsTab(tabId);
  };
});

// Search filter in tabs
const searchInput = document.getElementById('settings-tab-search');
if (searchInput) {
  searchInput.addEventListener('input', (e) => {
    const q = e.target.value.toLowerCase().trim();
    document.querySelectorAll('.settings-tab-btn').forEach(b => {
      const text = b.textContent.toLowerCase();
      b.style.display = (!q || text.includes(q)) ? 'flex' : 'none';
    });
    cleanupEmptyNavGroups();
  });
}

// Support browser back/forward and deep hash links
window.addEventListener('hashchange', () => {
  const hash = location.hash ? location.hash.replace(/^#/, '') : '';
  if (hash) {
    const targetId = hash.startsWith('sec-') ? hash : ('sec-' + hash);
    if (document.getElementById(targetId)) {
      switchSettingsTab(targetId, false);
    }
  }
});



// --- GPU Status Query Logic ---
const secGpu = document.getElementById('sec-gpus');
const btnRefreshGpu = document.getElementById('btn-refresh-gpu');
async function triggerGpuQuery() {
  if (!secGpu || !document.getElementById('gpu-list')) return;
  const glist = document.getElementById('gpu-list');
  if (btnRefreshGpu) {
    btnRefreshGpu.disabled = true;
    btnRefreshGpu.textContent = 'Querying…';
  }
  if (glist && !glist.querySelector('.gpu')) {
    glist.innerHTML = '<div class="dim" style="font-size:12px">querying discrete GPUs…</div>';
  }
  try {
    await pollGpu();
  } finally {
    if (btnRefreshGpu) {
      btnRefreshGpu.disabled = false;
      btnRefreshGpu.textContent = '⚡ Check GPU';
    }
  }
}
if (btnRefreshGpu) {
  btnRefreshGpu.onclick = (e) => {
    e.stopPropagation();
    e.preventDefault();
    triggerGpuQuery();
  };
}

// --- Session & Permissions Ready ---
window.__sessionReady.then((data) => {
  if (!data) return;   // not authenticated -- fetch() 401 handler already redirects to /login

  // Clean empty category titles after applyPermGating removed unauthorized tabs
  cleanupEmptyNavGroups();

  // Determine initial tab from hash or localStorage or default to sec-theme
  let targetTab = location.hash ? location.hash.replace(/^#/, '') : localStorage.getItem('a770_active_settings_tab');
  if (targetTab && !targetTab.startsWith('sec-') && document.getElementById('sec-' + targetTab)) {
    targetTab = 'sec-' + targetTab;
  }
  const targetBtn = targetTab ? document.querySelector(`.settings-tab-btn[data-tab="${targetTab}"]`) : null;
  if (targetBtn && document.getElementById(targetTab)) {
    switchSettingsTab(targetTab, false);
  } else {
    const firstVisibleBtn = document.querySelector('.settings-tab-btn');
    if (firstVisibleBtn) {
      switchSettingsTab(firstVisibleBtn.getAttribute('data-tab'), false);
    }
  }

  // Permission-based panel initializers
  if (document.getElementById('sec-cfg')) {
    loadProfiles();
  }
  if (document.getElementById('sec-sampling')) initSamplingPanel();
  if (document.getElementById('sec-caps')) loadCapabilities();
  if (document.getElementById('cloud-content')) loadCloudCard();
  if (document.getElementById('devices-content')) loadDevicesPanel();
  if (document.getElementById('sec-users')) loadUsersPanel();
  if (document.getElementById('sec-knowledge')) loadKnowledgePanel();
  if (document.getElementById('sec-guard')) loadInputGuardPanel();
  if (document.getElementById('sec-db')) {
    fetch('/db/list').then(r => r.json()).then(d => {
      const box = document.getElementById('settings-db-summary');
      if (!box) return;
      const dbs = d.databases || [];
      box.innerHTML = dbs.map(db => `
        <div class="cap-item" style="display:flex; justify-content:space-between; align-items:center;">
          <div>
            <b>${esc(db.name)}</b> <span class="dim">(${esc(db.title)})</span>
            <div class="dim" style="font-size:10.5px;">${esc(db.description)}</div>
          </div>
          <span class="mono dim" style="font-size:11px;">${db.table_count} tables · ${(db.size_bytes / 1024).toFixed(1)} KB</span>
        </div>
      `).join('') || '<div class="dim" style="font-size:11px;">No databases found.</div>';
    }).catch(e => {
      const box = document.getElementById('settings-db-summary');
      if (box) box.innerHTML = `<div class="dim" style="color:var(--red); font-size:11px;">${esc(e.message)}</div>`;
    });
  }
  const clearBtn = document.getElementById('btn-clear-all-data');
  if (clearBtn) {
    clearBtn.onclick = async () => {
      const typed = prompt('This permanently deletes every user\'s chats, sessions, projects and workspace files.\n\nType DELETE to confirm:');
      if (typed !== 'DELETE') return;
      clearBtn.disabled = true;
      const out = document.getElementById('clear-all-data-result');
      out.textContent = 'Clearing…';
      try {
        const r = await fetch('/admin/clear_all_data', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ confirm: typed }),
        });
        const d = await r.json();
        if (!r.ok) throw new Error(d.detail || 'failed');
        out.textContent = `Done: removed ${d.cleared.projects} projects, ${d.cleared.sessions} sessions, `
          + `${d.cleared.messages} messages, ${d.cleared.plan_items} plan items.`;
      } catch (e) {
        out.textContent = `Error: ${e.message}`;
      } finally {
        clearBtn.disabled = false;
      }
    };
  }
});
pollStatus();

// Theme picker + DB console link (formerly inline onclick handlers)
document.querySelectorAll('.theme-opt[data-t]').forEach(btn => {
  btn.addEventListener('click', () => setTheme(btn.getAttribute('data-t')));
});
document.querySelectorAll('[data-open-db]').forEach(a => {
  a.addEventListener('click', () => localStorage.setItem('a770_open_db', '1'));
});

// Companion devices: the user's own paired machines (admins see everyone's)
async function loadDevicesPanel() {
  const box = document.getElementById('devices-content');
  if (!box) return;
  const isAdmin = window.hasPerm && (window.hasPerm('users.manage') || (window.__user && window.__user.is_super_admin));
  try {
    const r = await fetch('/companion/devices' + (isAdmin ? '?all=true' : ''));
    const d = await r.json();
    if (!r.ok) throw new Error(d.detail || ('HTTP ' + r.status));
    const fmt = t => t ? new Date(t * 1000).toLocaleString() : '—';
    const rows = (d.devices || []).map(x => `
      <div class="cap-item" style="display:flex; gap:10px; align-items:center; font-size:12px;">
        <span class="cap-dot ${x.connected ? 'on' : ''}" title="${x.connected ? 'connected now' : 'not connected'}"></span>
        <b>${esc(x.name)}</b>
        ${isAdmin ? `<span class="dim">${esc(x.username || x.user_id)}</span>` : ''}
        <span class="dim">paired ${esc(fmt(x.created_at))}</span>
        <span class="dim">last seen ${esc(fmt(x.last_seen_at))}</span>
        <span style="margin-left:auto;">${x.revoked_at ? 'revoked' : ''}</span>
        ${x.revoked_at ? '' : `<button class="btn ghost dev-revoke" data-id="${esc(x.id)}" style="width:auto; margin:0; padding:2px 10px; font-size:11px; color:var(--red);">Revoke</button>`}
      </div>`).join('');
    box.innerHTML = rows || '<div class="dim" style="font-size:12px;">No paired devices yet. Sign in inside the A770 Companion app and it pairs itself.</div>';
    box.querySelectorAll('.dev-revoke').forEach(btn => {
      btn.addEventListener('click', async () => {
        if (!confirm('Revoke this device? Its companion disconnects and must be signed in again.')) return;
        const rr = await fetch('/companion/devices/' + parseInt(btn.dataset.id, 10), { method: 'DELETE' });
        if (rr.ok) { toast('Device revoked'); loadDevicesPanel(); } else toast('Revoke failed', true);
      });
    });
  } catch (e) {
    box.innerHTML = '<div class="dim" style="font-size:12px;">Failed to load devices: ' + esc(e.message) + '</div>';
  }
}

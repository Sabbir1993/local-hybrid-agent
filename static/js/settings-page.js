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
    let reportedTab = tabId;
    if (tabId === 'sec-caps') {
      try {
        const sub = window._capTab || localStorage.getItem('a770_active_cap_tab');
        if (sub) reportedTab = 'sec-caps/' + sub;
      } catch (_) {}
    }
    try {
      history.replaceState(null, '', '#' + reportedTab);
    } catch (_) {}
    try {
      if (window.parent && window.parent !== window) {
        window.parent.postMessage({ type: 'settings-tab-change', tab: reportedTab }, '*');
      }
    } catch (_) {}
    try {
      if (window.parent && window.parent !== window) {
        window.parent.history.replaceState(null, '', window.parent.location.pathname + '#' + reportedTab);
      }
    } catch (_) {}
  }

  // Trigger special actions
  if (tabId === 'sec-live-monitor') {
    if (typeof isTelemetryPermitted === 'function' && !isTelemetryPermitted()) {
      return;
    }
    if (typeof startTelemetryMonitoring === 'function') startTelemetryMonitoring();
    if (typeof triggerGpuQuery === 'function') triggerGpuQuery();
  } else {
    if (typeof stopTelemetryMonitoring === 'function') stopTelemetryMonitoring();
  }

  if (tabId === 'sec-gpus' && typeof triggerGpuQuery === 'function') {
    triggerGpuQuery();
  }

  if (tabId === 'sec-profile' && typeof window.populateAccountPanels === 'function') {
    window.populateAccountPanels();
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
  // The page has password fields (MFA, new users), so Chrome treats the first text box above them as a login username
  // and fills in the saved account name - ignoring autocomplete="off". A value the user did not type is cleared.
  let searchTyped = false;
  const markTyped = () => { searchTyped = true; };
  searchInput.addEventListener('keydown', markTyped);
  searchInput.addEventListener('paste', markTyped);
  searchInput.addEventListener('drop', markTyped);
  const dropAutofill = () => {
    if (!searchTyped && searchInput.value) {
      searchInput.value = '';
      searchInput.dispatchEvent(new Event('input'));
    }
  };
  [0, 300, 1000, 2500].forEach((ms) => setTimeout(dropAutofill, ms));
  searchInput.addEventListener('input', (e) => {
    if (!searchTyped && e.isTrusted && searchInput.value) { dropAutofill(); return; }   // autofill, not the user
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
    let targetId = hash;
    let subTab = '';
    if (targetId.includes('/') || targetId.includes(':')) {
      const parts = targetId.split(/[\/:]/);
      targetId = parts[0];
      subTab = parts[1];
    }
    if (!targetId.startsWith('sec-') && document.getElementById('sec-' + targetId)) {
      targetId = 'sec-' + targetId;
    }
    if (document.getElementById(targetId)) {
      switchSettingsTab(targetId, false);
    }
    if (targetId === 'sec-caps' && subTab && typeof window._capShowTab === 'function') {
      window._capShowTab(subTab, false);
    }
  }
});

// React host can tell the iframe to switch tabs on browser back/forward or external actions
window.addEventListener('message', (e) => {
  if (e.data && e.data.type === 'navigate-tab' && e.data.tab) {
    let targetId = e.data.tab;
    let subTab = '';
    if (targetId.includes('/') || targetId.includes(':')) {
      const parts = targetId.split(/[\/:]/);
      targetId = parts[0];
      subTab = parts[1];
    }
    if (!targetId.startsWith('sec-') && document.getElementById('sec-' + targetId)) {
      targetId = 'sec-' + targetId;
    }
    if (targetId && document.getElementById(targetId)) {
      switchSettingsTab(targetId, false);
    }
    if (targetId === 'sec-caps' && subTab && typeof window._capShowTab === 'function') {
      window._capShowTab(subTab, false);
    }
  }
});

document.addEventListener('visibilitychange', () => {
  if (document.hidden) {
    if (typeof stopTelemetryMonitoring === 'function') stopTelemetryMonitoring();
  } else {
    const activePanel = document.querySelector('.settings-panel.active');
    if (activePanel && activePanel.id === 'sec-live-monitor') {
      if (typeof startTelemetryMonitoring === 'function') startTelemetryMonitoring();
    if (typeof triggerGpuQuery === 'function') triggerGpuQuery();
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
  let rawHash = '';
  try {
    rawHash = (location.hash || (window.parent && window.parent.location.hash) || '').replace(/^#/, '');
  } catch (_) {
    rawHash = location.hash ? location.hash.replace(/^#/, '') : '';
  }
  let targetTab = rawHash;
  if (targetTab.includes('/') || targetTab.includes(':')) {
    targetTab = targetTab.split(/[\/:]/)[0];
  }
  if (!targetTab) {
    targetTab = localStorage.getItem('a770_active_settings_tab') || 'sec-theme';
  }
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
  if (document.getElementById('sec-subagents')) initSubagentsPanel();
  if (document.getElementById('cloud-content')) loadCloudCard();
  if (document.getElementById('devices-content')) loadDevicesPanel();
  if (document.getElementById('sec-users')) loadUsersPanel();
  if (document.getElementById('sec-knowledge')) loadKnowledgePanel();
  if (document.getElementById('sec-custom-agents') && typeof loadCustomAgents === 'function') loadCustomAgents();
  if (document.getElementById('sec-guard')) loadInputGuardPanel();
  if (document.getElementById('sec-session') && window.loadSessionPolicyPanel) loadSessionPolicyPanel();
  if (document.getElementById('sec-runner') && window.loadRunnerPanel) loadRunnerPanel();
  if (document.getElementById('sec-mfa') && window.loadMfaPanel) loadMfaPanel();
  if (document.getElementById('sec-speech') && window.loadSpeechCorpusPanel) loadSpeechCorpusPanel();
  if (document.getElementById('speech-review-content') && window.loadSpeechReviewPanel) loadSpeechReviewPanel();
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
  btn.addEventListener('click', () => {
    setTheme(btn.getAttribute('data-t'));
    if (window.saveAppearance) window.saveAppearance({ theme: btn.getAttribute('data-t') });
  });
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
      <div class="cap-item" style="display:flex; flex-wrap:wrap; gap:6px 10px; align-items:center; font-size:12px; min-width:0;">
        <span class="cap-dot ${x.connected ? 'on' : ''}" title="${x.connected ? 'connected now' : 'not connected'}"></span>
        <b>${esc(x.name)}</b>
        ${isAdmin ? `<span class="dim" style="overflow-wrap:anywhere;">${esc(x.username || x.user_id)}</span>` : ''}
        <span class="dim">paired ${esc(fmt(x.created_at))}</span>
        <span class="dim">last seen ${esc(fmt(x.last_seen_at))}</span>
        <span style="margin-left:auto;">${x.revoked_at ? 'revoked' : ''}</span>
        ${x.revoked_at ? '' : `<button class="btn ghost dev-revoke" data-id="${esc(x.id)}" style="width:auto; margin:0; padding:2px 10px; font-size:11px; color:var(--red);">Revoke</button>`}
      </div>`).join('');
    box.innerHTML = rows || '<div class="dim" style="font-size:12px;">No paired devices yet. Sign in inside the SSL Local Agent app and it pairs itself.</div>';
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

// Standalone page (opened by a direct link such as /settings#sec-lanes, e.g. in the companion window): offer a way back.
// Inside the Settings modal the page is an iframe and the modal has its own close, so the button stays hidden there.
(function () {
  // settings-nav.js already made the nav visible in standalone mode from <head>.
  if (window.self === window.top) {
    const refresh = document.getElementById('settings-refresh');
    if (refresh) refresh.onclick = () => window.location.reload();
  }
})();

// --- Look and feel helpers (see settings-pro.css): nothing here changes what a setting does ---
(function () {
  // 1. each section header: the leading emoji becomes an icon tile, and the nav group names the section's home
  const emojiRx = /^(\p{Extended_Pictographic}(?:️|‍\p{Extended_Pictographic})*)\s*/u;
  document.querySelectorAll('.settings-panel > .settings-panel-head').forEach((head) => {
    const h2 = head.querySelector('h2');
    if (h2 && !h2.querySelector('.sp-ico')) {
      const first = h2.firstChild;
      if (first && first.nodeType === 3) {
        const m = emojiRx.exec(first.nodeValue);
        if (m) {
          const ico = document.createElement('span');
          ico.className = 'sp-ico';
          ico.setAttribute('aria-hidden', 'true');
          ico.textContent = m[1];
          first.nodeValue = first.nodeValue.slice(m[0].length);
          h2.insertBefore(ico, first);
        }
      }
    }
    const panel = head.closest('.settings-panel');
    const btn = panel && document.querySelector(`.settings-tab-btn[data-tab="${panel.id}"]`);
    let group = btn && btn.previousElementSibling;
    while (group && !group.classList.contains('settings-group-label')) group = group.previousElementSibling;
    if (group && h2 && !head.querySelector('.sp-crumb')) {
      const crumb = document.createElement('span');
      crumb.className = 'sp-crumb';
      crumb.textContent = group.textContent.trim();
      h2.appendChild(crumb);
    }
    if (h2 && panel && window.secHelpAttach) window.secHelpAttach(h2, panel.id);
  });

  // 2. "/" jumps to the settings search, Escape clears it
  const search = document.getElementById('settings-tab-search');
  if (search) {
    document.addEventListener('keydown', (e) => {
      const el = document.activeElement;
      const typing = el && (/^(INPUT|TEXTAREA|SELECT)$/.test(el.tagName) || el.isContentEditable);
      if (e.key === '/' && !typing && !e.ctrlKey && !e.metaKey && !e.altKey) { e.preventDefault(); search.focus(); search.select(); }
    });
    search.addEventListener('keydown', (e) => {
      if (e.key === 'Escape' && search.value) { search.value = ''; search.dispatchEvent(new Event('input')); }
    });
  }
})();

// --- Sub-Agents Settings Panel ---
async function initSubagentsPanel() {
  const defaultStepsInput = document.getElementById('subagent-default-steps');
  const maxStepsInput = document.getElementById('subagent-max-steps');
  const timeoutInput = document.getElementById('subagent-timeout-s');
  const maxParallelInput = document.getElementById('subagent-max-parallel');
  const saveBtn = document.getElementById('subagent-save-btn');
  const resetBtn = document.getElementById('subagent-reset-btn');
  const statusEl = document.getElementById('subagent-save-status');

  if (!defaultStepsInput) return;

  function setStatus(msg, isError = false) {
    if (!statusEl) return;
    statusEl.textContent = msg;
    statusEl.style.color = isError ? 'var(--danger, #ef4444)' : 'var(--accent, #38bdf8)';
    if (msg) setTimeout(() => { statusEl.textContent = ''; }, 4000);
  }

  async function loadConfig() {
    try {
      const res = await fetch('/control/subagents');
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      const data = await res.json();
      defaultStepsInput.value = data.subagent_default_steps ?? 40;
      maxStepsInput.value = data.subagent_max_steps ?? 80;
      timeoutInput.value = data.subagent_timeout_s ?? 3600;
      maxParallelInput.value = data.subagent_max_parallel ?? 4;
    } catch (err) {
      console.warn('Failed to load subagent settings:', err);
      setStatus('Failed to load current settings', true);
    }
  }

  async function saveConfig() {
    const payload = {
      subagent_default_steps: parseInt(defaultStepsInput.value, 10),
      subagent_max_steps: parseInt(maxStepsInput.value, 10),
      subagent_timeout_s: parseInt(timeoutInput.value, 10),
      subagent_max_parallel: parseInt(maxParallelInput.value, 10),
    };
    if (saveBtn) {
      saveBtn.disabled = true;
      saveBtn.textContent = 'Saving...';
    }
    try {
      const res = await fetch('/control/subagents', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(payload),
      });
      if (!res.ok) {
        const errData = await res.json().catch(() => ({}));
        throw new Error(errData.detail || `HTTP ${res.status}`);
      }
      setStatus('✓ Subagent settings saved successfully');
    } catch (err) {
      setStatus(`Error: ${err.message}`, true);
    } finally {
      if (saveBtn) {
        saveBtn.disabled = false;
        saveBtn.textContent = 'Save Subagent Settings';
      }
    }
  }

  function resetDefaults() {
    defaultStepsInput.value = 10;
    maxStepsInput.value = 20;
    timeoutInput.value = 180;
    maxParallelInput.value = 4;
    setStatus('Reset to default values (click Save to persist)');
  }

  if (saveBtn) saveBtn.onclick = saveConfig;
  if (resetBtn) resetBtn.onclick = resetDefaults;

  loadConfig();
}
window.initSubagentsPanel = initSubagentsPanel;

// Admin-only internal fetch allowlist (Settings -> Network Access).
// This panel exists only for holders of security.network_policy (data-perm), and the
// server re-checks that permission on both endpoints - hiding the tab is not the gate.
(function initFetchAllowlistPanel() {
  const area = document.getElementById('fetch-allow-cidrs');
  const saveBtn = document.getElementById('btn-fetch-allow-save');
  const statusEl = document.getElementById('fetch-allow-status');
  if (!area || !saveBtn) return;

  const show = (msg, bad) => {
    if (!statusEl) return;
    statusEl.textContent = msg;
    statusEl.style.color = bad ? '#f43f5e' : '';
  };

  async function load() {
    try {
      const res = await fetch('/control/fetch-allowlist', { headers: { 'Accept': 'application/json' } });
      if (!res.ok) throw new Error('HTTP ' + res.status);
      const data = await res.json();
      area.value = (data.cidrs || []).join('\n');
    } catch (err) {
      show('Could not load current ranges: ' + err.message, true);
    }
  }

  saveBtn.onclick = async () => {
    const cidrs = area.value.split(/[\r\n,]+/).map(s => s.trim()).filter(Boolean);
    saveBtn.disabled = true;
    try {
      const res = await fetch('/control/fetch-allowlist', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json', 'Accept': 'application/json' },
        body: JSON.stringify({ cidrs }),
      });
      const data = await res.json().catch(() => ({}));
      if (!res.ok) throw new Error(data.detail || ('HTTP ' + res.status));
      area.value = (data.cidrs || []).join('\n');
      show('✓ Saved ' + data.cidrs.length + ' range(s)', false);
    } catch (err) {
      show('Error: ' + err.message, true);
    } finally {
      saveBtn.disabled = false;
    }
  };

  load();
})();

// Per-user Appearance & Themes: load from the account, save back to the account.
(function initAppearancePanel() {
  const panel = document.getElementById('sec-theme');
  if (!panel) return;
  const status = document.getElementById('appearance-status');
  const root = document.documentElement;
  let prefs = null;

  const toast = (msg, bad) => {
    if (!status) return;
    status.textContent = msg;
    status.style.color = bad ? '#f43f5e' : '';
  };

  const mix = (a, b, t) => {
    const parse = h => { const v = String(h || '').replace('#', ''); return [0, 2, 4].map(i => parseInt(v.slice(i, i + 2), 16) || 0); };
    const x = parse(a), y = parse(b);
    return '#' + x.map((c, i) => Math.round(c + (y[i] - c) * t).toString(16).padStart(2, '0')).join('');
  };

  function apply(p) {
    prefs = p;
    try { localStorage.setItem('a770_appearance', JSON.stringify(p)); } catch (_) {}
    root.setAttribute('data-mode', p.mode);
    root.setAttribute('data-font', p.font);
    root.setAttribute('data-conversation-steps', p.conversation_steps || 'default');
    root.classList.toggle('contrast-strong', p.contrast === 'strong');
    // Background / Foreground / Accent are CODE-PREVIEW colours only. The theme
    // itself comes from the selected preset (data-theme) and must not be overridden.
    root.style.setProperty('--code-bg', p.background);
    root.style.setProperty('--code-fg', p.foreground);
    root.style.setProperty('--code-accent', p.accent);
    root.style.fontSize = p.font_size + 'px';
    const effective = p.mode === 'light'
      ? (p.theme.indexOf('light') >= 0 ? p.theme : 'light')
      : p.mode === 'dark' && p.theme.indexOf('light') >= 0 ? 'slate' : p.theme;
    setTheme(effective);
    const put = (id, v) => { const el = document.getElementById(id); if (el) el.value = v; };
    const pick = (attr, value) => panel.querySelectorAll('[' + attr + ']').forEach(b => b.classList.toggle('active', b.getAttribute(attr) === value));
    pick('data-mode', p.mode);
    pick('data-contrast', p.contrast);
    pick('data-mode-step', p.conversation_steps);
    put('ap-theme', p.theme); put('ap-font', p.font);
    put('ap-size', String(p.font_size));
    put('ap-bg', p.background); put('ap-fg', p.foreground); put('ap-accent', p.accent);
    ['bg', 'fg', 'accent'].forEach(k => {
      const c = document.getElementById('ap-c-' + k), t = document.getElementById('ap-t-' + k);
      if (c) c.value = p[k === 'bg' ? 'background' : k === 'fg' ? 'foreground' : 'accent'];
      if (t) t.textContent = p[k === 'bg' ? 'background' : k === 'fg' ? 'foreground' : 'accent'];
    });
  }

  async function save(patch) {
    // Apply locally first — the UI must never wait on the account API.
    const next = { ...(prefs || {}), ...(patch || {}) };
    apply(next);
    toast('Appearance saved', false);
    try {
      const r = await fetch('/control/appearance', {
        method: 'PUT', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(next),
      });
      if (!r.ok) return;                       // local choice stays active
      const data = await r.json().catch(() => null);
      if (data) apply(data);
    } catch (_) { /* offline / backend not restarted: local preference is enough */ }
  }
  window.saveAppearance = save;

  const active = (attr) => panel.querySelector('[' + attr + '].active')?.getAttribute(attr);
  const collect = () => ({
    mode: active('data-mode') || 'system',
    contrast: active('data-contrast') || 'default',
    font: document.getElementById('ap-font')?.value,
    font_size: Number(document.getElementById('ap-size')?.value || 14),
    theme: document.getElementById('ap-theme')?.value,
    conversation_steps: active('data-mode-step') || 'default',
    background: document.getElementById('ap-bg')?.value,
    foreground: document.getElementById('ap-fg')?.value,
    accent: document.getElementById('ap-accent')?.value,
  });

  panel.addEventListener('change', (e) => {
    if (!e.target?.id?.startsWith('ap-')) return;
    const id = e.target.id;
    if (id.startsWith('ap-c-')) {
      const key = id.slice(5) === 'bg' ? 'background' : id.slice(5) === 'fg' ? 'foreground' : 'accent';
      const text = document.getElementById('ap-t-' + id.slice(5));
      if (text) text.textContent = e.target.value;
      document.getElementById('ap-' + (key === 'background' ? 'bg' : key === 'foreground' ? 'fg' : 'accent')).value = e.target.value;
      save({ [key]: e.target.value });
    } else {
      save(collect());
    }
  });
  panel.addEventListener('click', (e) => {
    const btn = e.target.closest('[data-mode], [data-contrast], [data-mode-step]');
    if (!btn) return;
    const attr = ['data-mode', 'data-contrast', 'data-mode-step'].find(a => btn.hasAttribute(a));
    panel.querySelectorAll('[' + attr + ']').forEach(b => b.classList.toggle('active', b === btn));
    save(collect());
  });
  const localPrefs = (() => { try { return JSON.parse(localStorage.getItem('a770_appearance') || 'null'); } catch (_) { return null; } })();
  if (localPrefs) apply(localPrefs);
  fetch('/control/appearance').then(r => r.ok ? r.json() : null).then(d => {
    if (d) {
      apply(d);
    } else if (!localPrefs) {
      toast('Using locally saved appearance', false);
    }
  }).catch(() => { if (!localPrefs) toast('Using locally saved appearance', false); });
})();

// Account panels: profile readout and password change (moved out of the user menu).
function populateAccountPanels(u) {
  u = u || window.__user || {};
  const fill = (id, v) => { const el = document.getElementById(id); if (el) el.value = v || ''; };
  fill('prof-display-name', u.display_name || u.username);
  fill('prof-email', u.email || u.username);
  fill('prof-employee-id', u.employee_id);
  fill('prof-department', u.department);
  fill('prof-designation', u.designation);
  fill('prof-dob', u.dob);
  fill('prof-gender', u.gender);
  const role = document.getElementById('prof-role');
  if (role) role.textContent = (u.role_names || []).join(', ') || (u.is_super_admin ? 'Super admin' : 'User');
  const avatar = document.getElementById('prof-avatar');
  if (avatar) {
    if (u.image) {
      avatar.innerHTML = '<img src="' + u.image + '" alt="Avatar">';
    } else {
      const raw = u.display_name || u.email || u.username || 'U';
      const initials = raw.split(/[\s._-]+/).filter(Boolean).map(w => w[0]).join('').slice(0, 2).toUpperCase() || 'U';
      avatar.textContent = initials;
    }
  }
}
window.populateAccountPanels = populateAccountPanels;

(function initAccountPanels() {
  let imageData = (window.__user && window.__user.image) || '';
  if (window.__user) populateAccountPanels(window.__user);

  if (window.__sessionReady) {
    window.__sessionReady.then((data) => {
      if (data && data.user) {
        if (!imageData && data.user.image) imageData = data.user.image;
        populateAccountPanels(data.user);
      }
    });
  }
  document.addEventListener('session-ready', (e) => {
    if (e.detail && e.detail.user) {
      if (!imageData && e.detail.user.image) imageData = e.detail.user.image;
      populateAccountPanels(e.detail.user);
    }
  });

  // Fallback explicit fetch to guarantee profile loads even if session-ready already resolved
  fetch('/auth/me').then(r => r.ok ? r.json() : null).then(d => {
    if (d && d.user) {
      if (d.roles) d.user.role_names = d.roles;
      window.__user = Object.assign(window.__user || {}, d.user);
      if (!imageData && d.user.image) imageData = d.user.image;
      populateAccountPanels(d.user);
    }
  }).catch(() => {});

  const avatar = document.getElementById('prof-avatar');
  const imageFile = document.getElementById('prof-image-file');
  if (imageFile) imageFile.onchange = () => {
    const f = imageFile.files && imageFile.files[0];
    if (!f) return;
    if (f.size > 2 * 1024 * 1024) { const s = document.getElementById('prof-status'); if (s) { s.textContent = 'Image must be under 2MB'; s.style.color = '#f43f5e'; } return; }
    const rd = new FileReader();
    rd.onload = () => { imageData = String(rd.result || ''); if (avatar) avatar.innerHTML = '<img src="' + imageData + '" alt="Avatar">'; };
    rd.readAsDataURL(f);
  };

  const profileBtn = document.getElementById('prof-save');
  if (profileBtn) profileBtn.onclick = async () => {
    const status = document.getElementById('prof-status');
    profileBtn.disabled = true;
    if (status) status.textContent = 'Saving…';
    try {
      const r = await fetch('/auth/profile', {
        method: 'PUT', headers: { 'Content-Type': 'application/json', 'Accept': 'application/json' },
        body: JSON.stringify({
          display_name: document.getElementById('prof-display-name')?.value,
          email: document.getElementById('prof-email')?.value,
          employee_id: document.getElementById('prof-employee-id')?.value,
          department: document.getElementById('prof-department')?.value,
          designation: document.getElementById('prof-designation')?.value,
          dob: document.getElementById('prof-dob')?.value,
          gender: document.getElementById('prof-gender')?.value,
          image: imageData,
        }),
      });
      const d = await r.json().catch(() => ({}));
      if (!r.ok || d.ok === false) throw new Error(d.error || ('HTTP ' + r.status));
      if (status) { status.textContent = 'Profile saved.'; status.style.color = 'var(--green)'; }
      if (d.user) {
        window.__user = Object.assign(window.__user || {}, d.user);
        populateAccountPanels(window.__user);
      }
      try {
        if (window.parent && window.parent !== window) {
          window.parent.postMessage({ type: 'profile-updated', user: d.user }, '*');
        }
      } catch (_) {}
    } catch (e) {
      if (status) { status.textContent = e.message; status.style.color = '#f43f5e'; }
    } finally { profileBtn.disabled = false; }
  };

  const btn = document.getElementById('pwd-save-btn');
  const status = document.getElementById('pwd-status');
  const policy = document.getElementById('pwd-policy');
  const newPwd = document.getElementById('pwd-new');
  const confirmPwd = document.getElementById('pwd-confirm');
  // read at check time: window.__user can load after this IIFE runs, and `u` is not in scope here
  const identityOf = () => {
    const u = window.__user || {};
    return ((u.display_name || '') + ' ' + (u.email || u.username || '')).toLowerCase();
  };

  const updatePolicy = () => {
    if (!policy) return;
    const v = newPwd?.value || '';
    const rules = {
      length: v.length >= 12,
      letter: /[A-Za-z]/.test(v),
      digit: /\d/.test(v),
      match: !!v && v === (confirmPwd?.value || ''),
      identity: !v || !identityOf().includes(v.toLowerCase()),
    };
    policy.querySelectorAll('[data-rule]').forEach(el => {
      const ok = rules[el.getAttribute('data-rule')];
      el.classList.toggle('ok', !!ok);
      el.classList.toggle('bad', !ok && (v.length > 0 || el.getAttribute('data-rule') === 'match'));
    });
  };
  [newPwd, confirmPwd].forEach(el => { if (el) el.addEventListener('input', updatePolicy); });
  updatePolicy();

  if (!btn) return;
  btn.onclick = async () => {
    const cur = document.getElementById('pwd-current')?.value || '';
    const next = document.getElementById('pwd-new')?.value || '';
    const confirm = document.getElementById('pwd-confirm')?.value || '';
    if (next !== confirm) { status.textContent = 'New passwords do not match.'; status.style.color = '#f43f5e'; return; }
    if (next.length < 12 || !/[A-Za-z]/.test(next) || !/\d/.test(next) || identityOf().includes(next.toLowerCase())) {
      status.textContent = 'New password does not meet the policy below.'; status.style.color = '#f43f5e'; return;
    }
    btn.disabled = true;
    status.textContent = 'Saving…';
    try {
      const r = await fetch('/auth/change_password', {
        method: 'POST', headers: { 'Content-Type': 'application/json', 'Accept': 'application/json' },
        body: JSON.stringify({ current_password: cur, new_password: next }),
      });
      const d = await r.json().catch(() => ({}));
      if (!r.ok) throw new Error(d.detail || ('HTTP ' + r.status));
      status.textContent = 'Password changed.'; status.style.color = '';
      ['pwd-current', 'pwd-new', 'pwd-confirm'].forEach(id => { const el = document.getElementById(id); if (el) el.value = ''; });
    } catch (e) {
      status.textContent = e.message; status.style.color = '#f43f5e';
    } finally { btn.disabled = false; }
  };
})();

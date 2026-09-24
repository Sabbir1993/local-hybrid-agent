/**
 * input-guard.js - Admin UI for both Input & Output Sanitizer
 * (settings.html #sec-guard).
 * Features:
 *   - Tabbed interface for Input Sanitizer (Prompts) & Output Sanitizer (Redactions)
 *   - Multi-select dropdown for Roles (all roles or specific)
 *   - Multi-select dropdown for Users (populated dynamically from the users table)
 *   - Rule categories (cloud_only, block_all)
 *   - Replacement text customization for output redactions
 */
'use strict';

let _guardRoles = [];
let _guardUsers = [];
let _guardData = {
  input_guard: { enabled: false, rules: [] },
  output_guard: { enabled: false, rules: [] },
};
let _activeGuardTab = 'input_guard';

function _gesc(s) {
  return String(s == null ? '' : s)
    .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;');
}

async function loadInputGuardPanel() {
  const box = document.getElementById('guard-content');
  if (!box) return;
  box.innerHTML = '<div class="mon-empty">Loading sanitizer configuration…</div>';

  try {
    const [inRes, outRes, rolesRes, usersRes] = await Promise.all([
      fetch('/control/input_guard'),
      fetch('/control/output_guard'),
      fetch('/admin/role_names'),
      fetch('/admin/user_names').catch(() => null),
    ]);

    if (!inRes.ok && !outRes.ok) {
      box.innerHTML = '<div class="mon-empty">You do not have permission to configure the sanitizer.</div>';
      return;
    }

    _guardData.input_guard = inRes.ok ? await inRes.json() : { enabled: false, rules: [] };
    _guardData.output_guard = outRes.ok ? await outRes.json() : { enabled: false, rules: [] };

    _guardRoles = rolesRes && rolesRes.ok ? ((await rolesRes.json()).roles || []) : ['admin', 'user'];

    if (usersRes && usersRes.ok) {
      const uData = await usersRes.json();
      _guardUsers = uData.users || [];
    } else {
      // Fallback: fetch from /admin/users if user has users.manage
      try {
        const uFallback = await fetch('/admin/users');
        if (uFallback.ok) {
          const ud = await uFallback.json();
          _guardUsers = (ud.users || []).map(u => ({ id: u.id, username: u.username, display_name: u.display_name || u.username }));
        }
      } catch (e) {
        _guardUsers = [];
      }
    }

    renderSanitizerPanel(box);
  } catch (e) {
    box.innerHTML = '<div class="mon-empty">Failed to load sanitizer: ' + _gesc(e.message) + '</div>';
  }
}

function renderSanitizerPanel(box) {
  const isInput = _activeGuardTab === 'input_guard';
  const cfg = _guardData[_activeGuardTab] || { enabled: false, rules: [] };
  const rules = cfg.rules || [];

  box.innerHTML = `
    <!-- Direction Tabs -->
    <div class="guard-tabs">
      <button type="button" class="guard-tab-btn ${isInput ? 'active' : ''}" id="btn-tab-input">
        📥 Input Sanitizer (Prompts)
      </button>
      <button type="button" class="guard-tab-btn ${!isInput ? 'active' : ''}" id="btn-tab-output">
        📤 Output Sanitizer (Redactions)
      </button>
    </div>

    <!-- Master Header Toggle -->
    <div class="guard-header">
      <label style="display:inline-flex; align-items:center; gap:8px; cursor:pointer; user-select:none; margin:0;">
        <input type="checkbox" id="guard-master-enabled" ${cfg.enabled ? 'checked' : ''} style="cursor:pointer; width:16px; height:16px; margin:0;">
        <strong style="font-size:13px; letter-spacing:0.3px;">
          ${isInput ? '🛡️ Enable Input Sanitizer' : '🧼 Enable Output Sanitizer'}
        </strong>
      </label>
      <span class="guard-badge ${cfg.enabled ? 'cloud' : 'block'}" style="font-size:10.5px;" id="guard-active-indicator">
        ${cfg.enabled ? '● Active' : '○ Disabled'}
      </span>
    </div>

    <!-- Subtitle -->
    <div class="dim" style="font-size:11.5px; margin-bottom:12px; line-height:1.4;">
      ${isInput 
        ? 'Inspect prompt text before sending to LLM. Block prohibited patterns, credentials, and sensitive keywords.' 
        : 'Inspect LLM completion tokens and redact/mask sensitive data (keys, PII, internal IDs) before streaming to users.'}
    </div>

    <!-- Rules List -->
    <div id="guard-rules-list">
      ${rules.map((r, i) => _ruleRow(r, i, _activeGuardTab)).join('') || '<div class="mon-empty">No rules defined yet. Click "+ Add rule" below.</div>'}
    </div>

    <!-- Actions Footer -->
    <div style="display:flex; gap:8px; margin-top:12px; align-items:center; flex-wrap:wrap;">
      <button class="btn ghost" id="guard-btn-add" type="button" style="width:auto; margin:0; padding:6px 14px; font-size:12px;">+ Add rule</button>
      <button class="btn blue" id="guard-btn-save" type="button" style="width:auto; margin:0; padding:6px 18px; font-size:12px;">💾 Save ${isInput ? 'Input' : 'Output'} Rules</button>
      <span id="guard-save-status" class="dim mono" style="font-size:11.5px; margin-left:6px;"></span>
    </div>
  `;

  // Tab switcher events
  box.querySelector('#btn-tab-input').onclick = () => {
    _activeGuardTab = 'input_guard';
    renderSanitizerPanel(box);
  };
  box.querySelector('#btn-tab-output').onclick = () => {
    _activeGuardTab = 'output_guard';
    renderSanitizerPanel(box);
  };

  // Master switch live indicator
  const masterCb = box.querySelector('#guard-master-enabled');
  masterCb.onchange = () => {
    cfg.enabled = masterCb.checked;
    const ind = box.querySelector('#guard-active-indicator');
    if (ind) {
      ind.textContent = cfg.enabled ? '● Active' : '○ Disabled';
      ind.className = 'guard-badge ' + (cfg.enabled ? 'cloud' : 'block');
    }
  };

  // Add rule button
  box.querySelector('#guard-btn-add').onclick = () => {
    const list = box.querySelector('#guard-rules-list');
    if (list.querySelector('.mon-empty')) list.innerHTML = '';
    const newIdx = list.querySelectorAll('.guard-rule').length;
    list.insertAdjacentHTML('beforeend', _ruleRow({
      name: '',
      type: 'regex',
      scope: 'cloud_only',
      enabled: true,
      patterns: [],
      description: '',
      roles: [],
      users: [],
      message: '',
      replacement: isInput ? undefined : '█████',
    }, newIdx, _activeGuardTab));
    _bindRuleInteractions(box);
  };

  // Save rules button
  box.querySelector('#guard-btn-save').onclick = () => saveCurrentGuard(box);

  _bindRuleInteractions(box);
}

function _ruleRow(r, i, cfgKey) {
  const isOutput = cfgKey === 'output_guard';
  const isCloud = r.scope === 'cloud_only';
  const scopeBadgeClass = isCloud ? 'cloud' : 'block';
  const scopeBadgeText = isCloud ? '☁️ Cloud only' : (isOutput ? '🔒 Redact everywhere' : '🚫 Block everywhere');

  const selectedRoles = Array.isArray(r.roles) ? r.roles : [];
  const selectedUsers = Array.isArray(r.users) ? r.users : [];

  const target = (selectedRoles.length) ? 'roles: ' + selectedRoles.join(', ')
    : (selectedUsers.length) ? 'users: ' + selectedUsers.join(', ') : 'everyone';

  return `
  <div class="guard-rule" data-idx="${i}">
    <!-- Top Row: Name, Scope, Enabled, Delete -->
    <div class="guard-top-row">
      <input class="g-name guard-input" value="${_gesc(r.name)}" placeholder="Rule name (e.g. Block PII)" style="flex:1 1 160px; min-width:140px;">

      <select class="g-type guard-input" style="cursor:pointer;" title="Regex patterns match text; natural-language policy is judged by the local model">
        <option value="regex" ${(r.type || 'regex') === 'regex' ? 'selected' : ''}>Regex</option>
        <option value="semantic" ${r.type === 'semantic' ? 'selected' : ''}>🧠 Policy</option>
      </select>

      <select class="g-scope guard-input" style="cursor:pointer;">
        <option value="cloud_only" ${isCloud ? 'selected' : ''}>☁️ Cloud only</option>
        <option value="block_all" ${!isCloud ? 'selected' : ''}>${isOutput ? '🔒 Redact everywhere' : '🚫 Block everywhere'}</option>
      </select>

      <label style="font-size:11.5px; display:inline-flex; align-items:center; gap:5px; padding:5px 8px; border-radius:6px; background:var(--panel2); border:1px solid var(--border); cursor:pointer; user-select:none;">
        <input type="checkbox" class="g-enabled" ${r.enabled !== false ? 'checked' : ''} style="margin:0; cursor:pointer;">
        <span class="dim" style="font-size:11px;">enabled</span>
      </label>

      <button class="guard-del-btn g-del" type="button" title="Delete rule">✕</button>
    </div>

    <!-- Patterns Section (regex rules) -->
    <div class="g-sec-patterns" style="display:${r.type === 'semantic' ? 'none' : 'block'};">
      <div style="font-size:11px; font-weight:600; color:var(--dim); margin-bottom:4px; display:flex; justify-content:space-between;">
        <span>Regex Patterns (one per line):</span>
        <span class="mono" style="font-size:10px; opacity:0.8;">ECMAScript RegEx</span>
      </div>
      <textarea class="g-patterns guard-input" rows="3" style="width:100%;"
        placeholder="\bINV-\d{4,}\b">${_gesc((r.patterns || []).join('\n'))}</textarea>
    </div>

    <!-- Description Section (semantic rules) -->
    <div class="g-sec-desc" style="display:${r.type === 'semantic' ? 'block' : 'none'};">
      <div style="font-size:11px; font-weight:600; color:var(--dim); margin-bottom:4px;">Policy description (plain English, judged by local executor model):</div>
      <textarea class="g-description guard-input" rows="3" style="width:100%;"
        placeholder="e.g. Any content containing transactional data (payments, invoices, bank transfers)">${_gesc(r.description || '')}</textarea>
    </div>

    ${isOutput ? `
    <!-- Replacement Text (Output Only) -->
    <div>
      <div style="font-size:11px; font-weight:600; color:var(--dim); margin-bottom:4px;">Replacement Mask Text (default: █████):</div>
      <input class="g-replacement guard-input" value="${_gesc(r.replacement || '█████')}" placeholder="█████ or [REDACTED]" style="width:100%; font-family:monospace;">
    </div>` : ''}

    <!-- Roles & Users Multi-Select Dropdowns -->
    <div style="display:flex; gap:10px; flex-wrap:wrap;">
      
      <!-- Roles Multi-Select -->
      <div style="flex:1; min-width:160px;">
        <div style="font-size:11px; font-weight:600; color:var(--dim); margin-bottom:4px;">Roles — empty = no role filter:</div>
        <div class="guard-ms-wrap" data-ms-type="roles">
          <input type="hidden" class="g-roles-val" value="${_gesc(JSON.stringify(selectedRoles))}">
          <button type="button" class="guard-ms-btn">
            <span class="guard-ms-chips">
              ${_renderChips(selectedRoles, 'Everyone (all roles)')}
            </span>
            <span class="dim" style="font-size:10px;">▾</span>
          </button>
          <div class="guard-ms-popover">
            <input type="text" class="guard-ms-search" placeholder="Filter roles…">
            <div class="guard-ms-actions">
              <a class="ms-select-all">Select all</a>
              <a class="ms-clear-all" style="color:var(--dim);">Clear</a>
            </div>
            <div class="guard-ms-items">
              ${_guardRoles.map(ro => `
                <label class="guard-ms-item">
                  <span>${_gesc(ro)}</span>
                  <input type="checkbox" value="${_gesc(ro)}" ${selectedRoles.includes(ro) ? 'checked' : ''}>
                </label>
              `).join('')}
            </div>
          </div>
        </div>
      </div>

      <!-- Users Multi-Select (Dynamically from Users Table) -->
      <div style="flex:1; min-width:160px;">
        <div style="font-size:11px; font-weight:600; color:var(--dim); margin-bottom:4px;">Users — empty = no user filter:</div>
        <div class="guard-ms-wrap" data-ms-type="users">
          <input type="hidden" class="g-users-val" value="${_gesc(JSON.stringify(selectedUsers))}">
          <button type="button" class="guard-ms-btn">
            <span class="guard-ms-chips">
              ${_renderChips(selectedUsers, 'Everyone (all users)')}
            </span>
            <span class="dim" style="font-size:10px;">▾</span>
          </button>
          <div class="guard-ms-popover">
            <input type="text" class="guard-ms-search" placeholder="Filter users…">
            <div class="guard-ms-actions">
              <a class="ms-select-all">Select all</a>
              <a class="ms-clear-all" style="color:var(--dim);">Clear</a>
            </div>
            <div class="guard-ms-items">
              ${_guardUsers.length ? _guardUsers.map(u => {
                const uname = typeof u === 'string' ? u : u.username;
                const dname = (typeof u === 'object' && u.display_name && u.display_name !== uname) ? ` (${u.display_name})` : '';
                return `
                  <label class="guard-ms-item">
                    <span>${_gesc(uname)}<small class="dim" style="margin-left:4px;">${_gesc(dname)}</small></span>
                    <input type="checkbox" value="${_gesc(uname)}" ${selectedUsers.includes(uname) ? 'checked' : ''}>
                  </label>
                `;
              }).join('') : '<div class="dim" style="font-size:11px; padding:4px;">No users in database</div>'}
            </div>
          </div>
        </div>
      </div>

    </div>

    <!-- Message Section -->
    <div>
      <div style="font-size:11px; font-weight:600; color:var(--dim); margin-bottom:4px;">
        ${isOutput ? 'Policy note / audit description:' : 'Message shown to user when blocked:'}
      </div>
      <input class="g-message guard-input" value="${_gesc(r.message || '')}" placeholder="Human-readable explanation..." style="width:100%;">
    </div>

    <!-- Meta Footer -->
    <div class="guard-footer-meta">
      <span class="guard-badge ${scopeBadgeClass}">${scopeBadgeText}</span>
      <span>${(!(r.roles || []).length && !(r.users || []).length)
        ? '🌐 No role filter — applies to <strong style="color:var(--text);">everyone</strong>'
        : 'Applies to: <strong style="color:var(--text);">' + _gesc(target) + '</strong>'}</span>
      ${r.type === 'semantic' ? '<span class="guard-badge block" style="font-size:10px;" title="Evaluated locally by the executor small model (port 8091 / GPU 1) before sending to the target model">🧠 local executor model judges</span>' : ''}
    </div>
  </div>`;
}

function _renderChips(items, emptyLabel) {
  if (!items || !items.length) {
    return `<span class="dim">${_gesc(emptyLabel)}</span>`;
  }
  return items.map(item => `<span class="guard-chip">${_gesc(item)}</span>`).join('');
}

function _bindRuleInteractions(box) {
  // Rule type toggle: swap regex patterns / policy description sections
  box.querySelectorAll('.g-type').forEach(sel => {
    sel.onchange = () => {
      const rule = sel.closest('.guard-rule');
      if (!rule) return;
      const isSem = sel.value === 'semantic';
      const pat = rule.querySelector('.g-sec-patterns');
      const desc = rule.querySelector('.g-sec-desc');
      if (pat) pat.style.display = isSem ? 'none' : 'block';
      if (desc) desc.style.display = isSem ? 'block' : 'none';
    };
  });

  // Delete rule
  box.querySelectorAll('.g-del').forEach(btn => {
    btn.onclick = () => {
      const row = btn.closest('.guard-rule');
      if (row) {
        row.style.opacity = '0';
        row.style.transform = 'scale(0.98)';
        setTimeout(() => {
          row.remove();
          if (!box.querySelector('.guard-rule')) {
            box.querySelector('#guard-rules-list').innerHTML = '<div class="mon-empty">No rules defined yet. Click "+ Add rule" below.</div>';
          }
        }, 150);
      }
    };
  });

  // Multi-select dropdown popovers
  box.querySelectorAll('.guard-ms-wrap').forEach(wrap => {
    const btn = wrap.querySelector('.guard-ms-btn');
    const popover = wrap.querySelector('.guard-ms-popover');
    const hidden = wrap.querySelector('input[type="hidden"]');
    const chipsSpan = wrap.querySelector('.guard-ms-chips');
    const search = wrap.querySelector('.guard-ms-search');
    const type = wrap.dataset.msType;
    const emptyLabel = type === 'roles' ? 'Everyone (all roles)' : 'Everyone (all users)';

    const updateState = () => {
      const checkedBoxes = Array.from(popover.querySelectorAll('.guard-ms-item input[type="checkbox"]:checked'));
      const selected = checkedBoxes.map(cb => cb.value);
      hidden.value = JSON.stringify(selected);
      chipsSpan.innerHTML = _renderChips(selected, emptyLabel);
      
      // Update rule footer summary
      const ruleEl = wrap.closest('.guard-rule');
      if (ruleEl) {
        const rRoles = JSON.parse(ruleEl.querySelector('.g-roles-val').value || '[]');
        const rUsers = JSON.parse(ruleEl.querySelector('.g-users-val').value || '[]');
        const targetStr = rRoles.length ? 'roles: ' + rRoles.join(', ')
          : (rUsers.length ? 'users: ' + rUsers.join(', ') : 'everyone');
        const footerStrong = ruleEl.querySelector('.guard-footer-meta strong');
        if (footerStrong) footerStrong.textContent = targetStr;
      }
    };

    btn.onclick = (e) => {
      e.stopPropagation();
      const isOpen = popover.classList.contains('open');
      document.querySelectorAll('.guard-ms-popover.open').forEach(p => p.classList.remove('open'));
      if (!isOpen) {
        popover.classList.add('open');
        if (search) { search.value = ''; search.focus(); }
        popover.querySelectorAll('.guard-ms-item').forEach(item => item.style.display = '');
      }
    };

    popover.onclick = (e) => e.stopPropagation();

    // Checkbox changes
    popover.querySelectorAll('.guard-ms-item input[type="checkbox"]').forEach(cb => {
      cb.onchange = updateState;
    });

    // Search filter
    if (search) {
      search.oninput = () => {
        const q = search.value.toLowerCase().trim();
        popover.querySelectorAll('.guard-ms-item').forEach(item => {
          item.style.display = item.textContent.toLowerCase().includes(q) ? '' : 'none';
        });
      };
    }

    // Select all / clear all
    const selectAllBtn = popover.querySelector('.ms-select-all');
    if (selectAllBtn) {
      selectAllBtn.onclick = () => {
        popover.querySelectorAll('.guard-ms-item input[type="checkbox"]').forEach(cb => cb.checked = true);
        updateState();
      };
    }
    const clearAllBtn = popover.querySelector('.ms-clear-all');
    if (clearAllBtn) {
      clearAllBtn.onclick = () => {
        popover.querySelectorAll('.guard-ms-item input[type="checkbox"]').forEach(cb => cb.checked = false);
        updateState();
      };
    }
  });
}

// Close multi-select popovers when clicking outside
document.addEventListener('click', () => {
  document.querySelectorAll('.guard-ms-popover.open').forEach(p => p.classList.remove('open'));
});

async function saveCurrentGuard(box) {
  const result = box.querySelector('#guard-save-status');
  const cfgKey = _activeGuardTab;
  const isOutput = cfgKey === 'output_guard';
  const masterCb = box.querySelector('#guard-master-enabled');

  const rules = Array.from(box.querySelectorAll('.guard-rule')).map((el, i) => {
    let roles = [];
    try { roles = JSON.parse(el.querySelector('.g-roles-val').value || '[]'); } catch (e) {}

    let users = [];
    try { users = JSON.parse(el.querySelector('.g-users-val').value || '[]'); } catch (e) {}

    const patterns = el.querySelector('.g-patterns').value.split('\n').map(s => s.trim()).filter(Boolean);
    const descEl = el.querySelector('.g-description');
    const typeEl = el.querySelector('.g-type');
    const rtype = typeEl ? typeEl.value : 'regex';
    const replEl = el.querySelector('.g-replacement');
    const replacement = replEl ? replEl.value.trim() : undefined;

    const rule = {
      id: (_guardData[cfgKey].rules && _guardData[cfgKey].rules[i] && _guardData[cfgKey].rules[i].id) || (`${cfgKey}-${i + 1}-${Date.now()}`),
      name: el.querySelector('.g-name').value.trim(),
      type: rtype,
      scope: el.querySelector('.g-scope').value,
      roles: roles,
      users: users,
      message: el.querySelector('.g-message').value.trim(),
      enabled: el.querySelector('.g-enabled').checked,
    };
    if (rtype === 'semantic') {
      rule.description = descEl ? descEl.value.trim() : '';
    } else {
      rule.patterns = patterns;
      if (replacement !== undefined) rule.replacement = replacement;
    }
    return rule;
  });

  try {
    const res = await fetch('/control/' + cfgKey, {
      method: 'PUT',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ enabled: masterCb.checked, rules: rules }),
    });
    const j = await res.json().catch(() => ({}));
    if (!res.ok) throw new Error(j.error || j.detail || ('HTTP ' + res.status));

    _guardData[cfgKey].enabled = masterCb.checked;
    _guardData[cfgKey].rules = rules;

    let msg = `✓ Saved ${rules.length} ${isOutput ? 'output' : 'input'} rule(s).`;
    if (j.problems && j.problems.length) msg += ' Skipped: ' + j.problems.join(' | ');
    result.textContent = msg;

    if (typeof showToast === 'function') {
      showToast(`${isOutput ? 'Output' : 'Input'} sanitizer rules saved`);
    }

    setTimeout(() => {
      renderSanitizerPanel(box);
    }, 400);
  } catch (e) {
    result.textContent = 'Save failed: ' + e.message;
  }
}

window.loadInputGuardPanel = loadInputGuardPanel;
window.loadOutputGuardPanel = loadInputGuardPanel;

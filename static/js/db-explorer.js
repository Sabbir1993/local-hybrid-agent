/* ---------------- db-explorer.js ----------------
 * Interactive Database Explorer & SQL Query Console.
 * Permission-restricted to users with 'database.manage'.
 */

(function () {
  let _currentDbId = 'auth';
  let _dbs = [];
  let _schema = null;
  let _currentTable = null;
  let _lastResult = null;
  let _queryHistory = [];

  const STORAGE_KEY_HISTORY = 'a770_db_query_history';

  function initHistory() {
    try {
      const saved = localStorage.getItem(STORAGE_KEY_HISTORY);
      if (saved) _queryHistory = JSON.parse(saved);
    } catch (e) {}
  }

  function saveHistory(sql) {
    sql = sql.trim();
    if (!sql) return;
    _queryHistory = [sql, ..._queryHistory.filter(q => q !== sql)].slice(0, 30);
    try {
      localStorage.setItem(STORAGE_KEY_HISTORY, JSON.stringify(_queryHistory));
    } catch (e) {}
    renderHistoryDropdown();
  }

  function renderHistoryDropdown() {
    const sel = document.getElementById('db-history-select');
    if (!sel) return;
    if (!_queryHistory.length) {
      sel.innerHTML = '<option value="">(No recent queries)</option>';
      return;
    }
    let html = '<option value="">📜 Recent Queries (' + _queryHistory.length + ')</option>';
    _queryHistory.forEach((q, idx) => {
      const truncated = q.replace(/\s+/g, ' ').slice(0, 60);
      html += `<option value="${idx}">${escapeHtml(truncated)}</option>`;
    });
    sel.innerHTML = html;
  }

  function escapeHtml(str) {
    if (str == null) return '';
    return String(str)
      .replace(/&/g, '&amp;')
      .replace(/</g, '&lt;')
      .replace(/>/g, '&gt;')
      .replace(/"/g, '&quot;')
      .replace(/'/g, '&#039;');
  }

  function formatBytes(bytes) {
    if (!bytes || bytes <= 0) return '0 B';
    const units = ['B', 'KB', 'MB', 'GB'];
    let i = Math.floor(Math.log(bytes) / Math.log(1024));
    return (bytes / Math.pow(1024, i)).toFixed(1) + ' ' + units[i];
  }

  async function openDbModal() {
    const modal = document.getElementById('db-modal');
    if (!modal) return;
    modal.hidden = false;
    initHistory();
    renderHistoryDropdown();
    await loadDatabaseList();
  }

  function closeDbModal() {
    const modal = document.getElementById('db-modal');
    if (modal) modal.hidden = true;
  }

  async function loadDatabaseList() {
    const sel = document.getElementById('db-select');
    const badge = document.getElementById('db-info-badge');
    if (!sel) return;

    try {
      const resp = await fetch('/db/list');
      if (resp.status === 403) {
        if (typeof toast === 'function') toast('Access denied: missing database.manage permission', true);
        closeDbModal();
        return;
      }
      if (!resp.ok) throw new Error('Failed to load database list');
      const data = await resp.json();
      _dbs = data.databases || [];

      sel.innerHTML = _dbs.map(d => {
        const sysTag = d.is_system ? '⚡ [System]' : '📁 [Workspace]';
        return `<option value="${escapeHtml(d.id)}">${sysTag} ${escapeHtml(d.name)} (${d.table_count} tables, ${formatBytes(d.size_bytes)})</option>`;
      }).join('');

      if (_dbs.length > 0) {
        // preserve current or default to first
        const exists = _dbs.some(d => d.id === _currentDbId);
        if (!exists) _currentDbId = _dbs[0].id;
        sel.value = _currentDbId;
        updateDbInfoBadge();
        await loadSchema(_currentDbId);
      }
    } catch (e) {
      if (badge) badge.textContent = 'Error loading databases: ' + e.message;
    }
  }

  function updateDbInfoBadge() {
    const badge = document.getElementById('db-info-badge');
    if (!badge) return;
    const db = _dbs.find(d => d.id === _currentDbId);
    if (!db) {
      badge.textContent = '';
      return;
    }
    const typeLabel = db.is_system ? 'System Database' : 'Workspace Database';
    badge.innerHTML = `<span class="db-chip ${db.is_system ? 'sys' : 'ws'}">${typeLabel}</span> ` +
      `<span class="dim" style="font-size:11px;">${escapeHtml(db.description || '')} · <b>${formatBytes(db.size_bytes)}</b></span>`;
  }

  async function loadSchema(dbId) {
    const listEl = document.getElementById('db-table-list');
    if (!listEl) return;
    listEl.innerHTML = '<div class="dim" style="padding:10px; font-size:11px; text-align:center;">Loading tables…</div>';

    try {
      const resp = await fetch(`/db/${encodeURIComponent(dbId)}/schema`);
      if (!resp.ok) {
        const err = await resp.json().catch(() => ({}));
        throw new Error(err.detail || 'Failed to fetch schema');
      }
      _schema = await resp.json();
      renderTableList();
      if (_schema.tables && _schema.tables.length > 0) {
        // Select first table if none selected
        selectTable(_schema.tables[0].name);
      } else {
        showSchemaView(null);
      }
    } catch (e) {
      listEl.innerHTML = `<div style="padding:10px; font-size:11px; color:var(--red);">Schema error: ${escapeHtml(e.message)}</div>`;
    }
  }

  function renderTableList() {
    const listEl = document.getElementById('db-table-list');
    const filterInput = document.getElementById('db-table-filter');
    if (!listEl || !_schema) return;

    const query = filterInput ? filterInput.value.trim().toLowerCase() : '';
    const tables = (_schema.tables || []).filter(t => !query || t.name.toLowerCase().includes(query));

    if (!tables.length) {
      listEl.innerHTML = '<div class="dim" style="padding:10px; font-size:11px; text-align:center;">No matching tables</div>';
      return;
    }

    listEl.innerHTML = tables.map(t => {
      const isSelected = t.name === _currentTable;
      const countLabel = t.row_count >= 0 ? t.row_count.toLocaleString() : '?';
      return `
        <div class="db-table-item ${isSelected ? 'active' : ''}" data-table="${escapeHtml(t.name)}">
          <div class="db-table-name">
            <span class="db-tbl-icon">${t.type === 'view' ? '👁️' : '📋'}</span>
            <span class="db-tbl-text mono" title="${escapeHtml(t.name)}">${escapeHtml(t.name)}</span>
          </div>
          <span class="db-row-badge" title="${countLabel} rows">${countLabel}</span>
        </div>
      `;
    }).join('');

    listEl.querySelectorAll('.db-table-item').forEach(el => {
      el.addEventListener('click', () => {
        const tblName = el.getAttribute('data-table');
        selectTable(tblName);
      });
    });
  }

  function selectTable(tblName) {
    _currentTable = tblName;
    renderTableList();

    const tblObj = (_schema && _schema.tables) ? _schema.tables.find(t => t.name === tblName) : null;
    showSchemaView(tblObj);

    // Put a standard query in the editor and run it
    const sqlInput = document.getElementById('db-sql-input');
    if (sqlInput) {
      sqlInput.value = `SELECT * FROM "${tblName}" LIMIT 50;`;
      runQuery();
    }
  }

  function showSchemaView(tblObj) {
    const container = document.getElementById('db-schema-details');
    if (!container) return;

    if (!tblObj) {
      container.innerHTML = '<div class="dim" style="padding:14px; font-size:12px; text-align:center;">Select a table to view structure and DDL.</div>';
      return;
    }

    const cols = tblObj.columns || [];
    let colsHtml = `
      <div style="margin-bottom:12px;">
        <div style="display:flex; justify-content:space-between; align-items:center; margin-bottom:6px;">
          <span style="font-size:11px; font-weight:700; color:var(--text); text-transform:uppercase; letter-spacing:0.8px;">
            Structure: <span class="mono" style="color:var(--blue);">${escapeHtml(tblObj.name)}</span> (${cols.length} columns)
          </span>
          <button type="button" class="btn ghost" id="btn-copy-ddl" style="width:auto; margin:0; padding:2px 8px; font-size:10.5px;">📋 Copy DDL</button>
        </div>
        <div style="max-height:160px; overflow-y:auto; border:1px solid var(--border); border-radius:6px; background:var(--panel2);">
          <table class="db-schema-table">
            <thead>
              <tr>
                <th>#</th>
                <th>Column</th>
                <th>Type</th>
                <th>PK</th>
                <th>Not Null</th>
                <th>Default</th>
              </tr>
            </thead>
            <tbody>
              ${cols.map(c => `
                <tr>
                  <td class="dim">${c.cid}</td>
                  <td class="mono"><b>${escapeHtml(c.name)}</b></td>
                  <td class="mono dim" style="color:var(--amber);">${escapeHtml(c.type)}</td>
                  <td>${c.pk ? '<span class="db-tag pk">PK</span>' : '—'}</td>
                  <td>${c.notnull ? '<span class="db-tag notnull">YES</span>' : '—'}</td>
                  <td class="mono dim">${c.dflt_value != null ? escapeHtml(c.dflt_value) : 'NULL'}</td>
                </tr>
              `).join('')}
            </tbody>
          </table>
        </div>
      </div>
    `;

    if (tblObj.sql) {
      colsHtml += `
        <div>
          <span style="font-size:10.5px; font-weight:700; color:var(--dim); text-transform:uppercase; letter-spacing:0.8px;">DDL Definition</span>
          <pre class="db-ddl-code mono">${escapeHtml(tblObj.sql)}</pre>
        </div>
      `;
    }

    container.innerHTML = colsHtml;

    const copyBtn = document.getElementById('btn-copy-ddl');
    if (copyBtn && tblObj.sql) {
      copyBtn.onclick = () => {
        navigator.clipboard.writeText(tblObj.sql).then(() => {
          copyBtn.textContent = '✓ Copied';
          setTimeout(() => { copyBtn.textContent = '📋 Copy DDL'; }, 1200);
        });
      };
    }
  }

  async function runQuery() {
    const sqlInput = document.getElementById('db-sql-input');
    const statusEl = document.getElementById('db-status-bar');
    const resultsContainer = document.getElementById('db-results-container');
    const exportBtn = document.getElementById('db-btn-export');
    if (!sqlInput || !statusEl || !resultsContainer) return;

    const sql = sqlInput.value.trim();
    if (!sql) {
      statusEl.innerHTML = '<span style="color:var(--amber);">Please enter a SQL query.</span>';
      return;
    }

    statusEl.innerHTML = '<span class="dim">Executing query…</span>';
    const runBtn = document.getElementById('db-btn-run');
    if (runBtn) runBtn.disabled = true;

    try {
      const resp = await fetch(`/db/${encodeURIComponent(_currentDbId)}/query`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ sql, max_rows: 500 }),
      });

      const data = await resp.json();
      if (!resp.ok) {
        throw new Error(data.detail || ('HTTP ' + resp.status));
      }

      saveHistory(sql);
      _lastResult = data;
      if (exportBtn) exportBtn.disabled = !(data.rows && data.rows.length > 0);

      if (data.is_mutation) {
        statusEl.innerHTML = `
          <span style="color:var(--green);">✓ Statement executed successfully</span> ·
          <span><b>${data.affected_rows}</b> row(s) affected</span> ·
          <span class="dim">${data.duration_ms} ms</span>
        `;
        resultsContainer.innerHTML = `
          <div class="db-mutation-notice">
            <div style="font-size:18px; margin-bottom:6px;">✓</div>
            <b>Query executed with no return dataset</b>
            <p class="dim" style="font-size:11.5px; margin:4px 0 0;">Affected rows: ${data.affected_rows} (${data.duration_ms} ms)</p>
          </div>
        `;
        // Refresh schema in case of DDL/table change
        loadSchema(_currentDbId);
      } else {
        const rowCount = data.row_count || 0;
        const truncNote = data.truncated ? ' <span style="color:var(--amber);">(capped at 500)</span>' : '';
        statusEl.innerHTML = `
          <span style="color:var(--green);">✓ Query returned <b>${rowCount}</b> row(s)${truncNote}</span> ·
          <span class="dim">${data.columns.length} columns</span> ·
          <span class="dim">${data.duration_ms} ms</span>
        `;
        renderResultsTable(data.columns, data.rows);
      }
    } catch (e) {
      statusEl.innerHTML = `<span style="color:var(--red);">✗ Error: ${escapeHtml(e.message)}</span>`;
      resultsContainer.innerHTML = `
        <div class="db-error-box">
          <b>Query Failed</b>
          <pre class="mono" style="margin-top:6px; font-size:11px; white-space:pre-wrap; word-break:break-all;">${escapeHtml(e.message)}</pre>
        </div>
      `;
      if (exportBtn) exportBtn.disabled = true;
    } finally {
      if (runBtn) runBtn.disabled = false;
    }
  }

  function renderResultsTable(columns, rows) {
    const container = document.getElementById('db-results-container');
    if (!container) return;

    if (!columns || !columns.length) {
      container.innerHTML = '<div class="dim" style="padding:20px; text-align:center;">No columns returned.</div>';
      return;
    }

    if (!rows || !rows.length) {
      container.innerHTML = '<div class="dim" style="padding:20px; text-align:center; font-size:12px;">(0 rows returned)</div>';
      return;
    }

    let html = `
      <div class="db-table-wrapper">
        <table class="db-data-table">
          <thead>
            <tr>
              <th class="db-th-num">#</th>
              ${columns.map(c => `<th>${escapeHtml(c)}</th>`).join('')}
            </tr>
          </thead>
          <tbody>
            ${rows.map((row, rIdx) => `
              <tr>
                <td class="db-td-num dim">${rIdx + 1}</td>
                ${row.map(cell => {
                  if (cell === null) {
                    return '<td class="db-cell-null">NULL</td>';
                  }
                  if (typeof cell === 'number') {
                    return `<td class="mono db-cell-num">${cell.toLocaleString()}</td>`;
                  }
                  if (typeof cell === 'boolean') {
                    return `<td class="mono">${cell ? 'TRUE' : 'FALSE'}</td>`;
                  }
                  return `<td class="db-cell-text" title="${escapeHtml(cell)}">${escapeHtml(cell)}</td>`;
                }).join('')}
              </tr>
            `).join('')}
          </tbody>
        </table>
      </div>
    `;

    container.innerHTML = html;
  }

  function exportCsv() {
    if (!_lastResult || !_lastResult.columns || !_lastResult.rows || !_lastResult.rows.length) {
      if (typeof toast === 'function') toast('No result rows to export', true);
      return;
    }

    const cols = _lastResult.columns;
    const rows = _lastResult.rows;

    const escapeCsvCell = val => {
      if (val === null || val === undefined) return '';
      const str = String(val);
      if (str.includes(',') || str.includes('"') || str.includes('\n') || str.includes('\r')) {
        return '"' + str.replace(/"/g, '""') + '"';
      }
      return str;
    };

    const csvLines = [
      cols.map(escapeCsvCell).join(','),
      ...rows.map(row => row.map(escapeCsvCell).join(','))
    ];

    const blob = new Blob([csvLines.join('\r\n')], { type: 'text/csv;charset=utf-8;' });
    const url = URL.createObjectURL(blob);
    const a = document.createElement('a');
    a.href = url;
    a.download = `query_${_currentDbId}_${Date.now()}.csv`;
    document.body.appendChild(a);
    a.click();
    document.body.removeChild(a);
    URL.revokeObjectURL(url);
    if (typeof toast === 'function') toast('CSV export downloaded ✓');
  }

  function setupEventHandlers() {
    // Open modal button
    const openBtn = document.getElementById('btn-db');
    if (openBtn) {
      openBtn.addEventListener('click', openDbModal);
    }

    // Also handle open button in settings page if present
    const openBtnSettings = document.getElementById('btn-db-settings-open');
    if (openBtnSettings) {
      openBtnSettings.addEventListener('click', openDbModal);
    }

    // Close buttons
    const closeBtn = document.getElementById('db-modal-close');
    if (closeBtn) closeBtn.addEventListener('click', closeDbModal);

    const modal = document.getElementById('db-modal');
    if (modal) {
      modal.addEventListener('click', e => {
        if (e.target.id === 'db-modal') closeDbModal();
      });
    }

    // Escape key
    window.addEventListener('keydown', e => {
      if (e.key === 'Escape') {
        const m = document.getElementById('db-modal');
        if (m && !m.hidden) closeDbModal();
      }
    });

    // DB selection change
    const sel = document.getElementById('db-select');
    if (sel) {
      sel.addEventListener('change', () => {
        _currentDbId = sel.value;
        updateDbInfoBadge();
        loadSchema(_currentDbId);
      });
    }

    // Table filter
    const filterInput = document.getElementById('db-table-filter');
    if (filterInput) {
      filterInput.addEventListener('input', renderTableList);
    }

    // Run query button
    const runBtn = document.getElementById('db-btn-run');
    if (runBtn) runBtn.addEventListener('click', runQuery);

    // Clear query button
    const clearBtn = document.getElementById('db-btn-clear');
    if (clearBtn) {
      clearBtn.addEventListener('click', () => {
        const sqlInput = document.getElementById('db-sql-input');
        if (sqlInput) sqlInput.value = '';
        const statusEl = document.getElementById('db-status-bar');
        if (statusEl) statusEl.textContent = 'Ready';
        const container = document.getElementById('db-results-container');
        if (container) container.innerHTML = '<div class="dim" style="padding:20px; text-align:center; font-size:12px;">Query output will appear here.</div>';
        const exportBtn = document.getElementById('db-btn-export');
        if (exportBtn) exportBtn.disabled = true;
        _lastResult = null;
      });
    }

    // Keyboard shortcut Ctrl+Enter / Cmd+Enter inside SQL input
    const sqlInput = document.getElementById('db-sql-input');
    if (sqlInput) {
      sqlInput.addEventListener('keydown', e => {
        if ((e.ctrlKey || e.metaKey) && e.key === 'Enter') {
          e.preventDefault();
          runQuery();
        }
      });
    }

    // Export CSV button
    const exportBtn = document.getElementById('db-btn-export');
    if (exportBtn) exportBtn.addEventListener('click', exportCsv);

    // History dropdown change
    const historySel = document.getElementById('db-history-select');
    if (historySel) {
      historySel.addEventListener('change', () => {
        const idx = historySel.value;
        if (idx !== '' && _queryHistory[idx]) {
          if (sqlInput) sqlInput.value = _queryHistory[idx];
          historySel.value = '';
        }
      });
    }

    // Snippets / Presets dropdown
    const presetSel = document.getElementById('db-preset-select');
    if (presetSel) {
      presetSel.addEventListener('change', () => {
        const val = presetSel.value;
        if (!val) return;
        const tbl = _currentTable || 'sqlite_master';
        let snippet = '';
        if (val === 'select_50') snippet = `SELECT * FROM "${tbl}" LIMIT 50;`;
        else if (val === 'count') snippet = `SELECT COUNT(*) AS total_count FROM "${tbl}";`;
        else if (val === 'columns') snippet = `PRAGMA table_info("${tbl}");`;
        else if (val === 'tables') snippet = `SELECT type, name, sql FROM sqlite_master WHERE type IN ('table', 'view') AND name NOT LIKE 'sqlite_%' ORDER BY name;`;
        else if (val === 'recent_requests') snippet = `SELECT id, datetime(ts, 'unixepoch', 'localtime') AS local_time, model, prompt_tokens, completion_tokens, tps, duration_s FROM requests ORDER BY id DESC LIMIT 20;`;
        else if (val === 'users_roles') snippet = `SELECT u.id, u.username, u.is_active, u.is_super_admin, r.name AS role FROM users u LEFT JOIN user_roles ur ON ur.user_id = u.id LEFT JOIN roles r ON r.id = ur.role_id;`;

        if (sqlInput) {
          sqlInput.value = snippet;
          runQuery();
        }
        presetSel.value = '';
      });
    }

    // Switch between Results view and Schema view
    const tabResults = document.getElementById('db-tab-results');
    const tabSchema = document.getElementById('db-tab-schema');
    const viewResults = document.getElementById('db-view-results');
    const viewSchema = document.getElementById('db-view-schema');

    if (tabResults && tabSchema && viewResults && viewSchema) {
      tabResults.addEventListener('click', () => {
        tabResults.classList.add('active');
        tabSchema.classList.remove('active');
        viewResults.style.display = 'block';
        viewSchema.style.display = 'none';
      });
      tabSchema.addEventListener('click', () => {
        tabSchema.classList.add('active');
        tabResults.classList.remove('active');
        viewResults.style.display = 'none';
        viewSchema.style.display = 'block';
      });
    }
    // Check if opened via link or flag from Settings
    if (localStorage.getItem('a770_open_db') === '1' || window.location.hash === '#db') {
      localStorage.removeItem('a770_open_db');
      openDbModal();
    }
  }

  // Initialize once session is ready
  if (window.__sessionReady) {
    window.__sessionReady.then(() => {
      setupEventHandlers();
    });
  } else {
    document.addEventListener('DOMContentLoaded', setupEventHandlers);
  }

  window.openDbExplorer = openDbModal;
})();

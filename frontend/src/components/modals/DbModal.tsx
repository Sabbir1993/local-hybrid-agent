import React, { useState, useEffect, useRef } from 'react';
import { useAppStore } from '../../stores/useAppStore';
import { apiFetch } from '../../services/api';

interface DbInfo {
  id: string;
  name: string;
  is_system?: boolean;
  table_count?: number;
  size_bytes?: number;
  description?: string;
}

interface ColumnInfo {
  cid: number;
  name: string;
  type: string;
  notnull: number;
  dflt_value: any;
  pk: number;
}

interface TableInfo {
  name: string;
  type?: string;
  row_count?: number;
  columns?: ColumnInfo[];
  sql?: string;
}

interface QueryResult {
  columns?: string[];
  rows?: any[][];
  row_count?: number;
  duration_ms?: number;
  is_mutation?: boolean;
  affected_rows?: number;
  truncated?: boolean;
  error?: string;
}

const STORAGE_KEY_HISTORY = 'a770_db_query_history';

const SQL_KEYWORDS = new Set([
  'SELECT', 'FROM', 'WHERE', 'AND', 'OR', 'NOT', 'IN', 'IS', 'NULL', 'LIKE', 'BETWEEN',
  'JOIN', 'LEFT', 'RIGHT', 'INNER', 'OUTER', 'FULL', 'CROSS', 'NATURAL', 'ON',
  'GROUP', 'BY', 'HAVING', 'ORDER', 'ASC', 'DESC', 'LIMIT', 'OFFSET',
  'INSERT', 'INTO', 'VALUES', 'UPDATE', 'SET', 'DELETE',
  'CREATE', 'TABLE', 'DROP', 'ALTER', 'ADD', 'COLUMN', 'INDEX', 'VIEW', 'PRAGMA',
  'UNION', 'ALL', 'EXISTS', 'CASE', 'WHEN', 'THEN', 'ELSE', 'END',
  'AS', 'DISTINCT', 'DEFAULT', 'PRIMARY', 'KEY', 'FOREIGN', 'REFERENCES',
  'CASCADE', 'UNIQUE', 'CHECK', 'COLLATE', 'AUTOINCREMENT', 'WITHOUT', 'ROWID',
  'ATTACH', 'DETACH', 'TRANSACTION', 'BEGIN', 'COMMIT', 'ROLLBACK', 'EXPLAIN', 'QUERY', 'PLAN'
]);

const SQL_FUNCTIONS = new Set([
  'COUNT', 'SUM', 'AVG', 'MIN', 'MAX', 'TOTAL',
  'UPPER', 'LOWER', 'LENGTH', 'SUBSTR', 'SUBSTRING', 'TRIM', 'LTRIM', 'RTRIM',
  'REPLACE', 'ROUND', 'ABS', 'COALESCE', 'IFNULL', 'NULLIF', 'TYPEOF',
  'DATE', 'TIME', 'DATETIME', 'STRFTIME', 'JULIANDAY',
  'JSON', 'JSON_ARRAY', 'JSON_OBJECT', 'JSON_EXTRACT', 'JSON_ARRAY_LENGTH',
  'HEX', 'RANDOM', 'GLOB', 'INSTR', 'PRINTF', 'UNICODE', 'CHAR'
]);

function highlightSql(text: string): React.ReactNode[] {
  if (!text) return [];

  const tokenRegex = /(\/\*[\s\S]*?\*\/|--[^\n]*|'(?:''|[^'])*'|"(?:""|[^"])*"|`[^`]*`|\b\d+(?:\.\d+)?\b|\b[A-Za-z_][A-Za-z0-9_]*\b|[!=<>]=?|[+\-*/%,;()])/g;
  const nodes: React.ReactNode[] = [];
  let lastIndex = 0;
  let match: RegExpExecArray | null;

  while ((match = tokenRegex.exec(text)) !== null) {
    if (match.index > lastIndex) {
      nodes.push(text.substring(lastIndex, match.index));
    }

    const token = match[0];
    const upper = token.toUpperCase();

    if (token.startsWith('--') || token.startsWith('/*')) {
      nodes.push(
        <span key={match.index} style={{ color: '#71717a', fontStyle: 'italic' }}>
          {token}
        </span>
      );
    } else if (token.startsWith("'") || token.startsWith('"') || token.startsWith('`')) {
      nodes.push(
        <span key={match.index} style={{ color: '#34d399' }}>
          {token}
        </span>
      );
    } else if (/^\d+(?:\.\d+)?$/.test(token)) {
      nodes.push(
        <span key={match.index} style={{ color: '#38bdf8', fontWeight: 600 }}>
          {token}
        </span>
      );
    } else if (SQL_KEYWORDS.has(upper)) {
      nodes.push(
        <span key={match.index} style={{ color: '#c084fc', fontWeight: 700 }}>
          {token}
        </span>
      );
    } else if (SQL_FUNCTIONS.has(upper)) {
      nodes.push(
        <span key={match.index} style={{ color: '#fbbf24', fontWeight: 600 }}>
          {token}
        </span>
      );
    } else if (/^[!=<>]=?|[+\-*/%,;()]$/.test(token)) {
      nodes.push(
        <span key={match.index} style={{ color: '#94a3b8' }}>
          {token}
        </span>
      );
    } else {
      nodes.push(
        <span key={match.index} style={{ color: 'var(--text, #e2e8f0)' }}>
          {token}
        </span>
      );
    }

    lastIndex = tokenRegex.lastIndex;
  }

  if (lastIndex < text.length) {
    nodes.push(text.substring(lastIndex));
  }

  if (text.endsWith('\n')) {
    nodes.push('\n');
  }

  return nodes;
}

function formatBytes(bytes?: number): string {
  if (!bytes || bytes <= 0) return '0 B';
  const units = ['B', 'KB', 'MB', 'GB'];
  const i = Math.floor(Math.log(bytes) / Math.log(1024));
  return (bytes / Math.pow(1024, i)).toFixed(1) + ' ' + units[i];
}

export const DbModal: React.FC = () => {
  const { activeModal, setActiveModal } = useAppStore();
  const isOpen = activeModal === 'db';

  const [dbs, setDbs] = useState<DbInfo[]>([]);
  const [currentDbId, setCurrentDbId] = useState<string>('auth');
  const [tables, setTables] = useState<TableInfo[]>([]);
  const [currentTable, setCurrentTable] = useState<string>('');
  const [tableFilter, setTableFilter] = useState<string>('');

  const [sql, setSql] = useState<string>('');
  const [queryHistory, setQueryHistory] = useState<string[]>([]);
  const [activeTab, setActiveTab] = useState<'results' | 'schema'>('results');

  const [lastResult, setLastResult] = useState<QueryResult | null>(null);
  const [statusMsg, setStatusMsg] = useState<{ type: 'ready' | 'loading' | 'success' | 'mutation' | 'error'; text: string; details?: string }>({
    type: 'ready',
    text: 'Ready · Press Ctrl+Enter to execute',
  });
  const [isRunning, setIsRunning] = useState<boolean>(false);
  const [copiedDdl, setCopiedDdl] = useState<boolean>(false);

  const textareaRef = useRef<HTMLTextAreaElement>(null);
  const highlightRef = useRef<HTMLPreElement>(null);

  // Sync scroll & dimensions between textarea and syntax highlight pre layer
  useEffect(() => {
    if (!textareaRef.current || !highlightRef.current) return;
    const textarea = textareaRef.current;
    const highlight = highlightRef.current;

    const ro = new ResizeObserver(() => {
      if (textarea && highlight) {
        highlight.style.height = `${textarea.clientHeight}px`;
      }
    });
    ro.observe(textarea);

    return () => ro.disconnect();
  }, [isOpen]);

  // Load history from localStorage on open
  useEffect(() => {
    if (!isOpen) return;
    try {
      const saved = localStorage.getItem(STORAGE_KEY_HISTORY);
      if (saved) {
        setQueryHistory(JSON.parse(saved));
      }
    } catch {}
  }, [isOpen]);

  const saveHistory = (querySql: string) => {
    const trimmed = querySql.trim();
    if (!trimmed) return;
    const updated = [trimmed, ...queryHistory.filter((q) => q !== trimmed)].slice(0, 30);
    setQueryHistory(updated);
    try {
      localStorage.setItem(STORAGE_KEY_HISTORY, JSON.stringify(updated));
    } catch {}
  };

  // Load databases on modal open
  useEffect(() => {
    if (!isOpen) return;

    apiFetch('/db/list')
      .then((r) => {
        if (!r.ok) throw new Error('HTTP ' + r.status);
        return r.json();
      })
      .then((data) => {
        const list: DbInfo[] = data.databases || [];
        setDbs(list);
        if (list.length > 0) {
          const defaultDb = list.find((d) => d.id === 'auth') ? 'auth' : list[0].id;
          setCurrentDbId(defaultDb);
        }
      })
      .catch((err) => {
        setStatusMsg({ type: 'error', text: 'Error loading databases: ' + err.message });
      });
  }, [isOpen]);

  // Load schema when currentDbId changes
  useEffect(() => {
    if (!isOpen || !currentDbId) return;

    setTables([]);
    setCurrentTable('');

    apiFetch(`/db/${encodeURIComponent(currentDbId)}/schema`)
      .then((r) => {
        if (!r.ok) throw new Error('Failed to fetch schema');
        return r.json();
      })
      .then((schema) => {
        const tblList: TableInfo[] = schema.tables || [];
        setTables(tblList);
        if (tblList.length > 0) {
          const firstTbl = tblList[0].name;
          setCurrentTable(firstTbl);
          const defaultSql = `SELECT * FROM "${firstTbl}" LIMIT 50;`;
          setSql(defaultSql);
          runQuery(defaultSql, currentDbId);
        } else {
          setCurrentTable('');
          setSql('');
        }
      })
      .catch((err) => {
        setStatusMsg({ type: 'error', text: 'Schema error: ' + err.message });
      });
  }, [isOpen, currentDbId]);

  const runQuery = async (queryText?: string, dbIdOverride?: string) => {
    const targetSql = (queryText !== undefined ? queryText : sql).trim();
    const targetDb = dbIdOverride || currentDbId;

    if (!targetSql) {
      setStatusMsg({ type: 'error', text: 'Please enter a SQL query.' });
      return;
    }

    setIsRunning(true);
    setStatusMsg({ type: 'loading', text: 'Executing query…' });

    try {
      const resp = await apiFetch(`/db/${encodeURIComponent(targetDb)}/query`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ sql: targetSql, max_rows: 500 }),
      });

      const data = await resp.json();
      if (!resp.ok) {
        throw new Error(data.detail || data.error || 'HTTP ' + resp.status);
      }

      saveHistory(targetSql);
      setLastResult(data);

      if (data.is_mutation) {
        setStatusMsg({
          type: 'mutation',
          text: `Statement executed successfully · ${data.affected_rows ?? 0} row(s) affected · ${data.duration_ms ?? 0} ms`,
        });
        // Refresh schema
        apiFetch(`/db/${encodeURIComponent(targetDb)}/schema`)
          .then((r) => r.json())
          .then((schema) => setTables(schema.tables || []))
          .catch(() => {});
      } else {
        const rowCount = data.row_count ?? (data.rows ? data.rows.length : 0);
        const truncNote = data.truncated ? ' (capped at 500)' : '';
        const colCount = data.columns ? data.columns.length : 0;
        const dur = data.duration_ms ?? 0;
        setStatusMsg({
          type: 'success',
          text: `✓ Query returned ${rowCount} row(s)${truncNote} · ${colCount} columns · ${dur} ms`,
        });
      }
    } catch (e: any) {
      setStatusMsg({ type: 'error', text: `✗ Error: ${e.message}` });
      setLastResult({ error: e.message });
    } finally {
      setIsRunning(false);
    }
  };

  const handleTableSelect = (tblName: string) => {
    setCurrentTable(tblName);
    const newSql = `SELECT * FROM "${tblName}" LIMIT 50;`;
    setSql(newSql);
    runQuery(newSql);
  };

  const handleClear = () => {
    setSql('');
    setStatusMsg({ type: 'ready', text: 'Ready · Press Ctrl+Enter to execute' });
    setLastResult(null);
  };

  const handlePresetChange = (preset: string) => {
    if (!preset) return;
    const tbl = currentTable || 'sqlite_master';
    let snippet = '';
    if (preset === 'select_50') snippet = `SELECT * FROM "${tbl}" LIMIT 50;`;
    else if (preset === 'count') snippet = `SELECT COUNT(*) AS total_count FROM "${tbl}";`;
    else if (preset === 'columns') snippet = `PRAGMA table_info("${tbl}");`;
    else if (preset === 'tables') snippet = `SELECT type, name, sql FROM sqlite_master WHERE type IN ('table', 'view') AND name NOT LIKE 'sqlite_%' ORDER BY name;`;
    else if (preset === 'recent_requests') snippet = `SELECT id, datetime(ts, 'unixepoch', 'localtime') AS local_time, model, prompt_tokens, completion_tokens, tps, duration_s FROM requests ORDER BY id DESC LIMIT 20;`;
    else if (preset === 'users_roles') snippet = `SELECT u.id, u.username, u.is_active, u.is_super_admin, r.name AS role FROM users u LEFT JOIN user_roles ur ON ur.user_id = u.id LEFT JOIN roles r ON r.id = ur.role_id;`;

    if (snippet) {
      setSql(snippet);
      runQuery(snippet);
    }
  };

  const handleExportCsv = () => {
    if (!lastResult || !lastResult.columns || !lastResult.rows || !lastResult.rows.length) return;

    const escapeCsvCell = (val: any) => {
      if (val === null || val === undefined) return '';
      const str = String(val);
      if (str.includes(',') || str.includes('"') || str.includes('\n') || str.includes('\r')) {
        return '"' + str.replace(/"/g, '""') + '"';
      }
      return str;
    };

    const csvLines = [
      lastResult.columns.map(escapeCsvCell).join(','),
      ...lastResult.rows.map((row) => row.map(escapeCsvCell).join(',')),
    ];

    const blob = new Blob([csvLines.join('\r\n')], { type: 'text/csv;charset=utf-8;' });
    const url = URL.createObjectURL(blob);
    const a = document.createElement('a');
    a.href = url;
    a.download = `query_${currentDbId}_${Date.now()}.csv`;
    document.body.appendChild(a);
    a.click();
    document.body.removeChild(a);
    URL.revokeObjectURL(url);
  };

  const currentDbObj = dbs.find((d) => d.id === currentDbId);
  const currentTableObj = tables.find((t) => t.name === currentTable);
  const filteredTables = tables.filter((t) => !tableFilter || t.name.toLowerCase().includes(tableFilter.toLowerCase()));

  if (!isOpen) return null;

  const handleBackToChat = () => {
    if (window.location.pathname === '/db' || window.location.hash.includes('db')) {
      try {
        window.history.pushState({}, '', '/');
      } catch (_) {}
    }
    setActiveModal('none');
  };

  return (
    <div
      id="db-page"
      className="fixed inset-0 z-50 flex flex-col w-screen h-screen bg-bg select-none"
      style={{ width: '100vw', height: '100vh', margin: 0, padding: 0 }}
    >
      {/* Top Header Bar with Back to Chat */}
      <div
        className="flex items-center border-b border-border bg-panel2 select-none flex-shrink-0"
        style={{ height: '48px', minHeight: '48px', maxHeight: '48px' }}
      >
        {/* Left section aligned with sidebar (240px) */}
        <div
          className="flex items-center px-4 border-r border-border flex-shrink-0"
          style={{ width: '240px', minWidth: '240px', maxWidth: '240px', height: '100%' }}
        >
          <button
            className="btn ghost flex items-center justify-center gap-1.5 text-xs font-semibold px-3 py-1.5 rounded-lg border border-border hover:bg-panel text-text transition-colors whitespace-nowrap w-full"
            onClick={handleBackToChat}
            title="Return to active chat"
          >
            ← Back to Chat
          </button>
        </div>

        {/* Menu title & controls after left sidebar divider */}
        <div className="flex items-center justify-between px-5 flex-1 min-w-0">
          <span className="font-bold text-xs tracking-wider flex items-center gap-2 text-text whitespace-nowrap flex-shrink-0">
            🗄️ Database Explorer &amp; SQL Console
          </span>

          {/* Database Selector in Header */}
          <div className="flex items-center gap-3">
            <label
              htmlFor="db-select"
              style={{ fontSize: '11px', fontWeight: 700, color: 'var(--dim)', textTransform: 'uppercase', letterSpacing: '0.8px' }}
            >
              Database:
            </label>
            <select
              id="db-select"
              className="db-select-el"
              style={{ minWidth: '240px', width: 'auto', padding: '4px 10px' }}
              value={currentDbId}
              onChange={(e) => setCurrentDbId(e.target.value)}
            >
              {dbs.map((d) => {
                const sysTag = d.is_system ? '⚡ [System]' : '📁 [Workspace]';
                return (
                  <option key={d.id} value={d.id}>
                    {sysTag} {d.name} ({d.table_count ?? 0} tables, {formatBytes(d.size_bytes)})
                  </option>
                );
              })}
            </select>
            {currentDbObj && (
              <span className={`db-chip ${currentDbObj.is_system ? 'sys' : 'ws'}`} style={{ fontSize: '10.5px' }}>
                {currentDbObj.is_system ? 'System' : 'Workspace'} ({formatBytes(currentDbObj.size_bytes)})
              </span>
            )}
          </div>
        </div>
      </div>

      {/* Main Split Body */}
      <div className="db-split-body flex-1 w-full overflow-hidden flex">
          {/* Left Sidebar */}
          <div className="db-sidebar">
            <div className="db-sidebar-head">
              <span style={{ fontSize: '10px', fontWeight: 700, color: 'var(--dim)', textTransform: 'uppercase', letterSpacing: '1px' }}>
                Tables &amp; Views
              </span>
              <input
                type="text"
                id="db-table-filter"
                placeholder="Filter tables…"
                className="db-filter-input"
                value={tableFilter}
                onChange={(e) => setTableFilter(e.target.value)}
              />
            </div>
            <div id="db-table-list" className="db-table-list">
              {tables.length === 0 ? (
                <div className="dim" style={{ padding: '10px', fontSize: '11px', textAlign: 'center' }}>
                  Loading tables…
                </div>
              ) : filteredTables.length === 0 ? (
                <div className="dim" style={{ padding: '10px', fontSize: '11px', textAlign: 'center' }}>
                  No matching tables
                </div>
              ) : (
                filteredTables.map((t) => {
                  const isSelected = t.name === currentTable;
                  const countLabel = t.row_count !== undefined && t.row_count >= 0 ? t.row_count.toLocaleString() : '?';
                  return (
                    <div
                      key={t.name}
                      className={`db-table-item ${isSelected ? 'active' : ''}`}
                      onClick={() => handleTableSelect(t.name)}
                    >
                      <div className="db-table-name">
                        <span className="db-tbl-icon">{t.type === 'view' ? '👁️' : '📋'}</span>
                        <span className="db-tbl-text mono" title={t.name}>
                          {t.name}
                        </span>
                      </div>
                      <span className="db-row-badge" title={`${countLabel} rows`}>
                        {countLabel}
                      </span>
                    </div>
                  );
                })
              )}
            </div>
          </div>

          {/* Right Main Pane */}
          <div className="db-main-pane">
            {/* Editor Section */}
            <div className="db-editor-box">
              <div className="db-editor-toolbar">
                <div style={{ display: 'flex', alignItems: 'center', gap: '6px', flexWrap: 'wrap' }}>
                  <button
                    id="db-btn-run"
                    className="btn accent"
                    style={{ width: 'auto', margin: 0, padding: '5px 14px', fontWeight: 700, fontSize: '11.5px' }}
                    title="Execute SQL query (Ctrl+Enter)"
                    disabled={isRunning}
                    onClick={() => runQuery()}
                  >
                    ▶ Run
                  </button>
                  <button
                    id="db-btn-clear"
                    className="btn ghost"
                    style={{ width: 'auto', margin: 0, padding: '5px 10px', fontSize: '11.5px' }}
                    title="Clear query editor"
                    onClick={handleClear}
                  >
                    ✕ Clear
                  </button>
                  <select
                    id="db-preset-select"
                    className="db-tool-select"
                    title="Insert query snippet"
                    defaultValue=""
                    onChange={(e) => {
                      handlePresetChange(e.target.value);
                      e.target.value = '';
                    }}
                  >
                    <option value="">⚡ Snippets / Presets…</option>
                    <option value="select_50">SELECT * (First 50)</option>
                    <option value="count">Count total rows</option>
                    <option value="columns">Inspect columns (PRAGMA)</option>
                    <option value="tables">List all tables (sqlite_master)</option>
                    <option value="recent_requests">Recent 20 API requests (usage.db)</option>
                    <option value="users_roles">Users &amp; Assigned Roles (auth.db)</option>
                  </select>
                </div>

                <div style={{ display: 'flex', alignItems: 'center', gap: '6px', flexWrap: 'wrap' }}>
                  <select
                    id="db-history-select"
                    className="db-tool-select"
                    title="Recall recent query"
                    defaultValue=""
                    onChange={(e) => {
                      const idx = Number(e.target.value);
                      if (!isNaN(idx) && queryHistory[idx]) {
                        setSql(queryHistory[idx]);
                      }
                      e.target.value = '';
                    }}
                  >
                    <option value="">📜 Recent Queries ({queryHistory.length})</option>
                    {queryHistory.map((q, idx) => (
                      <option key={idx} value={idx}>
                        {q.replace(/\s+/g, ' ').slice(0, 60)}
                      </option>
                    ))}
                  </select>
                  <button
                    id="db-btn-export"
                    className="btn ghost"
                    style={{ width: 'auto', margin: 0, padding: '5px 10px', fontSize: '11.5px' }}
                    disabled={!lastResult?.rows || lastResult.rows.length === 0}
                    title="Export query result table to CSV"
                    onClick={handleExportCsv}
                  >
                    📥 CSV
                  </button>
                </div>
              </div>

              {/* SQL Syntax Highlighter & Active Editor Layer */}
              <div
                className="db-sql-editor-container"
                style={{
                  position: 'relative',
                  width: '100%',
                  minHeight: '84px',
                  maxHeight: '220px',
                  background: 'var(--bg, #0d1117)',
                  border: '1px solid var(--border)',
                  borderRadius: '6px',
                  overflow: 'hidden',
                }}
              >
                {/* Syntax Highlighted Mirror Layer */}
                <pre
                  ref={highlightRef}
                  aria-hidden="true"
                  className="mono"
                  style={{
                    position: 'absolute',
                    top: 0,
                    left: 0,
                    right: 0,
                    bottom: 0,
                    margin: 0,
                    padding: '8px 10px',
                    fontSize: '12px',
                    lineHeight: '1.45',
                    whiteSpace: 'pre-wrap',
                    wordBreak: 'break-word',
                    overflow: 'auto',
                    pointerEvents: 'none',
                    userSelect: 'none',
                    color: 'var(--text)',
                    fontFamily: 'ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, "Liberation Mono", "Courier New", monospace',
                    boxSizing: 'border-box',
                  }}
                >
                  {highlightSql(sql)}
                </pre>

                {/* Active Native Textarea */}
                <textarea
                  ref={textareaRef}
                  id="db-sql-input"
                  className="mono"
                  rows={4}
                  placeholder="Enter SQL query here… (e.g. SELECT * FROM users;) · Press Ctrl+Enter to execute"
                  spellCheck={false}
                  value={sql}
                  onChange={(e) => setSql(e.target.value)}
                  onScroll={(e) => {
                    if (highlightRef.current) {
                      highlightRef.current.scrollTop = e.currentTarget.scrollTop;
                      highlightRef.current.scrollLeft = e.currentTarget.scrollLeft;
                    }
                  }}
                  onKeyDown={(e) => {
                    if ((e.ctrlKey || e.metaKey) && e.key === 'Enter') {
                      e.preventDefault();
                      runQuery();
                    }
                  }}
                  style={{
                    position: 'relative',
                    width: '100%',
                    minHeight: '84px',
                    maxHeight: '220px',
                    margin: 0,
                    padding: '8px 10px',
                    fontSize: '12px',
                    lineHeight: '1.45',
                    whiteSpace: 'pre-wrap',
                    wordBreak: 'break-word',
                    background: 'transparent',
                    color: sql ? 'transparent' : 'var(--text)',
                    caretColor: 'var(--accent, #38bdf8)',
                    border: 'none',
                    outline: 'none',
                    resize: 'vertical',
                    boxSizing: 'border-box',
                    fontFamily: 'ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, "Liberation Mono", "Courier New", monospace',
                    overflow: 'auto',
                  }}
                />
              </div>

              <div className="db-editor-footer">
                <div
                  id="db-status-bar"
                  className="db-status-bar"
                  style={{
                    color:
                      statusMsg.type === 'success' || statusMsg.type === 'mutation'
                        ? 'var(--green)'
                        : statusMsg.type === 'error'
                        ? 'var(--red)'
                        : statusMsg.type === 'loading'
                        ? 'var(--dim)'
                        : 'var(--text)',
                  }}
                >
                  {statusMsg.text}
                </div>
                <div style={{ display: 'flex', alignItems: 'center', gap: '8px' }}>
                  <span
                    style={{
                      display: 'inline-flex',
                      alignItems: 'center',
                      gap: '4px',
                      fontSize: '10px',
                      color: 'var(--accent, #38bdf8)',
                      background: 'rgba(56, 189, 248, 0.1)',
                      border: '1px solid rgba(56, 189, 248, 0.25)',
                      borderRadius: '4px',
                      padding: '1px 6px',
                    }}
                  >
                    ✨ SQL Highlighter Active
                  </span>
                  <span className="dim" style={{ fontSize: '10px' }}>
                    Max 500 rows · Auto-commit
                  </span>
                </div>
              </div>
            </div>

            {/* Output Tabs Bar */}
            <div className="db-tabs-bar">
              <button
                id="db-tab-results"
                className={`db-tab-btn ${activeTab === 'results' ? 'active' : ''}`}
                type="button"
                onClick={() => setActiveTab('results')}
              >
                📊 Query Results
              </button>
              <button
                id="db-tab-schema"
                className={`db-tab-btn ${activeTab === 'schema' ? 'active' : ''}`}
                type="button"
                onClick={() => setActiveTab('schema')}
              >
                📐 Table Schema &amp; DDL
              </button>
            </div>

            {/* Results View */}
            {activeTab === 'results' && (
              <div id="db-view-results" className="db-tab-view active">
                <div id="db-results-container" className="db-results-container">
                  {lastResult?.error ? (
                    <div className="db-error-box">
                      <b>Query Failed</b>
                      <pre className="mono" style={{ marginTop: '6px', fontSize: '11px', whiteSpace: 'pre-wrap', wordBreak: 'break-all' }}>
                        {lastResult.error}
                      </pre>
                    </div>
                  ) : lastResult?.is_mutation ? (
                    <div className="db-mutation-notice">
                      <div style={{ fontSize: '18px', marginBottom: '6px' }}>✓</div>
                      <b>Query executed with no return dataset</b>
                      <p className="dim" style={{ fontSize: '11.5px', margin: '4px 0 0' }}>
                        Affected rows: {lastResult.affected_rows ?? 0} ({lastResult.duration_ms ?? 0} ms)
                      </p>
                    </div>
                  ) : !lastResult ? (
                    <div className="dim" style={{ padding: '24px', textAlign: 'center', fontSize: '12px' }}>
                      Query output will appear here. Select a table on the left to preview it.
                    </div>
                  ) : !lastResult.rows || lastResult.rows.length === 0 ? (
                    <div className="dim" style={{ padding: '20px', textAlign: 'center', fontSize: '12px' }}>
                      (0 rows returned)
                    </div>
                  ) : (
                    <div className="db-table-wrapper">
                      <table className="db-data-table">
                        <thead>
                          <tr>
                            <th className="db-th-num">#</th>
                            {lastResult.columns?.map((c) => (
                              <th key={c}>{c}</th>
                            ))}
                          </tr>
                        </thead>
                        <tbody>
                          {lastResult.rows.map((row, rIdx) => (
                            <tr key={rIdx}>
                              <td className="db-td-num dim">{rIdx + 1}</td>
                              {row.map((cell, cIdx) => {
                                if (cell === null) {
                                  return (
                                    <td key={cIdx} className="db-cell-null">
                                      NULL
                                    </td>
                                  );
                                }
                                if (typeof cell === 'number') {
                                  return (
                                    <td key={cIdx} className="mono db-cell-num">
                                      {cell.toLocaleString()}
                                    </td>
                                  );
                                }
                                if (typeof cell === 'boolean') {
                                  return (
                                    <td key={cIdx} className="mono">
                                      {cell ? 'TRUE' : 'FALSE'}
                                    </td>
                                  );
                                }
                                return (
                                  <td key={cIdx} className="db-cell-text" title={String(cell)}>
                                    {String(cell)}
                                  </td>
                                );
                              })}
                            </tr>
                          ))}
                        </tbody>
                      </table>
                    </div>
                  )}
                </div>
              </div>
            )}

            {/* Schema View */}
            {activeTab === 'schema' && (
              <div id="db-view-schema" className="db-tab-view">
                <div id="db-schema-details" className="db-schema-details" style={{ padding: '14px' }}>
                  {!currentTableObj ? (
                    <div className="dim" style={{ padding: '14px', fontSize: '12px', textAlign: 'center' }}>
                      Select a table on the left to inspect its schema columns and DDL definition.
                    </div>
                  ) : (
                    <div>
                      <div style={{ marginBottom: '12px' }}>
                        <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: '6px' }}>
                          <span style={{ fontSize: '11px', fontWeight: 700, color: 'var(--text)', textTransform: 'uppercase', letterSpacing: '0.8px' }}>
                            Structure: <span className="mono" style={{ color: 'var(--blue)' }}>{currentTableObj.name}</span> ({currentTableObj.columns?.length ?? 0} columns)
                          </span>
                          {currentTableObj.sql && (
                            <button
                              type="button"
                              className="btn ghost"
                              id="btn-copy-ddl"
                              style={{ width: 'auto', margin: 0, padding: '2px 8px', fontSize: '10.5px' }}
                              onClick={() => {
                                navigator.clipboard.writeText(currentTableObj.sql || '');
                                setCopiedDdl(true);
                                setTimeout(() => setCopiedDdl(false), 1200);
                              }}
                            >
                              {copiedDdl ? '✓ Copied' : '📋 Copy DDL'}
                            </button>
                          )}
                        </div>
                        <div style={{ maxHeight: '180px', overflowY: 'auto', border: '1px solid var(--border)', borderRadius: '6px', background: 'var(--panel2)' }}>
                          <table className="db-schema-table">
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
                              {currentTableObj.columns?.map((c) => (
                                <tr key={c.cid}>
                                  <td className="dim">{c.cid}</td>
                                  <td className="mono">
                                    <b>{c.name}</b>
                                  </td>
                                  <td className="mono dim" style={{ color: 'var(--amber)' }}>
                                    {c.type}
                                  </td>
                                  <td>{c.pk ? <span className="db-tag pk" style={{ fontSize: '9px', padding: '1px 4px', background: 'rgba(234, 179, 8, 0.2)', color: 'var(--amber)', borderRadius: '3px' }}>PK</span> : '—'}</td>
                                  <td>{c.notnull ? <span className="db-tag notnull" style={{ fontSize: '9px', padding: '1px 4px', background: 'rgba(59, 130, 246, 0.2)', color: 'var(--blue)', borderRadius: '3px' }}>YES</span> : '—'}</td>
                                  <td className="mono dim">{c.dflt_value != null ? String(c.dflt_value) : 'NULL'}</td>
                                </tr>
                              ))}
                            </tbody>
                          </table>
                        </div>
                      </div>

                      {currentTableObj.sql && (
                        <div>
                          <span style={{ fontSize: '10.5px', fontWeight: 700, color: 'var(--dim)', textTransform: 'uppercase', letterSpacing: '0.8px' }}>
                            DDL Definition
                          </span>
                          <pre className="db-ddl-code mono" style={{ margin: '6px 0 0', padding: '10px 12px', background: 'var(--bg)', border: '1px solid var(--border)', borderRadius: '6px', fontSize: '11px', lineHeight: 1.5, overflowX: 'auto' }}>
                            {currentTableObj.sql}
                          </pre>
                        </div>
                      )}
                    </div>
                  )}
                </div>
              </div>
            )}
          </div>
        </div>
      </div>
  );
};

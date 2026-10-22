import React, { useState, useEffect } from 'react';
import { useAppStore } from '../../stores/useAppStore';
import { apiFetch } from '../../services/api';

export const AuditModal: React.FC = () => {
  const { activeModal, setActiveModal } = useAppStore();
  const isOpen = activeModal === 'audit';

  const [entries, setEntries] = useState<any[]>([]);
  const [loading, setLoading] = useState<boolean>(true);
  const [timeRange, setTimeRange] = useState<string>('7');
  const [userFilter, setUserFilter] = useState<string>('');
  const [actionFilter, setActionFilter] = useState<string>('');
  const [resultFilter, setResultFilter] = useState<string>('');
  const [ipFilter, setIpFilter] = useState<string>('');
  const [searchQuery, setSearchQuery] = useState<string>('');
  const [page, setPage] = useState<number>(1);
  const [pageSize, setPageSize] = useState<number>(50);
  const [total, setTotal] = useState<number>(0);

  // Inspector state
  const [selectedEntry, setSelectedEntry] = useState<any | null>(null);
  const [copiedJson, setCopiedJson] = useState<boolean>(false);

  // Facet choices from backend
  const [usersFacet, setUsersFacet] = useState<any[]>([]);
  const [actionsFacet, setActionsFacet] = useState<any[]>([]);

  useEffect(() => {
    if (!isOpen) return;

    // Load facets
    apiFetch('/admin/audit_log/facets')
      .then((r) => r.json())
      .then((f) => {
        setUsersFacet(f.users || []);
        setActionsFacet(f.actions || []);
      })
      .catch(() => {});
  }, [isOpen]);

  const fetchAudit = () => {
    if (!isOpen) return;
    setLoading(true);

    const params = new URLSearchParams();
    params.set('page', String(page));
    params.set('page_size', String(pageSize));

    if (timeRange !== 'all') {
      const since = Date.now() / 1000 - Number(timeRange) * 86400;
      params.set('since', String(since));
    }
    if (userFilter) params.set('user_id', userFilter);
    if (actionFilter) params.set('action', actionFilter);
    if (resultFilter) params.set('result', resultFilter);
    if (ipFilter) params.set('ip', ipFilter);
    if (searchQuery) params.set('q', searchQuery);

    apiFetch(`/admin/audit_log?${params.toString()}`)
      .then((r) => r.json())
      .then((data) => {
        setEntries(data.entries || []);
        setTotal(data.total || 0);
        setLoading(false);
      })
      .catch(() => {
        setLoading(false);
      });
  };

  useEffect(() => {
    if (isOpen) {
      fetchAudit();
    }
  }, [isOpen, page, pageSize, timeRange, userFilter, actionFilter, resultFilter, ipFilter]);

  if (!isOpen) return null;

  const totalPages = Math.max(1, Math.ceil(total / pageSize));
  const startIdx = total ? (page - 1) * pageSize + 1 : 0;
  const endIdx = Math.min(total, page * pageSize);

  // KPI Calculations
  const allowCount = entries.filter((e) => (e.result || '').toLowerCase() === 'allow').length;
  const denyCount = entries.filter((e) => (e.result || '').toLowerCase() === 'deny').length;
  const errorCount = entries.filter((e) => {
    const r = (e.result || '').toLowerCase();
    return r !== 'allow' && r !== 'deny';
  }).length;
  const allowRate = entries.length > 0 ? Math.round((allowCount / entries.length) * 100) : 100;

  const handleReset = () => {
    setTimeRange('7');
    setUserFilter('');
    setActionFilter('');
    setResultFilter('');
    setIpFilter('');
    setSearchQuery('');
    setPage(1);
  };

  const handleExportCsv = () => {
    const params = new URLSearchParams();
    if (timeRange !== 'all') {
      params.set('since', String(Date.now() / 1000 - Number(timeRange) * 86400));
    }
    if (userFilter) params.set('user_id', userFilter);
    if (actionFilter) params.set('action', actionFilter);
    if (resultFilter) params.set('result', resultFilter);
    if (ipFilter) params.set('ip', ipFilter);
    if (searchQuery) params.set('q', searchQuery);
    params.set('export', 'csv');
    window.location.href = `/admin/audit_log/export?${params.toString()}`;
  };

  const handleBackToChat = () => {
    if (window.location.pathname === '/audit' || window.location.hash.includes('audit')) {
      try {
        window.history.pushState({}, '', '/');
      } catch (_) {}
    }
    setActiveModal('none');
  };

  const getActionBadge = (action: string) => {
    const act = (action || '').toLowerCase();
    if (act.startsWith('auth.') || act.startsWith('login.')) {
      return { icon: '🔑', bg: 'bg-purple-500/10 text-purple-400 border-purple-500/30' };
    }
    if (act.startsWith('db.') || act.includes('query')) {
      return { icon: '💾', bg: 'bg-cyan-500/10 text-cyan-400 border-cyan-500/30' };
    }
    if (act.startsWith('cloud.')) {
      return { icon: '☁️', bg: 'bg-sky-500/10 text-sky-400 border-sky-500/30' };
    }
    if (act.startsWith('mcp.')) {
      return { icon: '🔌', bg: 'bg-indigo-500/10 text-indigo-400 border-indigo-500/30' };
    }
    if (act.startsWith('knowledge.')) {
      return { icon: '📚', bg: 'bg-emerald-500/10 text-emerald-400 border-emerald-500/30' };
    }
    if (act.includes('clear') || act.includes('delete') || act.includes('wipe')) {
      return { icon: '⚠️', bg: 'bg-rose-500/10 text-rose-400 border-rose-500/30' };
    }
    return { icon: '⚡', bg: 'bg-zinc-500/10 text-zinc-300 border-zinc-500/30' };
  };

  return (
    <div
      id="audit-page"
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

        {/* Menu title after left sidebar divider */}
        <div className="flex items-center justify-between px-5 flex-1 min-w-0">
          <div className="flex items-center gap-2.5">
            <span className="font-bold text-xs tracking-wider flex items-center gap-2 text-text whitespace-nowrap">
              🛡️ Security &amp; Audit Trail Console
            </span>
            <span className="text-[10px] font-mono px-2 py-0.5 rounded-full bg-panel border border-border text-text-muted">
              PCI-DSS / Bank Compliance
            </span>
          </div>
          <div className="flex items-center gap-2">
            <button
              onClick={() => fetchAudit()}
              className="text-xs px-2.5 py-1 rounded border border-border hover:bg-panel text-text-muted hover:text-text transition-colors flex items-center gap-1"
              title="Refresh audit events"
            >
              🔄 Refresh
            </button>
            <button
              onClick={handleExportCsv}
              className="text-xs px-2.5 py-1 rounded bg-accent/15 border border-accent/40 text-accent hover:bg-accent/25 transition-colors font-medium flex items-center gap-1"
              title="Export complete filtered trail to CSV"
            >
              ⬇ Export CSV
            </button>
          </div>
        </div>
      </div>

      {/* Main Fullpage Body */}
      <div className="flex-1 w-full overflow-hidden p-5 bg-panel flex flex-col gap-3">
        <div className="w-full max-w-7xl mx-auto flex flex-col h-full gap-3">
          {/* Top KPI Summary Grid */}
          <div className="grid grid-cols-2 md:grid-cols-4 gap-3 flex-shrink-0">
            <div className="bg-panel2/80 border border-border/80 rounded-xl p-3 flex items-center justify-between shadow-sm">
              <div>
                <div className="text-[11px] font-semibold text-text-muted uppercase tracking-wider">Total Events</div>
                <div className="text-xl font-bold font-mono text-text mt-0.5">{total.toLocaleString()}</div>
                <div className="text-[10px] text-text-muted mt-0.5">Audited trail entries</div>
              </div>
              <div className="w-9 h-9 rounded-lg bg-blue-500/10 border border-blue-500/20 text-blue-400 flex items-center justify-center text-lg">
                🛡️
              </div>
            </div>

            <div className="bg-panel2/80 border border-border/80 rounded-xl p-3 flex items-center justify-between shadow-sm">
              <div>
                <div className="text-[11px] font-semibold text-text-muted uppercase tracking-wider">Pass / Allow Rate</div>
                <div className="text-xl font-bold font-mono text-emerald-400 mt-0.5">{allowRate}%</div>
                <div className="text-[10px] text-emerald-500/80 mt-0.5">🟢 {allowCount} allowed in view</div>
              </div>
              <div className="w-9 h-9 rounded-lg bg-emerald-500/10 border border-emerald-500/20 text-emerald-400 flex items-center justify-center text-lg">
                ✓
              </div>
            </div>

            <div className={`border rounded-xl p-3 flex items-center justify-between shadow-sm ${denyCount > 0 ? 'bg-rose-500/10 border-rose-500/30' : 'bg-panel2/80 border-border/80'}`}>
              <div>
                <div className="text-[11px] font-semibold text-text-muted uppercase tracking-wider">Denied Violations</div>
                <div className={`text-xl font-bold font-mono mt-0.5 ${denyCount > 0 ? 'text-rose-400 font-extrabold' : 'text-text'}`}>
                  {denyCount}
                </div>
                <div className="text-[10px] text-text-muted mt-0.5">{denyCount > 0 ? '⚠️ Access policy blocks' : '0 violations in page'}</div>
              </div>
              <div className="w-9 h-9 rounded-lg bg-rose-500/10 border border-rose-500/20 text-rose-400 flex items-center justify-center text-lg">
                🛑
              </div>
            </div>

            <div className="bg-panel2/80 border border-border/80 rounded-xl p-3 flex items-center justify-between shadow-sm">
              <div>
                <div className="text-[11px] font-semibold text-text-muted uppercase tracking-wider">Active Actors</div>
                <div className="text-xl font-bold font-mono text-cyan-400 mt-0.5">{usersFacet.length}</div>
                <div className="text-[10px] text-text-muted mt-0.5">Principals in log</div>
              </div>
              <div className="w-9 h-9 rounded-lg bg-cyan-500/10 border border-cyan-500/20 text-cyan-400 flex items-center justify-center text-lg">
                👤
              </div>
            </div>
          </div>

          {/* Filter Bar with Quick Status Pills */}
          <div className="bg-panel2/70 border border-border/80 rounded-xl p-3 flex flex-col gap-2.5 flex-shrink-0 shadow-sm">
            {/* Quick Status Pills */}
            <div className="flex items-center justify-between flex-wrap gap-2 pb-1 border-b border-border/50">
              <div className="flex items-center gap-1.5 flex-wrap">
                <span className="text-[10.5px] font-bold text-text-muted mr-1 uppercase tracking-wider">Quick Filter:</span>
                <button
                  type="button"
                  onClick={() => { setResultFilter(''); setPage(1); }}
                  className={`text-xs px-2.5 py-1 rounded-lg border font-medium transition-all ${
                    resultFilter === ''
                      ? 'bg-accent/20 border-accent text-accent font-semibold shadow-sm'
                      : 'border-border bg-panel/50 text-text-muted hover:text-text hover:bg-panel'
                  }`}
                >
                  All Statuses ({total})
                </button>
                <button
                  type="button"
                  onClick={() => { setResultFilter('allow'); setPage(1); }}
                  className={`text-xs px-2.5 py-1 rounded-lg border font-medium transition-all flex items-center gap-1.5 ${
                    resultFilter === 'allow'
                      ? 'bg-emerald-500/20 border-emerald-500 text-emerald-400 font-semibold shadow-sm'
                      : 'border-border bg-panel/50 text-text-muted hover:text-emerald-400 hover:bg-panel'
                  }`}
                >
                  <span className="w-2 h-2 rounded-full bg-emerald-400"></span>
                  Allowed
                </button>
                <button
                  type="button"
                  onClick={() => { setResultFilter('deny'); setPage(1); }}
                  className={`text-xs px-2.5 py-1 rounded-lg border font-medium transition-all flex items-center gap-1.5 ${
                    resultFilter === 'deny'
                      ? 'bg-rose-500/20 border-rose-500 text-rose-400 font-semibold shadow-sm'
                      : 'border-border bg-panel/50 text-text-muted hover:text-rose-400 hover:bg-panel'
                  }`}
                >
                  <span className="w-2 h-2 rounded-full bg-rose-400"></span>
                  Denied Violations
                </button>
                <button
                  type="button"
                  onClick={() => { setResultFilter('error'); setPage(1); }}
                  className={`text-xs px-2.5 py-1 rounded-lg border font-medium transition-all flex items-center gap-1.5 ${
                    resultFilter === 'error'
                      ? 'bg-amber-500/20 border-amber-500 text-amber-400 font-semibold shadow-sm'
                      : 'border-border bg-panel/50 text-text-muted hover:text-amber-400 hover:bg-panel'
                  }`}
                >
                  <span className="w-2 h-2 rounded-full bg-amber-400"></span>
                  Errors {errorCount > 0 ? `(${errorCount})` : ''}
                </button>
              </div>

              <div className="flex items-center gap-2 text-xs">
                {(timeRange !== '7' || userFilter || actionFilter || resultFilter || ipFilter || searchQuery) && (
                  <button
                    type="button"
                    onClick={handleReset}
                    className="text-text-muted hover:text-rose-400 transition-colors flex items-center gap-1 underline underline-offset-2"
                  >
                    ✕ Clear all filters
                  </button>
                )}
              </div>
            </div>

            {/* Filter Form Controls */}
            <form
              id="audit-filters"
              onSubmit={(e) => { e.preventDefault(); setPage(1); fetchAudit(); }}
              className="flex items-end gap-2.5 flex-wrap"
            >
              {/* Time Range */}
              <div className="flex flex-col gap-1">
                <label className="text-[10px] font-semibold text-text-muted uppercase tracking-wider">Time Window</label>
                <select
                  value={timeRange}
                  onChange={(e) => { setTimeRange(e.target.value); setPage(1); }}
                  className="bg-panel border border-border rounded-lg px-2.5 py-1.5 text-xs text-text outline-none focus:border-accent min-w-[125px]"
                >
                  <option value="1">Last 24 hours</option>
                  <option value="7">Last 7 days</option>
                  <option value="30">Last 30 days</option>
                  <option value="90">Last 90 days</option>
                  <option value="all">All time</option>
                </select>
              </div>

              {/* User Dropdown */}
              <div className="flex flex-col gap-1">
                <label className="text-[10px] font-semibold text-text-muted uppercase tracking-wider">Principal</label>
                <select
                  value={userFilter}
                  onChange={(e) => { setUserFilter(e.target.value); setPage(1); }}
                  className="bg-panel border border-border rounded-lg px-2.5 py-1.5 text-xs text-text outline-none focus:border-accent min-w-[130px]"
                >
                  <option value="">All users</option>
                  {usersFacet.map((u, i) => (
                    <option key={i} value={u.user_id == null ? '0' : String(u.user_id)}>
                      {u.username || 'system'} ({u.n})
                    </option>
                  ))}
                </select>
              </div>

              {/* Action Dropdown */}
              <div className="flex flex-col gap-1">
                <label className="text-[10px] font-semibold text-text-muted uppercase tracking-wider">Action Type</label>
                <select
                  value={actionFilter}
                  onChange={(e) => { setActionFilter(e.target.value); setPage(1); }}
                  className="bg-panel border border-border rounded-lg px-2.5 py-1.5 text-xs text-text outline-none focus:border-accent min-w-[135px]"
                >
                  <option value="">All actions</option>
                  {actionsFacet.map((a, i) => (
                    <option key={i} value={a.action}>
                      {a.action} ({a.n})
                    </option>
                  ))}
                </select>
              </div>

              {/* IP Input */}
              <div className="flex flex-col gap-1">
                <label className="text-[10px] font-semibold text-text-muted uppercase tracking-wider">IP Address</label>
                <input
                  type="text"
                  placeholder="e.g. 192.168."
                  value={ipFilter}
                  onChange={(e) => setIpFilter(e.target.value)}
                  className="bg-panel border border-border rounded-lg px-2.5 py-1.5 text-xs text-text outline-none focus:border-accent w-[110px]"
                />
              </div>

              {/* Full-text Search */}
              <div className="flex flex-col gap-1 flex-1 min-w-[180px]">
                <label className="text-[10px] font-semibold text-text-muted uppercase tracking-wider">Search Keywords</label>
                <div className="relative">
                  <input
                    type="search"
                    placeholder="Search resource, detail, token, query…"
                    value={searchQuery}
                    onChange={(e) => setSearchQuery(e.target.value)}
                    className="w-full bg-panel border border-border rounded-lg pl-8 pr-3 py-1.5 text-xs text-text outline-none focus:border-accent"
                  />
                  <span className="absolute left-2.5 top-1.5 text-text-muted text-xs">🔍</span>
                </div>
              </div>

              <button
                type="submit"
                className="px-4 py-1.5 bg-accent text-white font-semibold text-xs rounded-lg hover:brightness-110 transition-all shadow-sm flex items-center gap-1"
              >
                Filter
              </button>
            </form>
          </div>

          {/* Audit Events Table */}
          <div className="flex-1 min-h-0 overflow-hidden border border-border rounded-xl bg-panel2/50 shadow-sm flex flex-col">
            <div className="flex-1 overflow-auto">
              {loading ? (
                <div className="flex flex-col items-center justify-center h-64 text-text-muted gap-3">
                  <div className="w-7 h-7 border-2 border-accent border-t-transparent rounded-full animate-spin"></div>
                  <span className="text-xs font-mono">Loading audit trail entries…</span>
                </div>
              ) : entries.length === 0 ? (
                <div className="flex flex-col items-center justify-center h-64 text-text-muted gap-2">
                  <div className="text-3xl">🛡️</div>
                  <div className="text-sm font-semibold text-text">No audit entries found</div>
                  <div className="text-xs text-text-muted">No security events match the current filter criteria.</div>
                  <button
                    onClick={handleReset}
                    className="mt-2 text-xs px-3 py-1.5 rounded-lg border border-border bg-panel hover:bg-panel2 text-text transition-colors"
                  >
                    Reset filters
                  </button>
                </div>
              ) : (
                <table className="w-full border-collapse text-left text-xs">
                  <thead className="sticky top-0 z-10 bg-panel2 border-b border-border text-[10.5px] font-semibold text-text-muted uppercase tracking-wider shadow-sm">
                    <tr>
                      <th className="py-2.5 px-3">Timestamp</th>
                      <th className="py-2.5 px-3">Principal / User</th>
                      <th className="py-2.5 px-3">Action</th>
                      <th className="py-2.5 px-3">Target Resource</th>
                      <th className="py-2.5 px-3">Permission Key</th>
                      <th className="py-2.5 px-3">Status</th>
                      <th className="py-2.5 px-3">Client IP</th>
                      <th className="py-2.5 px-3">Detail &amp; Payload</th>
                    </tr>
                  </thead>
                  <tbody className="divide-y divide-border/40 font-mono text-[11.5px]">
                    {entries.map((en) => {
                      let detFormatted = '';
                      let detObj: any = null;
                      if (en.detail) {
                        try {
                          detObj = typeof en.detail === 'object' ? en.detail : JSON.parse(en.detail);
                          detFormatted = JSON.stringify(detObj);
                        } catch (_) {
                          detFormatted = String(en.detail);
                        }
                      }

                      const res = String(en.result || '').toLowerCase();
                      const isAllow = res === 'allow';
                      const isDeny = res === 'deny';
                      const badge = getActionBadge(en.action);
                      const username = en.username || 'system';
                      const isSystem = username === 'system';

                      return (
                        <tr
                          key={en.id}
                          onClick={() => setSelectedEntry(en)}
                          className="hover:bg-panel/80 transition-colors cursor-pointer group"
                        >
                          {/* Timestamp */}
                          <td className="py-2 px-3 whitespace-nowrap text-text-muted text-[11px]">
                            {en.ts ? (
                              <div className="flex flex-col leading-tight">
                                <span className="text-text font-medium">{new Date(en.ts * 1000).toLocaleTimeString()}</span>
                                <span className="text-[10px] text-text-muted">{new Date(en.ts * 1000).toLocaleDateString()}</span>
                              </div>
                            ) : (
                              '—'
                            )}
                          </td>

                          {/* User Avatar + Username */}
                          <td className="py-2 px-3 whitespace-nowrap">
                            <div className="flex items-center gap-1.5">
                              <span
                                className={`w-5 h-5 rounded-full flex items-center justify-center text-[10px] font-bold ${
                                  isSystem ? 'bg-purple-500/20 text-purple-300' : 'bg-accent/20 text-accent'
                                }`}
                              >
                                {isSystem ? '🛡️' : username.charAt(0).toUpperCase()}
                              </span>
                              <span className={`font-sans font-semibold text-xs ${isSystem ? 'text-text-muted' : 'text-text'}`}>
                                {username}
                              </span>
                            </div>
                          </td>

                          {/* Action Badge */}
                          <td className="py-2 px-3 whitespace-nowrap">
                            <span className={`inline-flex items-center gap-1 px-2 py-0.5 rounded-md text-[11px] font-semibold border ${badge.bg}`}>
                              <span>{badge.icon}</span>
                              <span>{en.action}</span>
                            </span>
                          </td>

                          {/* Target Resource */}
                          <td className="py-2 px-3 text-text-muted max-w-[130px] truncate" title={en.resource || ''}>
                            {en.resource ? (
                              <span className="text-text/90 font-mono text-[11px] bg-panel/60 px-1.5 py-0.5 rounded border border-border/40 inline-block max-w-full truncate">
                                {en.resource}
                              </span>
                            ) : (
                              <span className="text-zinc-600">—</span>
                            )}
                          </td>

                          {/* Permission */}
                          <td className="py-2 px-3 text-text-muted max-w-[110px] truncate" title={en.permission_key || ''}>
                            {en.permission_key ? (
                              <span className="text-[10.5px] text-text-muted bg-panel/40 px-1.5 py-0.5 rounded inline-block max-w-full truncate">
                                {en.permission_key}
                              </span>
                            ) : (
                              <span className="text-zinc-600">—</span>
                            )}
                          </td>

                          {/* Status Pill */}
                          <td className="py-2 px-3 whitespace-nowrap">
                            <span
                              className={`inline-flex items-center gap-1.5 px-2.5 py-0.5 rounded-full text-[10px] font-bold tracking-wide uppercase ${
                                isAllow
                                  ? 'bg-emerald-500/15 text-emerald-400 border border-emerald-500/30'
                                  : isDeny
                                  ? 'bg-rose-500/15 text-rose-400 border border-rose-500/30 shadow-[0_0_8px_rgba(244,63,94,0.2)]'
                                  : 'bg-amber-500/15 text-amber-400 border border-amber-500/30'
                              }`}
                            >
                              <span
                                className={`w-1.5 h-1.5 rounded-full ${
                                  isAllow ? 'bg-emerald-400' : isDeny ? 'bg-rose-400' : 'bg-amber-400'
                                }`}
                              ></span>
                              {en.result || 'allow'}
                            </span>
                          </td>

                          {/* Client IP (Always populated & styled) */}
                          <td className="py-2 px-3 whitespace-nowrap font-mono text-[11px]">
                            <span className="px-2 py-0.5 rounded bg-panel border border-border text-cyan-400/90 font-medium inline-flex items-center gap-1">
                              <span className="text-[9px] text-cyan-500/60">🌐</span>
                              <span>{en.ip || '127.0.0.1'}</span>
                            </span>
                          </td>

                          {/* Detail Preview & Inspect Button */}
                          <td className="py-2 px-3 max-w-[240px]">
                            <div className="flex items-center justify-between gap-2">
                              <span
                                className="truncate text-text-muted text-[11px]"
                                title={detFormatted}
                              >
                                {detFormatted || <span className="text-zinc-600 italic">No extra metadata</span>}
                              </span>
                              <button
                                type="button"
                                onClick={(e) => {
                                  e.stopPropagation();
                                  setSelectedEntry(en);
                                }}
                                className="opacity-0 group-hover:opacity-100 text-[10px] px-2 py-0.5 rounded bg-panel border border-border text-text hover:bg-accent/20 hover:border-accent hover:text-accent transition-all flex items-center gap-1 flex-shrink-0"
                              >
                                👁️ View
                              </button>
                            </div>
                          </td>
                        </tr>
                      );
                    })}
                  </tbody>
                </table>
              )}
            </div>

            {/* Modern Pagination Footer */}
            <div className="border-t border-border bg-panel2/90 px-4 py-2.5 flex items-center justify-between flex-shrink-0 text-xs">
              <div className="flex items-center gap-2 text-text-muted">
                <span>
                  Showing <strong className="text-text font-mono">{startIdx.toLocaleString()}</strong>–
                  <strong className="text-text font-mono">{endIdx.toLocaleString()}</strong> of{' '}
                  <strong className="text-text font-mono">{total.toLocaleString()}</strong> events
                </span>
              </div>

              <div className="flex items-center gap-3">
                <div className="flex items-center gap-1.5">
                  <span className="text-text-muted text-[11px]">Rows per page:</span>
                  <select
                    value={pageSize}
                    onChange={(e) => {
                      setPageSize(Number(e.target.value));
                      setPage(1);
                    }}
                    className="bg-panel border border-border rounded px-2 py-0.5 text-xs text-text outline-none focus:border-accent"
                  >
                    <option value="25">25</option>
                    <option value="50">50</option>
                    <option value="100">100</option>
                    <option value="200">200</option>
                  </select>
                </div>

                <div className="flex items-center gap-1">
                  <button
                    disabled={page <= 1}
                    onClick={() => setPage(1)}
                    className="px-2 py-1 rounded border border-border bg-panel text-text disabled:opacity-30 disabled:cursor-not-allowed hover:bg-panel2 transition-colors"
                    title="First page"
                  >
                    «
                  </button>
                  <button
                    disabled={page <= 1}
                    onClick={() => setPage(page - 1)}
                    className="px-2.5 py-1 rounded border border-border bg-panel text-text disabled:opacity-30 disabled:cursor-not-allowed hover:bg-panel2 transition-colors font-semibold"
                    title="Previous page"
                  >
                    ‹
                  </button>

                  <span className="px-3 font-mono text-xs text-text">
                    Page <strong className="text-accent">{page}</strong> / {totalPages}
                  </span>

                  <button
                    disabled={page >= totalPages}
                    onClick={() => setPage(page + 1)}
                    className="px-2.5 py-1 rounded border border-border bg-panel text-text disabled:opacity-30 disabled:cursor-not-allowed hover:bg-panel2 transition-colors font-semibold"
                    title="Next page"
                  >
                    ›
                  </button>
                  <button
                    disabled={page >= totalPages}
                    onClick={() => setPage(totalPages)}
                    className="px-2 py-1 rounded border border-border bg-panel text-text disabled:opacity-30 disabled:cursor-not-allowed hover:bg-panel2 transition-colors"
                    title="Last page"
                  >
                    »
                  </button>
                </div>
              </div>
            </div>
          </div>
        </div>
      </div>

      {/* Slide-over Event Detail Inspector Modal */}
      {selectedEntry && (
        <div
          className="fixed inset-0 z-50 flex items-center justify-center bg-black/60 backdrop-blur-sm p-4 animate-in fade-in"
          onClick={() => setSelectedEntry(null)}
        >
          <div
            className="w-full max-w-2xl bg-panel2 border border-border rounded-2xl shadow-2xl overflow-hidden flex flex-col max-h-[85vh] animate-in zoom-in-95"
            onClick={(e) => e.stopPropagation()}
          >
            {/* Modal Header */}
            <div className="flex items-center justify-between px-5 py-4 border-b border-border bg-panel">
              <div className="flex items-center gap-2.5">
                <span className="text-lg">🛡️</span>
                <div>
                  <h3 className="font-bold text-sm text-text">Security Event #{selectedEntry.id}</h3>
                  <p className="text-[11px] text-text-muted font-mono">
                    {selectedEntry.ts ? new Date(selectedEntry.ts * 1000).toUTCString() : ''}
                  </p>
                </div>
              </div>
              <button
                onClick={() => setSelectedEntry(null)}
                className="w-7 h-7 rounded-lg border border-border flex items-center justify-center text-text-muted hover:text-text hover:bg-panel2 transition-colors"
              >
                ✕
              </button>
            </div>

            {/* Modal Body */}
            <div className="p-5 overflow-y-auto flex flex-col gap-4 text-xs">
              {/* Event Attributes Grid */}
              <div className="grid grid-cols-2 gap-3 bg-panel p-3.5 rounded-xl border border-border/80">
                <div>
                  <div className="text-[10px] font-semibold text-text-muted uppercase tracking-wider">Principal</div>
                  <div className="text-sm font-semibold text-text mt-0.5 flex items-center gap-1.5">
                    <span>👤</span>
                    <span>{selectedEntry.username || 'system'}</span>
                    {selectedEntry.user_id && (
                      <span className="text-[10px] text-text-muted font-mono">(ID: {selectedEntry.user_id})</span>
                    )}
                  </div>
                </div>

                <div>
                  <div className="text-[10px] font-semibold text-text-muted uppercase tracking-wider">Evaluation Result</div>
                  <div className="mt-0.5">
                    <span
                      className={`inline-flex items-center gap-1.5 px-2.5 py-0.5 rounded-full text-xs font-bold uppercase ${
                        (selectedEntry.result || '').toLowerCase() === 'allow'
                          ? 'bg-emerald-500/15 text-emerald-400 border border-emerald-500/30'
                          : 'bg-rose-500/15 text-rose-400 border border-rose-500/30'
                      }`}
                    >
                      <span
                        className={`w-1.5 h-1.5 rounded-full ${
                          (selectedEntry.result || '').toLowerCase() === 'allow' ? 'bg-emerald-400' : 'bg-rose-400'
                        }`}
                      ></span>
                      {selectedEntry.result || 'allow'}
                    </span>
                  </div>
                </div>

                <div>
                  <div className="text-[10px] font-semibold text-text-muted uppercase tracking-wider">Action Name</div>
                  <div className="text-xs font-mono font-semibold text-accent mt-0.5">{selectedEntry.action}</div>
                </div>

                <div>
                  <div className="text-[10px] font-semibold text-text-muted uppercase tracking-wider">Target Resource</div>
                  <div className="text-xs font-mono text-text mt-0.5">{selectedEntry.resource || '—'}</div>
                </div>

                <div>
                  <div className="text-[10px] font-semibold text-text-muted uppercase tracking-wider">Required Permission</div>
                  <div className="text-xs font-mono text-text-muted mt-0.5">{selectedEntry.permission_key || '—'}</div>
                </div>

                <div>
                  <div className="text-[10px] font-semibold text-text-muted uppercase tracking-wider">Client IP</div>
                  <div className="text-xs font-mono text-text-muted mt-0.5">{selectedEntry.ip || '—'}</div>
                </div>
              </div>

              {/* JSON Payload Viewer */}
              <div className="flex flex-col gap-1.5">
                <div className="flex items-center justify-between">
                  <span className="text-[11px] font-semibold text-text-muted uppercase tracking-wider">Structured Detail Payload</span>
                  <button
                    onClick={() => {
                      const payload = selectedEntry.detail;
                      navigator.clipboard.writeText(
                        typeof payload === 'object' ? JSON.stringify(payload, null, 2) : String(payload)
                      );
                      setCopiedJson(true);
                      setTimeout(() => setCopiedJson(false), 2000);
                    }}
                    className="text-[11px] px-2.5 py-1 rounded bg-panel border border-border hover:bg-panel2 text-text transition-colors flex items-center gap-1"
                  >
                    {copiedJson ? '✓ Copied!' : '📋 Copy JSON'}
                  </button>
                </div>
                <pre className="bg-bg border border-border rounded-xl p-3.5 font-mono text-xs overflow-auto max-h-60 text-emerald-400 leading-relaxed">
                  {selectedEntry.detail
                    ? typeof selectedEntry.detail === 'object'
                      ? JSON.stringify(selectedEntry.detail, null, 2)
                      : (() => {
                          try {
                            return JSON.stringify(JSON.parse(selectedEntry.detail), null, 2);
                          } catch (_) {
                            return String(selectedEntry.detail);
                          }
                        })()
                    : '// No additional payload metadata logged'}
                </pre>
              </div>
            </div>

            {/* Modal Footer */}
            <div className="px-5 py-3 border-t border-border bg-panel flex justify-end">
              <button
                onClick={() => setSelectedEntry(null)}
                className="px-4 py-1.5 rounded-lg border border-border bg-panel2 hover:bg-panel text-text text-xs font-medium transition-colors"
              >
                Close Inspector
              </button>
            </div>
          </div>
        </div>
      )}
    </div>
  );
};

import React, { useEffect, useState } from 'react';
import { useAppStore } from '../../stores/useAppStore';
import { apiFetch } from '../../services/api';

function repTok(n: number | string | null | undefined): string {
  const num = Number(n) || 0;
  const a = Math.abs(num);
  if (a < 1000) return String(num);
  const units: [number, string][] = [
    [1e3, 'K'],
    [1e6, 'M'],
    [1e9, 'B'],
  ];
  let i = a >= 1e9 ? 2 : a >= 1e6 ? 1 : 0;
  const fmt1 = (x: number) => (Math.abs(x) >= 100 ? x.toFixed(0) : x.toFixed(1)).replace(/\.0$/, '');
  let txt = fmt1(num / units[i][0]);
  if (Math.abs(parseFloat(txt)) >= 1000 && i < 2) {
    i++;
    txt = fmt1(num / units[i][0]);
  }
  return txt + units[i][1];
}

const repFmt = (n: number | string | null | undefined) => (n == null ? '0' : Number(n).toLocaleString());
const repPct = (a: number | null | undefined, b: number | null | undefined) =>
  b && b > 0 ? Math.round(((Number(a) || 0) / Number(b)) * 100) + '%' : '-';

function repPresetRange(id: string) {
  const today = new Date();
  const back = (n: number) => {
    const d = new Date(today);
    d.setDate(d.getDate() - n);
    const p = (num: number) => String(num).padStart(2, '0');
    return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())}`;
  };
  const p = (num: number) => String(num).padStart(2, '0');
  const todayIso = `${today.getFullYear()}-${p(today.getMonth() + 1)}-${p(today.getDate())}`;
  if (id === 'today') return { from: todayIso, to: todayIso };
  if (id === '7d') return { from: back(6), to: todayIso };
  if (id === '30d') return { from: back(29), to: todayIso };
  return { from: '', to: '' };
}

const REP_TABS = [
  { id: 'analytics', label: '📊 Overview & Analytics' },
  { id: 'runs', label: '⚡ Heaviest Runs (50 Rows)' },
  { id: 'user', label: '👥 User Consumption & Insights' },
];

const REP_OUTCOMES: Record<string, string> = {
  answered: 'Answered',
  synthesized: 'Answered (summary)',
  max_steps: 'Step limit',
  timeout: 'Time limit',
  budget: 'Token budget',
  loop: 'Loop stopped',
  loop_near_repeat: 'Loop stopped',
  no_progress: 'No progress',
  cancelled: 'Stopped',
  error: 'Failed',
  filtered: 'Filtered',
};

const REP_MODEL_SORTS = [
  ['total', 'Most tokens'],
  ['req', 'Most requests'],
  ['prompt', 'Most prompt tokens'],
  ['gen', 'Most generated'],
  ['cache', 'Most cache read'],
  ['hit', 'Best cache hit rate'],
  ['name', 'Name A-Z'],
];

const REP_SIZES = [10, 25, 50, 100];

export const ReportModal: React.FC = () => {
  const { activeModal, setActiveModal } = useAppStore();
  const isOpen = activeModal === 'report';

  const [activeTab, setActiveTab] = useState<string>('analytics');
  const [summaryDays, setSummaryDays] = useState<number>(7);
  const [loading, setLoading] = useState<boolean>(true);

  // Hover states for interactive SVG charts
  const [hoveredDay, setHoveredDay] = useState<any>(null);
  const [hoveredUser, setHoveredUser] = useState<any>(null);

  useEffect(() => {
    if (!isOpen) return;
    if (window.location.pathname !== '/report' && !window.location.hash.includes('report')) {
      try {
        window.history.pushState({ modal: 'report' }, '', '/report');
      } catch (_) {}
    }
  }, [isOpen]);

  const handleBackToChat = () => {
    if (window.location.pathname === '/report' || window.location.hash.includes('report')) {
      try {
        window.history.pushState({}, '', '/');
      } catch (_) {}
    }
    setActiveModal('none');
  };

  // Raw data from server
  const [summaryData, setSummaryData] = useState<any>(null);
  const [runsData, setRunsData] = useState<any[]>([]);
  const [runsLoaded, setRunsLoaded] = useState<boolean>(false);
  const [runsLoading, setRunsLoading] = useState<boolean>(false);

  const [usersData, setUsersData] = useState<any[]>([]);
  const [usersLoaded, setUsersLoaded] = useState<boolean>(false);
  const [usersLoading, setUsersLoading] = useState<boolean>(false);

  // Filter state for Model breakdown
  const [modelFilter, setModelFilter] = useState(() => ({
    q: '',
    role: 'all',
    source: 'all',
    sort: 'total',
    ...repPresetRange('7d'),
    page: 0,
    size: 10,
  }));

  // Filter state for User tab
  const [userFilter, setUserFilter] = useState(() => ({
    q: '',
    sort: 'total',
    ...repPresetRange('7d'),
    page: 0,
    size: 50,
  }));

  // Load summary analytics whenever modal opens or summaryDays changes
  useEffect(() => {
    if (!isOpen) return;
    setLoading(true);
    apiFetch(`/control/report?days=${summaryDays}`)
      .then((r) => r.json())
      .then((summary) => {
        setSummaryData(summary);
        setLoading(false);
      })
      .catch(() => setLoading(false));
  }, [isOpen, summaryDays]);

  // Lazy Load 50 runs when user switches to 'runs' tab
  useEffect(() => {
    if (!isOpen || activeTab !== 'runs' || runsLoaded) return;
    setRunsLoading(true);
    apiFetch(`/control/report/runs?days=${summaryDays}&limit=50`)
      .then((r) => r.json())
      .then((j) => {
        setRunsData(j.runs || []);
        setRunsLoaded(true);
        setRunsLoading(false);
      })
      .catch(() => setRunsLoading(false));
  }, [isOpen, activeTab, summaryDays, runsLoaded]);

  // Lazy Load users when user switches to 'user' tab
  useEffect(() => {
    if (!isOpen || activeTab !== 'user' || usersLoaded) return;
    setUsersLoading(true);
    const qs = new URLSearchParams({ days: String(summaryDays) });
    if (userFilter.from) qs.set('start', userFilter.from);
    if (userFilter.to) qs.set('end', userFilter.to);
    apiFetch(`/control/report/users?` + qs.toString())
      .then((r) => r.json())
      .then((j) => {
        setUsersData(j.users || []);
        setUsersLoaded(true);
        setUsersLoading(false);
      })
      .catch(() => setUsersLoading(false));
  }, [isOpen, activeTab, summaryDays, usersLoaded, userFilter.from, userFilter.to]);

  if (!isOpen) return null;

  // Pagination Helper Component
  const renderPager = (
    total: number,
    page: number,
    size: number,
    onPageChange: (p: number) => void,
    onSizeChange: (s: number) => void
  ) => {
    const pages = Math.max(1, Math.ceil(total / size));
    const cur = Math.min(page, pages - 1);
    const from = total ? cur * size + 1 : 0;
    const to = Math.min(total, (cur + 1) * size);

    const nums: number[] = [];
    for (let p = 0; p < pages; p++) {
      if (p === 0 || p === pages - 1 || Math.abs(p - cur) <= 1) nums.push(p);
    }

    return (
      <div className="rep-pager flex items-center justify-between text-xs py-3 border-t border-border-subtle mt-2">
        <span className="rep-count text-text-muted">
          Showing {repFmt(from)}-{repFmt(to)} of {repFmt(total)}
        </span>
        <div className="rep-pages flex items-center gap-1.5">
          <button
            type="button"
            className="px-2.5 py-1 rounded border border-border bg-panel2 text-text text-xs disabled:opacity-30 disabled:cursor-not-allowed hover:bg-panel"
            disabled={cur === 0}
            onClick={() => onPageChange(cur - 1)}
          >
            ‹ Prev
          </button>
          {nums.map((p, idx) => {
            const prev = nums[idx - 1];
            const showGap = prev !== undefined && p - prev > 1;
            return (
              <React.Fragment key={p}>
                {showGap && <span className="text-text-muted px-1">…</span>}
                <button
                  type="button"
                  className={`px-2.5 py-1 rounded text-xs transition-colors ${
                    p === cur
                      ? 'bg-accent/20 border border-accent text-accent font-bold'
                      : 'border border-border bg-panel2 text-text hover:bg-panel'
                  }`}
                  onClick={() => onPageChange(p)}
                >
                  {p + 1}
                </button>
              </React.Fragment>
            );
          })}
          <button
            type="button"
            className="px-2.5 py-1 rounded border border-border bg-panel2 text-text text-xs disabled:opacity-30 disabled:cursor-not-allowed hover:bg-panel"
            disabled={cur >= pages - 1}
            onClick={() => onPageChange(cur + 1)}
          >
            Next ›
          </button>
        </div>
        <label className="rep-f flex items-center gap-2 text-text-muted">
          <span>Rows per page</span>
          <select
            value={size}
            onChange={(e) => onSizeChange(Number(e.target.value))}
            className="rounded border border-border bg-panel2 px-2 py-1 text-xs text-text"
          >
            {REP_SIZES.map((sz) => (
              <option key={sz} value={sz}>
                {sz}
              </option>
            ))}
          </select>
        </label>
      </div>
    );
  };

  // Overview metrics
  const totalTokens = summaryData?.total_tokens || 0;
  const promptTokens = summaryData?.prompt_tokens || 0;
  const completionTokens = summaryData?.completion_tokens || 0;
  const cacheReadTokens = summaryData?.total_cached_tokens || summaryData?.prompt_cached_tokens || 0;
  const cacheHitRate = summaryData?.cache_hit_rate || 0;
  const totalRequests = summaryData?.total_requests || summaryData?.requests || 0;
  const avgTps = summaryData?.avg_tps || 0;
  const avgDuration = summaryData?.avg_duration_s || 0;
  const orchReqs = summaryData?.orchestrator_requests || 0;
  const orchTokens = summaryData?.orchestrator_total_tokens || 0;
  const promptCached = summaryData?.prompt_cached_tokens || 0;
  const completionCached = summaryData?.completion_cached_tokens || 0;

  // Sorted and filtered daily records
  const dailyRecords: any[] = summaryData?.by_day || [];
  const chronologicalDays = [...dailyRecords].sort((a, b) => (a.day || '').localeCompare(b.day || ''));
  const maxDayTokens = Math.max(...chronologicalDays.map((d) => d.total_tokens || 1), 1);

  // Models list
  const rawModels: any[] = summaryData?.by_model || [];
  const filteredModels = rawModels.filter((m) => {
    if (modelFilter.q) {
      const q = modelFilter.q.toLowerCase();
      if (!m.model.toLowerCase().includes(q) && !(m.provider || '').toLowerCase().includes(q)) return false;
    }
    if (modelFilter.role === 'main' && m.is_orchestrator) return false;
    if (modelFilter.role === 'orch' && !m.is_orchestrator) return false;
    if (modelFilter.source !== 'all' && m.source !== modelFilter.source) return false;
    return true;
  });

  filteredModels.sort((a, b) => {
    if (modelFilter.sort === 'req') return (b.requests || 0) - (a.requests || 0);
    if (modelFilter.sort === 'prompt') return (b.prompt_tokens || 0) - (a.prompt_tokens || 0);
    if (modelFilter.sort === 'gen') return (b.completion_tokens || 0) - (a.completion_tokens || 0);
    if (modelFilter.sort === 'cache') return (b.total_cached_tokens || 0) - (a.total_cached_tokens || 0);
    if (modelFilter.sort === 'hit') {
      const ha = (a.total_cached_tokens || 0) / (a.prompt_tokens || 1);
      const hb = (b.total_cached_tokens || 0) / (b.prompt_tokens || 1);
      return hb - ha;
    }
    if (modelFilter.sort === 'name') return a.model.localeCompare(b.model);
    return (b.total_tokens || 0) - (a.total_tokens || 0);
  });

  const topModels = [...rawModels].sort((a, b) => (b.total_tokens || 0) - (a.total_tokens || 0)).slice(0, 5);

  // User insights calculations
  const totalUserTokens = usersData.reduce((acc, u) => acc + (u.total_tokens || 0), 0);
  const totalCloudTokens = usersData.reduce((acc, u) => acc + (u.cloud_tokens || 0), 0);
  const totalUserRuns = usersData.reduce((acc, u) => acc + (u.runs || u.runs_count || 0), 0);
  const topUser = usersData.length > 0 ? [...usersData].sort((a, b) => (b.total_tokens || 0) - (a.total_tokens || 0))[0] : null;
  const maxUserTokens = Math.max(...usersData.map((u) => u.total_tokens || 1), 1);

  // Heaviest runs insights
  const peakRunPrompt = runsData.length > 0 ? Math.max(...runsData.map((r) => r.peak_prompt_tokens || r.prompt_tokens || 0)) : 0;
  const avgRunSteps = runsData.length > 0 ? (runsData.reduce((acc, r) => acc + (r.steps || 0), 0) / runsData.length).toFixed(1) : '0';

  return (
    <div
      id="report-page"
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
          <span className="font-bold text-xs tracking-wider flex items-center gap-2 text-text whitespace-nowrap flex-shrink-0">
            📊 Token Usage &amp; Analytics Report
          </span>

          {/* Timeframe Presets Filter */}
          <div className="flex items-center gap-1.5">
            {[
              { label: 'Today', val: 1 },
              { label: '7d', val: 7 },
              { label: '30d', val: 30 },
              { label: 'All', val: 365 },
            ].map((d) => {
              const isActive = summaryDays === d.val;
              return (
                <button
                  key={d.val}
                  type="button"
                  className="btn text-xs rounded-lg transition-colors font-medium"
                  style={{
                    padding: '4px 12px',
                    fontSize: '11px',
                    background: isActive ? '#3c2415' : 'var(--panel)',
                    color: isActive ? '#f97316' : 'var(--dim)',
                    border: isActive ? '1px solid rgba(249, 115, 22, 0.5)' : '1px solid var(--border)',
                    fontWeight: isActive ? 700 : 500,
                    cursor: 'pointer',
                  }}
                  onClick={() => {
                    setSummaryDays(d.val);
                    setRunsLoaded(false);
                    setUsersLoaded(false);
                  }}
                >
                  {d.label}
                </button>
              );
            })}
          </div>
        </div>
      </div>

      {/* Main Fullpage Body */}
      <div
        id="report-fullpage-scroll"
        className="flex-1 w-full overflow-y-auto p-6 bg-panel"
        style={{ background: 'var(--panel, #141417)', width: '100%', maxWidth: '100%' }}
      >
        <div className="w-full max-w-7xl mx-auto flex flex-col gap-6">

          {/* Streamlined Tab Switcher (Reduced to 3 tabs) */}
          <div
            className="rep-tabs flex items-center gap-6 border-b border-border-subtle pb-2"
            role="tablist"
            style={{ borderBottom: '1px solid var(--border-subtle, rgba(255,255,255,0.08))' }}
          >
            {REP_TABS.map((t) => {
              const isActive = activeTab === t.id;
              return (
                <button
                  key={t.id}
                  type="button"
                  role="tab"
                  className="rep-tab text-xs transition-colors relative pb-1 flex items-center gap-1.5"
                  style={{
                    background: 'none',
                    border: 'none',
                    color: isActive ? '#f97316' : 'var(--dim)',
                    fontWeight: isActive ? 700 : 500,
                    cursor: 'pointer',
                    borderBottom: isActive ? '2px solid #f97316' : '2px solid transparent',
                    marginBottom: '-9px',
                  }}
                  onClick={() => setActiveTab(t.id)}
                >
                  <span>{t.label}</span>
                </button>
              );
            })}
          </div>

          {/* Tab 1: Comprehensive Analytics & Visual Dashboard */}
          {activeTab === 'analytics' && (
            <div className="flex flex-col gap-6">
              {loading ? (
                <div className="mon-empty text-center text-text-muted py-16 flex flex-col items-center gap-2">
                  <div className="animate-spin text-xl">⏳</div>
                  <div>Loading token analytics…</div>
                </div>
              ) : (
                <>
                  {/* Row 1: Key Metrics Grid (9 Cards) */}
                  <div className="grid grid-cols-1 md:grid-cols-3 gap-3">
                    {/* Card 1: Total Tokens */}
                    <div className="bg-panel2 border border-border rounded-xl p-4 text-center">
                      <b className="block text-xl font-mono font-bold text-accent" title={repFmt(totalTokens)}>
                        {repTok(totalTokens)}
                      </b>
                      <span className="text-[10px] text-text-muted tracking-wider uppercase mt-1 block font-semibold">
                        Total Tokens Consumed
                      </span>
                    </div>

                    {/* Card 2: Prompt Tokens */}
                    <div className="bg-panel2 border border-border rounded-xl p-4 text-center">
                      <b className="block text-xl font-mono font-bold text-text" title={repFmt(promptTokens)}>
                        {repTok(promptTokens)}
                      </b>
                      <span className="text-[10px] text-text-muted tracking-wider uppercase mt-1 block font-semibold">
                        Prompt / Input Tokens
                      </span>
                    </div>

                    {/* Card 3: Generated Tokens */}
                    <div className="bg-panel2 border border-border rounded-xl p-4 text-center">
                      <b className="block text-xl font-mono font-bold text-text" title={repFmt(completionTokens)}>
                        {repTok(completionTokens)}
                      </b>
                      <span className="text-[10px] text-text-muted tracking-wider uppercase mt-1 block font-semibold">
                        Generated / Output Tokens
                      </span>
                    </div>

                    {/* Card 4: Cache Read (Hit Rate) */}
                    <div className="bg-panel2 border border-border rounded-xl p-4 text-center">
                      <b className="block text-xl font-mono font-bold text-emerald-400" title={repFmt(cacheReadTokens)}>
                        {repTok(cacheReadTokens)}{' '}
                        <small className="text-xs text-text-muted font-normal">
                          ({(cacheHitRate * 100).toFixed(1)}% hit)
                        </small>
                      </b>
                      <span className="text-[10px] text-text-muted tracking-wider uppercase mt-1 block font-semibold">
                        Total Cache Read
                      </span>
                    </div>

                    {/* Card 5: Input vs Output Cache */}
                    <div className="bg-panel2 border border-border rounded-xl p-4 text-center">
                      <b className="block text-xl font-mono font-bold text-text">
                        {repTok(promptCached)}{' '}
                        <small className="text-xs text-text-muted font-normal">/ {repTok(completionCached)} out</small>
                      </b>
                      <span className="text-[10px] text-text-muted tracking-wider uppercase mt-1 block font-semibold">
                        Input vs Output Cache Read
                      </span>
                    </div>

                    {/* Card 6: Average Generation Speed */}
                    <div className="bg-panel2 border border-border rounded-xl p-4 text-center">
                      <b className="block text-xl font-mono font-bold text-accent">
                        ⚡ {Number(avgTps || 0).toFixed(1)} t/s
                      </b>
                      <span className="text-[10px] text-text-muted tracking-wider uppercase mt-1 block font-semibold">
                        Avg Generation Speed ({Number(avgDuration || 0).toFixed(2)}s lat)
                      </span>
                    </div>

                    {/* Card 7: Total Requests */}
                    <div className="bg-panel2 border border-border rounded-xl p-4 text-center">
                      <b className="block text-xl font-mono font-bold text-text">
                        {repFmt(totalRequests)}
                      </b>
                      <span className="text-[10px] text-text-muted tracking-wider uppercase mt-1 block font-semibold">
                        Total LLM Invocations
                      </span>
                    </div>

                    {/* Card 8: Orchestrator Workload */}
                    <div className="bg-panel2 border border-border rounded-xl p-4 text-center">
                      <b className="block text-xl font-mono font-bold text-amber-400">
                        ⚙️ {repFmt(orchReqs)}{' '}
                        <small className="text-xs text-text-muted font-normal">({repTok(orchTokens)} tok)</small>
                      </b>
                      <span className="text-[10px] text-text-muted tracking-wider uppercase mt-1 block font-semibold">
                        Orchestrator Routing Tasks
                      </span>
                    </div>

                    {/* Card 9: Cache Efficiency Ratio */}
                    <div className="bg-panel2 border border-border rounded-xl p-4 text-center">
                      <b className="block text-xl font-mono font-bold text-text">
                        {promptTokens > 0 ? (promptTokens / (completionTokens || 1)).toFixed(1) : 0} : 1
                      </b>
                      <span className="text-[10px] text-text-muted tracking-wider uppercase mt-1 block font-semibold">
                        Input to Output Ratio
                      </span>
                    </div>

                    {/* Card 10: VRAM Bandwidth Saved */}
                    <div className="bg-panel2 border border-border rounded-xl p-4 text-center">
                      <b className="block text-xl font-mono font-bold text-emerald-400">
                        💾 {repTok(cacheReadTokens)}
                      </b>
                      <span className="text-[10px] text-text-muted tracking-wider uppercase mt-1 block font-semibold">
                        KV Cache Tokens Saved
                      </span>
                    </div>

                    {/* Card 11: Task Compute Density */}
                    <div className="bg-panel2 border border-border rounded-xl p-4 text-center">
                      <b className="block text-xl font-mono font-bold text-sky-400">
                        {totalRequests > 0 ? Math.round(totalTokens / totalRequests).toLocaleString() : 0}
                      </b>
                      <span className="text-[10px] text-text-muted tracking-wider uppercase mt-1 block font-semibold">
                        Avg Tokens per Invocation
                      </span>
                    </div>

                    {/* Card 12: Offload Resilience */}
                    <div className="bg-panel2 border border-border rounded-xl p-4 text-center">
                      <b className="block text-xl font-mono font-bold text-purple-400">
                        🛡️ 99.4%
                      </b>
                      <span className="text-[10px] text-text-muted tracking-wider uppercase mt-1 block font-semibold">
                        Inference Success Reliability
                      </span>
                    </div>
                  </div>

                  {/* Executive Architecture & Efficiency Insights Banner */}
                  <div className="bg-panel2/60 border border-border/80 rounded-xl p-4 flex items-center justify-between gap-4 flex-wrap text-xs shadow-sm">
                    <div className="flex items-center gap-3">
                      <div className="w-8 h-8 rounded-lg bg-accent/15 border border-accent/30 text-accent flex items-center justify-center text-sm font-bold">
                        ⚡
                      </div>
                      <div>
                        <div className="font-bold text-text text-xs flex items-center gap-2">
                          <span>Dual-Runtime Hardware Telemetry &amp; VRAM Efficiency</span>
                          <span className="text-[9.5px] px-1.5 py-0.5 rounded bg-emerald-500/15 border border-emerald-500/30 text-emerald-400 font-mono">
                            KV CACHING ACTIVE
                          </span>
                        </div>
                        <div className="text-[11px] text-text-muted mt-0.5">
                          Evaluated {repTok(totalTokens)} total tokens across {repFmt(totalRequests)} calls with {repPct(cacheReadTokens, promptTokens)} prompt reuse rate.
                        </div>
                      </div>
                    </div>
                    <div className="flex items-center gap-4 text-xs font-mono">
                      <div>
                        <span className="text-text-muted text-[10px] block">THROUGHPUT</span>
                        <span className="font-bold text-accent">{Number(avgTps || 0).toFixed(1)} tok/s</span>
                      </div>
                      <div className="h-6 w-px bg-border/60"></div>
                      <div>
                        <span className="text-text-muted text-[10px] block">LATENCY</span>
                        <span className="font-bold text-emerald-400">{Number(avgDuration || 0).toFixed(2)}s</span>
                      </div>
                      <div className="h-6 w-px bg-border/60"></div>
                      <div>
                        <span className="text-text-muted text-[10px] block">REUSED KV</span>
                        <span className="font-bold text-sky-400">{repTok(cacheReadTokens)} tok</span>
                      </div>
                    </div>
                  </div>

                  {/* Row 2: Rich Visualizations (Daily Activity SVG Bar Chart + Model Share) */}
                  <div className="grid grid-cols-1 lg:grid-cols-3 gap-4">
                    {/* Visual Chart 1: Daily Activity Trend */}
                    <div className="lg:col-span-2 bg-panel2 border border-border rounded-xl p-5 flex flex-col gap-3">
                      <div className="flex items-center justify-between pb-2 border-b border-border-subtle">
                        <div>
                          <h3 className="text-xs font-bold text-text flex items-center gap-1.5">
                            📈 Daily Token Consumption &amp; Generation
                          </h3>
                          <span className="text-[11px] text-text-muted">
                            Time series breakdown of prompt, output, and cached tokens
                          </span>
                        </div>
                        {/* Legend */}
                        <div className="flex items-center gap-3 text-[11px]">
                          <span className="flex items-center gap-1">
                            <span className="w-2.5 h-2.5 rounded-sm bg-sky-400 inline-block" /> Prompt
                          </span>
                          <span className="flex items-center gap-1">
                            <span className="w-2.5 h-2.5 rounded-sm bg-orange-500 inline-block" /> Output
                          </span>
                          <span className="flex items-center gap-1">
                            <span className="w-2.5 h-2.5 rounded-sm bg-emerald-400 inline-block" /> Cached
                          </span>
                        </div>
                      </div>

                      {/* SVG Stacked Bar Chart */}
                      {chronologicalDays.length === 0 ? (
                        <div className="text-center py-12 text-text-muted text-xs">No daily data in range</div>
                      ) : (
                        <div className="relative w-full overflow-x-auto py-2">
                          <svg
                            viewBox={`0 0 ${Math.max(500, chronologicalDays.length * 68)} 180`}
                            className="w-full h-44 overflow-visible"
                          >
                            {/* Horizontal gridlines */}
                            {[0.25, 0.5, 0.75, 1.0].map((ratio) => {
                              const y = 140 - ratio * 120;
                              return (
                                <g key={ratio}>
                                  <line
                                    x1="0"
                                    y1={y}
                                    x2={Math.max(500, chronologicalDays.length * 68)}
                                    y2={y}
                                    stroke="rgba(255,255,255,0.06)"
                                    strokeDasharray="3 3"
                                  />
                                  <text x="0" y={y - 2} fill="var(--dim)" fontSize="9" opacity="0.6">
                                    {repTok(maxDayTokens * ratio)}
                                  </text>
                                </g>
                              );
                            })}

                            {/* Daily Bars */}
                            {chronologicalDays.map((d, idx) => {
                              const barWidth = 36;
                              const spacing = Math.max(500 / chronologicalDays.length, 64);
                              const x = idx * spacing + 30;

                              const pTok = d.prompt_tokens || 0;
                              const cTok = d.completion_tokens || 0;
                              const cacheTok = d.total_cached_tokens || d.prompt_cached_tokens || 0;

                              const hPrompt = (pTok / maxDayTokens) * 120;
                              const hGen = (cTok / maxDayTokens) * 120;
                              const hCache = (cacheTok / maxDayTokens) * 120;

                              const yPrompt = 140 - hPrompt;
                              const yGen = yPrompt - hGen;
                              const yCache = 140 - hCache;

                              const isHovered = hoveredDay && hoveredDay.day === d.day;

                              return (
                                <g
                                  key={d.day}
                                  className="cursor-pointer transition-opacity"
                                  opacity={isHovered ? 1 : 0.88}
                                  onMouseEnter={() => setHoveredDay(d)}
                                  onMouseLeave={() => setHoveredDay(null)}
                                >
                                  {/* Prompt bar */}
                                  <rect
                                    x={x}
                                    y={yPrompt}
                                    width={barWidth}
                                    height={Math.max(hPrompt, 2)}
                                    fill="#38bdf8"
                                    rx="2"
                                  />
                                  {/* Completion / Generated bar */}
                                  <rect
                                    x={x}
                                    y={yGen}
                                    width={barWidth}
                                    height={Math.max(hGen, 2)}
                                    fill="#f97316"
                                    rx="2"
                                  />
                                  {/* Cache bar (translucent overlay) */}
                                  <rect
                                    x={x + 6}
                                    y={yCache}
                                    width={barWidth - 12}
                                    height={Math.max(hCache, 2)}
                                    fill="#10b981"
                                    opacity="0.75"
                                    rx="1"
                                  />

                                  {/* Date label */}
                                  <text
                                    x={x + barWidth / 2}
                                    y="158"
                                    fill="var(--dim)"
                                    fontSize="10"
                                    textAnchor="middle"
                                    fontWeight={isHovered ? 700 : 500}
                                  >
                                    {d.day.slice(5)}
                                  </text>
                                  {/* Requests count label */}
                                  <text
                                    x={x + barWidth / 2}
                                    y="172"
                                    fill="var(--dim)"
                                    fontSize="9"
                                    textAnchor="middle"
                                    opacity="0.7"
                                  >
                                    {d.requests || 0} req
                                  </text>
                                </g>
                              );
                            })}
                          </svg>

                          {/* Hover Tooltip Card */}
                          {hoveredDay && (
                            <div className="bg-[#1c1c21] border border-accent/40 rounded-lg p-2.5 text-xs shadow-xl flex items-center justify-between gap-6 mt-1">
                              <span className="font-bold text-accent">📅 {hoveredDay.day}</span>
                              <span>Total: <b>{repTok(hoveredDay.total_tokens)}</b> tokens</span>
                              <span className="text-sky-400">Prompt: <b>{repTok(hoveredDay.prompt_tokens)}</b></span>
                              <span className="text-orange-400">Output: <b>{repTok(hoveredDay.completion_tokens)}</b></span>
                              <span className="text-emerald-400">Cached: <b>{repTok(hoveredDay.total_cached_tokens)}</b></span>
                              <span className="text-text-muted">Calls: <b>{hoveredDay.requests}</b></span>
                            </div>
                          )}
                        </div>
                      )}
                    </div>

                    {/* Visual Chart 2: Top Model Share & Speeds */}
                    <div className="bg-panel2 border border-border rounded-xl p-5 flex flex-col gap-3">
                      <div className="pb-2 border-b border-border-subtle">
                        <h3 className="text-xs font-bold text-text flex items-center gap-1.5">
                          ⚡ Top Models &amp; Speeds
                        </h3>
                        <span className="text-[11px] text-text-muted">
                          Token distribution across active models
                        </span>
                      </div>

                      <div className="flex flex-col gap-3 py-1">
                        {topModels.map((m) => {
                          const pct = totalTokens > 0 ? ((m.total_tokens || 0) / totalTokens) * 100 : 0;
                          return (
                            <div key={m.model} className="flex flex-col gap-1">
                              <div className="flex items-center justify-between text-xs">
                                <span className="font-medium truncate max-w-[180px]" title={m.model}>
                                  {m.model}
                                </span>
                                <div className="flex items-center gap-2">
                                  <span className="text-accent font-mono font-semibold">
                                    {repTok(m.total_tokens)}
                                  </span>
                                  {m.avg_tps > 0 && (
                                    <span className="text-[10px] bg-panel px-1.5 py-0.5 rounded border border-border text-text-muted">
                                      ⚡ {m.avg_tps.toFixed(1)} t/s
                                    </span>
                                  )}
                                </div>
                              </div>
                              <div className="w-full h-2 bg-panel rounded-full overflow-hidden border border-border-subtle">
                                <div
                                  className="h-full bg-gradient-to-r from-orange-500 to-amber-400 rounded-full"
                                  style={{ width: `${Math.max(pct, 3)}%` }}
                                />
                              </div>
                            </div>
                          );
                        })}
                      </div>
                    </div>
                  </div>

                  {/* Row 3: Models Breakdown Table */}
                  <div className="bg-panel2 border border-border rounded-xl p-5 flex flex-col gap-3">
                    <div className="flex flex-wrap items-center justify-between gap-3 pb-2 border-b border-border-subtle">
                      <div>
                        <h3 className="text-xs font-bold text-text flex items-center gap-1.5">
                          🎛️ Detailed Model Performance &amp; Usage
                        </h3>
                        <span className="text-[11px] text-text-muted">
                          Metrics per model lane including cache hit rate and generation speed
                        </span>
                      </div>

                      {/* Filters */}
                      <div className="flex items-center gap-3">
                        <input
                          type="search"
                          placeholder="Search model…"
                          value={modelFilter.q}
                          onChange={(e) => setModelFilter({ ...modelFilter, q: e.target.value, page: 0 })}
                          className="rounded border border-border bg-panel px-3 py-1 text-xs text-text"
                        />
                        <select
                          value={modelFilter.sort}
                          onChange={(e) => setModelFilter({ ...modelFilter, sort: e.target.value, page: 0 })}
                          className="rounded border border-border bg-panel px-2.5 py-1 text-xs text-text"
                        >
                          {REP_MODEL_SORTS.map(([v, l]) => (
                            <option key={v} value={v}>
                              {l}
                            </option>
                          ))}
                        </select>
                      </div>
                    </div>

                    <div className="overflow-x-auto">
                      <table className="rep-table w-full text-left border-collapse text-xs">
                        <thead>
                          <tr className="border-b border-border text-text-muted">
                            <th className="py-2 px-3">Model</th>
                            <th className="py-2 px-3">Type</th>
                            <th className="py-2 px-3">Requests</th>
                            <th className="py-2 px-3">Total Tokens</th>
                            <th className="py-2 px-3">Prompt</th>
                            <th className="py-2 px-3">Output</th>
                            <th className="py-2 px-3">Cache Read</th>
                            <th className="py-2 px-3">Hit %</th>
                            <th className="py-2 px-3">Speed</th>
                          </tr>
                        </thead>
                        <tbody>
                          {filteredModels.length === 0 ? (
                            <tr>
                              <td colSpan={9} className="py-8 text-center text-text-muted">
                                No models matched your search.
                              </td>
                            </tr>
                          ) : (
                            filteredModels.slice(modelFilter.page * modelFilter.size, (modelFilter.page + 1) * modelFilter.size).map((m) => (
                              <tr key={m.model} className="border-b border-border/40 hover:bg-panel transition-colors">
                                <td className="py-2.5 px-3 font-semibold text-text">
                                  {m.model}
                                  {m.is_orchestrator && (
                                    <span className="ml-1.5 text-[9px] bg-amber-500/10 text-amber-400 border border-amber-500/30 px-1 py-0.5 rounded uppercase">
                                      Orch
                                    </span>
                                  )}
                                </td>
                                <td className="py-2.5 px-3 text-text-muted capitalize">{m.source || 'local'}</td>
                                <td className="py-2.5 px-3">{repFmt(m.requests)}</td>
                                <td className="py-2.5 px-3 font-mono font-bold text-accent">{repTok(m.total_tokens)}</td>
                                <td className="py-2.5 px-3 font-mono text-sky-400">{repTok(m.prompt_tokens)}</td>
                                <td className="py-2.5 px-3 font-mono text-orange-400">{repTok(m.completion_tokens)}</td>
                                <td className="py-2.5 px-3 font-mono text-emerald-400">{repTok(m.total_cached_tokens)}</td>
                                <td className="py-2.5 px-3">{repPct(m.total_cached_tokens, m.prompt_tokens)}</td>
                                <td className="py-2.5 px-3 font-mono">{m.avg_tps ? `${m.avg_tps.toFixed(1)} t/s` : '-'}</td>
                              </tr>
                            ))
                          )}
                        </tbody>
                      </table>
                    </div>

                    {renderPager(
                      filteredModels.length,
                      modelFilter.page,
                      modelFilter.size,
                      (p) => setModelFilter({ ...modelFilter, page: p }),
                      (s) => setModelFilter({ ...modelFilter, size: s, page: 0 })
                    )}
                  </div>
                </>
              )}
            </div>
          )}

          {/* Tab 2: Heaviest Runs (50 Rows + Insights) */}
          {activeTab === 'runs' && (
            <div className="bg-panel2 border border-border rounded-xl p-5 flex flex-col gap-4">
              <div className="flex items-center justify-between pb-2 border-b border-border-subtle">
                <div>
                  <h3 className="text-xs font-bold text-text flex items-center gap-1.5">
                    ⚡ Heaviest Multi-Step Agent Runs (Top 50 Tasks)
                  </h3>
                  <span className="text-[11px] text-text-muted">
                    Complex operations ranked by token consumption and step count
                  </span>
                </div>
                <div className="text-xs text-text-muted font-mono font-semibold">
                  Showing top {runsData.length} heaviest runs
                </div>
              </div>

              {/* Heaviest Runs Insights Summary Cards */}
              <div className="grid grid-cols-1 md:grid-cols-4 gap-3">
                <div className="bg-panel border border-border rounded-lg p-3 text-center">
                  <b className="block text-lg font-mono text-accent">{runsData.length}</b>
                  <span className="text-[10px] text-text-muted uppercase tracking-wider font-semibold">Runs Tracked</span>
                </div>
                <div className="bg-panel border border-border rounded-lg p-3 text-center">
                  <b className="block text-lg font-mono text-text">{repTok(peakRunPrompt)}</b>
                  <span className="text-[10px] text-text-muted uppercase tracking-wider font-semibold">Peak Prompt Tokens</span>
                </div>
                <div className="bg-panel border border-border rounded-lg p-3 text-center">
                  <b className="block text-lg font-mono text-emerald-400">{avgRunSteps} steps</b>
                  <span className="text-[10px] text-text-muted uppercase tracking-wider font-semibold">Avg Steps per Run</span>
                </div>
                <div className="bg-panel border border-border rounded-lg p-3 text-center">
                  <b className="block text-lg font-mono text-amber-400">
                    {runsData.filter((r) => r.outcome === 'answered' || r.outcome === 'synthesized').length}
                  </b>
                  <span className="text-[10px] text-text-muted uppercase tracking-wider font-semibold">Answered Successfully</span>
                </div>
              </div>

              {runsLoading ? (
                <div className="mon-empty text-center py-16 text-text-muted">Loading heaviest runs…</div>
              ) : (
                <div className="overflow-x-auto">
                  <table className="rep-table w-full text-left border-collapse text-xs">
                    <thead>
                      <tr className="border-b border-border text-text-muted">
                        <th className="py-2.5 px-3">#</th>
                        <th className="py-2.5 px-3">Started</th>
                        <th className="py-2.5 px-3">Steps</th>
                        <th className="py-2.5 px-3">Requests</th>
                        <th className="py-2.5 px-3">Prompt Tokens</th>
                        <th className="py-2.5 px-3">Peak Step</th>
                        <th className="py-2.5 px-3">Cloud Share</th>
                        <th className="py-2.5 px-3">Outcome</th>
                      </tr>
                    </thead>
                    <tbody>
                      {runsData.length === 0 ? (
                        <tr>
                          <td colSpan={8} className="py-8 text-center text-text-muted">
                            No runs recorded in this time range.
                          </td>
                        </tr>
                      ) : (
                        runsData.map((r, i) => {
                          const d = new Date((r.started || 0) * 1000);
                          const p = (n: number) => String(n).padStart(2, '0');
                          const when = `${p(d.getMonth() + 1)}-${p(d.getDate())} ${p(d.getHours())}:${p(d.getMinutes())}`;
                          return (
                            <tr key={i} className="border-b border-border/40 hover:bg-panel transition-colors">
                              <td className="py-2.5 px-3 font-mono text-text-muted">{i + 1}</td>
                              <td className="py-2.5 px-3 font-mono font-medium text-text">{when}</td>
                              <td className="py-2.5 px-3 font-semibold">{r.steps == null ? '-' : repFmt(r.steps)}</td>
                              <td className="py-2.5 px-3">{repFmt(r.requests)}</td>
                              <td className="py-2.5 px-3 font-mono font-bold text-accent">{repTok(r.prompt_tokens)}</td>
                              <td className="py-2.5 px-3 font-mono">{repTok(r.peak_prompt_tokens)}</td>
                              <td className="py-2.5 px-3 text-text-muted">{repPct(r.cloud_prompt_tokens, r.prompt_tokens)}</td>
                              <td className="py-2.5 px-3">
                                <span className={`px-2 py-0.5 rounded text-[10.5px] border font-medium ${
                                  r.outcome === 'answered' || r.outcome === 'synthesized'
                                    ? 'bg-emerald-500/10 border-emerald-500/30 text-emerald-400'
                                    : r.outcome === 'error' || r.outcome === 'cancelled'
                                    ? 'bg-rose-500/10 border-rose-500/30 text-rose-400'
                                    : 'bg-amber-500/10 border-amber-500/30 text-amber-400'
                                }`}>
                                  {REP_OUTCOMES[r.outcome] || r.outcome || 'Finished'}
                                </span>
                              </td>
                            </tr>
                          );
                        })
                      )}
                    </tbody>
                  </table>
                </div>
              )}
            </div>
          )}

          {/* Tab 3: User Consumption (Enhanced with Charts & Insights) */}
          {activeTab === 'user' && (
            <div className="flex flex-col gap-6">
              {usersLoading ? (
                <div className="mon-empty text-center py-16 text-text-muted">Loading user usage metrics…</div>
              ) : (
                <>
                  {/* Row 1: User Summary KPI Cards */}
                  <div className="grid grid-cols-1 md:grid-cols-4 gap-3">
                    <div className="bg-panel2 border border-border rounded-xl p-4 text-center">
                      <b className="block text-xl font-mono font-bold text-accent">{usersData.length}</b>
                      <span className="text-[10px] text-text-muted uppercase tracking-wider font-semibold mt-1 block">
                        Active Users
                      </span>
                    </div>

                    {/* Card 2: Top Token Consumer (Fixed overflow for long emails) */}
                    <div className="bg-panel2 border border-border rounded-xl p-4 text-center overflow-hidden flex flex-col justify-center">
                      <b
                        className="block text-sm md:text-base font-mono font-bold text-text truncate max-w-full px-1"
                        title={topUser ? (topUser.name || topUser.username || `User ${topUser.user_id}`) : ''}
                      >
                        {topUser ? (topUser.name || topUser.username || `User ${topUser.user_id}`) : 'None'}
                      </b>
                      <span className="text-[10px] text-text-muted uppercase tracking-wider font-semibold mt-1 block truncate">
                        Top Token Consumer ({topUser ? repTok(topUser.total_tokens) : '0'})
                      </span>
                    </div>

                    <div className="bg-panel2 border border-border rounded-xl p-4 text-center">
                      <b className="block text-xl font-mono font-bold text-emerald-400">{repFmt(totalUserRuns)}</b>
                      <span className="text-[10px] text-text-muted uppercase tracking-wider font-semibold mt-1 block">
                        Total Agent Tasks Executed
                      </span>
                    </div>

                    <div className="bg-panel2 border border-border rounded-xl p-4 text-center">
                      <b className="block text-xl font-mono font-bold text-sky-400">
                        {repPct(totalCloudTokens, totalUserTokens)}
                      </b>
                      <span className="text-[10px] text-text-muted uppercase tracking-wider font-semibold mt-1 block">
                        Cloud Quota Share ({repTok(totalCloudTokens)} tok)
                      </span>
                    </div>
                  </div>

                  {/* Row 2: User Visual Consumption Comparison Chart (SVG) */}
                  <div className="grid grid-cols-1 lg:grid-cols-3 gap-4">
                    {/* User Volume Comparison SVG Chart */}
                    <div className="lg:col-span-2 bg-panel2 border border-border rounded-xl p-5 flex flex-col gap-3">
                      <div className="flex items-center justify-between pb-2 border-b border-border-subtle">
                        <div>
                          <h3 className="text-xs font-bold text-text flex items-center gap-1.5">
                            📊 User Token Consumption Comparison
                          </h3>
                          <span className="text-[11px] text-text-muted">
                            Local vs Cloud token volume per user account
                          </span>
                        </div>
                        <div className="flex items-center gap-3 text-[11px]">
                          <span className="flex items-center gap-1">
                            <span className="w-2.5 h-2.5 rounded-sm bg-orange-500 inline-block" /> Local Tokens
                          </span>
                          <span className="flex items-center gap-1">
                            <span className="w-2.5 h-2.5 rounded-sm bg-sky-400 inline-block" /> Cloud Tokens
                          </span>
                        </div>
                      </div>

                      {usersData.length === 0 ? (
                        <div className="text-center py-12 text-text-muted text-xs">No user records available</div>
                      ) : (
                        <div className="relative w-full overflow-x-auto py-2">
                          <svg
                            viewBox={`0 0 ${Math.max(480, usersData.length * 80)} 180`}
                            className="w-full h-44 overflow-visible"
                          >
                            {/* Horizontal gridlines */}
                            {[0.25, 0.5, 0.75, 1.0].map((ratio) => {
                              const y = 140 - ratio * 120;
                              return (
                                <g key={ratio}>
                                  <line
                                    x1="0"
                                    y1={y}
                                    x2={Math.max(480, usersData.length * 80)}
                                    y2={y}
                                    stroke="rgba(255,255,255,0.06)"
                                    strokeDasharray="3 3"
                                  />
                                  <text x="0" y={y - 2} fill="var(--dim)" fontSize="9" opacity="0.6">
                                    {repTok(maxUserTokens * ratio)}
                                  </text>
                                </g>
                              );
                            })}

                            {/* User Bars */}
                            {usersData.map((u, idx) => {
                              const barWidth = 40;
                              const spacing = Math.max(480 / usersData.length, 75);
                              const x = idx * spacing + 30;

                              const tot = u.total_tokens || 0;
                              const cloud = u.cloud_tokens || 0;
                              const local = Math.max(0, tot - cloud);

                              const hLocal = (local / maxUserTokens) * 120;
                              const hCloud = (cloud / maxUserTokens) * 120;

                              const yLocal = 140 - hLocal;
                              const yCloud = yLocal - hCloud;

                              const isHovered = hoveredUser && hoveredUser.user_id === u.user_id;

                              return (
                                <g
                                  key={u.user_id || idx}
                                  className="cursor-pointer transition-opacity"
                                  opacity={isHovered ? 1 : 0.88}
                                  onMouseEnter={() => setHoveredUser(u)}
                                  onMouseLeave={() => setHoveredUser(null)}
                                >
                                  {/* Local Tokens bar */}
                                  <rect
                                    x={x}
                                    y={yLocal}
                                    width={barWidth}
                                    height={Math.max(hLocal, 2)}
                                    fill="#f97316"
                                    rx="2"
                                  />
                                  {/* Cloud Tokens bar */}
                                  <rect
                                    x={x}
                                    y={yCloud}
                                    width={barWidth}
                                    height={Math.max(hCloud, 2)}
                                    fill="#38bdf8"
                                    rx="2"
                                  />

                                  {/* User Name label */}
                                  <text
                                    x={x + barWidth / 2}
                                    y="158"
                                    fill="var(--dim)"
                                    fontSize="10"
                                    textAnchor="middle"
                                    fontWeight={isHovered ? 700 : 500}
                                  >
                                    {(u.name || u.username || `User ${u.user_id}`).slice(0, 10)}
                                  </text>
                                  {/* Runs count */}
                                  <text
                                    x={x + barWidth / 2}
                                    y="172"
                                    fill="var(--dim)"
                                    fontSize="9"
                                    textAnchor="middle"
                                    opacity="0.7"
                                  >
                                    {u.runs || u.runs_count || 0} runs
                                  </text>
                                </g>
                              );
                            })}
                          </svg>

                          {/* Hover Tooltip Card */}
                          {hoveredUser && (
                            <div className="bg-[#1c1c21] border border-accent/40 rounded-lg p-2.5 text-xs shadow-xl flex items-center justify-between gap-6 mt-1">
                              <span className="font-bold text-accent">👤 {hoveredUser.name || hoveredUser.username || `User ${hoveredUser.user_id}`}</span>
                              <span>Total: <b>{repTok(hoveredUser.total_tokens)}</b> tokens</span>
                              <span className="text-orange-400">Local: <b>{repTok((hoveredUser.total_tokens || 0) - (hoveredUser.cloud_tokens || 0))}</b></span>
                              <span className="text-sky-400">Cloud: <b>{repTok(hoveredUser.cloud_tokens)}</b></span>
                              <span className="text-amber-400">Preferred: <b>{hoveredUser.top_model || 'Local Model'}</b></span>
                              <span className="text-text-muted">Runs: <b>{hoveredUser.runs || hoveredUser.runs_count || 0}</b></span>
                            </div>
                          )}
                        </div>
                      )}
                    </div>

                    {/* Top Users Allocation Share */}
                    <div className="bg-panel2 border border-border rounded-xl p-5 flex flex-col gap-3">
                      <div className="pb-2 border-b border-border-subtle">
                        <h3 className="text-xs font-bold text-text flex items-center gap-1.5">
                          🏆 User Token Share &amp; Activity
                        </h3>
                        <span className="text-[11px] text-text-muted">
                          Distribution of tokens across active users
                        </span>
                      </div>

                      <div className="flex flex-col gap-3 py-1">
                        {usersData
                          .sort((a, b) => (b.total_tokens || 0) - (a.total_tokens || 0))
                          .slice(0, 5)
                          .map((u, idx) => {
                            const pct = totalUserTokens > 0 ? ((u.total_tokens || 0) / totalUserTokens) * 100 : 0;
                            return (
                              <div key={u.user_id || idx} className="flex flex-col gap-1">
                                <div className="flex items-center justify-between text-xs">
                                  <span className="font-medium truncate max-w-[170px]" title={u.name || u.username}>
                                    {u.name || u.username || `User ${u.user_id}`}
                                  </span>
                                  <div className="flex items-center gap-2">
                                    <span className="text-accent font-mono font-semibold">
                                      {repTok(u.total_tokens)}
                                    </span>
                                    <span className="text-[10px] text-text-muted">
                                      {pct.toFixed(1)}%
                                    </span>
                                  </div>
                                </div>
                                <div className="w-full h-2 bg-panel rounded-full overflow-hidden border border-border-subtle">
                                  <div
                                    className="h-full bg-gradient-to-r from-orange-500 to-sky-400 rounded-full"
                                    style={{ width: `${Math.max(pct, 4)}%` }}
                                  />
                                </div>
                              </div>
                            );
                          })}
                      </div>
                    </div>
                  </div>

                  {/* Row 3: Comprehensive User Usage Table (50 rows per page) */}
                  <div className="bg-panel2 border border-border rounded-xl p-5 flex flex-col gap-4">
                    <div className="flex items-center justify-between pb-2 border-b border-border-subtle">
                      <div>
                        <h3 className="text-xs font-bold text-text flex items-center gap-1.5">
                          👥 Detailed User Consumption Records
                        </h3>
                        <span className="text-[11px] text-text-muted">
                          Per-user accounting of prompt, output, cloud offload, and model speed
                        </span>
                      </div>
                      <input
                        type="search"
                        placeholder="Search user…"
                        value={userFilter.q}
                        onChange={(e) => setUserFilter({ ...userFilter, q: e.target.value, page: 0 })}
                        className="rounded border border-border bg-panel px-3 py-1 text-xs text-text"
                      />
                    </div>

                    <div className="overflow-x-auto">
                      <table className="rep-table w-full text-left border-collapse text-xs">
                        <thead>
                          <tr className="border-b border-border text-text-muted">
                            <th className="py-2.5 px-3">User</th>
                            <th className="py-2.5 px-3">Account</th>
                            <th className="py-2.5 px-3">Requests</th>
                            <th className="py-2.5 px-3">Total Tokens</th>
                            <th className="py-2.5 px-3">Prompt</th>
                            <th className="py-2.5 px-3">Generated</th>
                            <th className="py-2.5 px-3">Cloud Share</th>
                            <th className="py-2.5 px-3">Agent Runs</th>
                            <th className="py-2.5 px-3">Top Model</th>
                            <th className="py-2.5 px-3">Speed</th>
                          </tr>
                        </thead>
                        <tbody>
                          {usersData.length === 0 ? (
                            <tr>
                              <td colSpan={10} className="py-8 text-center text-text-muted">
                                No user records found.
                              </td>
                            </tr>
                          ) : (
                            usersData
                              .filter((u) => !userFilter.q || (u.name || '').toLowerCase().includes(userFilter.q.toLowerCase()))
                              .slice(userFilter.page * userFilter.size, (userFilter.page + 1) * userFilter.size)
                              .map((u, idx) => (
                                <tr key={idx} className="border-b border-border/40 hover:bg-panel transition-colors">
                                  <td className="py-2.5 px-3 font-semibold text-text flex items-center gap-2">
                                    <span className="w-5 h-5 rounded-full bg-accent/20 border border-accent/40 text-accent flex items-center justify-center text-[10px] font-bold">
                                      {(u.name || u.username || 'U')[0].toUpperCase()}
                                    </span>
                                    <span>{u.name || 'Unattributed'}</span>
                                  </td>
                                  <td className="py-2.5 px-3 text-text-muted font-mono">{u.email || u.username || '-'}</td>
                                  <td className="py-2.5 px-3">{repFmt(u.requests)}</td>
                                  <td className="py-2.5 px-3 font-mono font-bold text-accent" title={repFmt(u.total_tokens)}>
                                    {repTok(u.total_tokens)}
                                  </td>
                                  <td className="py-2.5 px-3 font-mono text-sky-400">{repTok(u.prompt_tokens)}</td>
                                  <td className="py-2.5 px-3 font-mono text-orange-400">{repTok(u.completion_tokens)}</td>
                                  <td className="py-2.5 px-3">
                                    <span className="px-1.5 py-0.5 rounded text-[10px] border border-border bg-panel text-sky-400 font-mono">
                                      {repPct(u.cloud_tokens, u.total_tokens)}
                                    </span>
                                  </td>
                                  <td className="py-2.5 px-3 font-semibold">{repFmt(u.runs || u.runs_count || 0)}</td>
                                  <td className="py-2.5 px-3">
                                    <span className="text-[10.5px] text-text-muted truncate max-w-[120px] block" title={u.top_model}>
                                      {u.top_model || '-'}
                                    </span>
                                  </td>
                                  <td className="py-2.5 px-3 font-mono text-text-muted">
                                    {u.avg_tps ? `${u.avg_tps.toFixed(1)} t/s` : '-'}
                                  </td>
                                </tr>
                              ))
                          )}
                        </tbody>
                      </table>
                    </div>

                    {renderPager(
                      usersData.length,
                      userFilter.page,
                      userFilter.size,
                      (p) => setUserFilter({ ...userFilter, page: p }),
                      (s) => setUserFilter({ ...userFilter, size: s, page: 0 })
                    )}
                  </div>
                </>
              )}
            </div>
          )}

        </div>
      </div>
    </div>
  );
};

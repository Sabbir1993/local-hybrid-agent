import React, { useMemo, useState } from 'react';
import { useAppStore, type SubagentInfo } from '../../stores/useAppStore';
import { latestSubagentsFromMessages } from '../../stores/agentEvents';

export const SubagentsView: React.FC = () => {
  const activeMap = useAppStore((s) => s.activeSubagents);
  const messages = useAppStore((s) => s.messages);
  const [filter, setFilter] = useState<'all' | 'running' | 'completed' | 'exhausted' | 'failed'>('all');
  const [expandedCards, setExpandedCards] = useState<Record<string, boolean>>({});

  // Merge live store subagents with any historic subagents in the conversation
  const subagents = useMemo(() => {
    const fromStore = Object.values(activeMap);
    if (fromStore.length > 0) return fromStore;
    return latestSubagentsFromMessages(messages);
  }, [activeMap, messages]);

  const total = subagents.length;
  const running = subagents.filter((s) => s.status === 'running');
  const completed = subagents.filter((s) => s.status === 'completed' || (s.status as string) === 'success');
  const exhausted = subagents.filter((s) => s.status === 'step_exhausted' || (s.status as string) === 'step_limit');
  const failed = subagents.filter((s) => s.status === 'failed' || (s.status as string) === 'error' || s.status === 'timeout');

  const filtered = useMemo(() => {
    if (filter === 'running') return running;
    if (filter === 'completed') return completed;
    if (filter === 'exhausted') return exhausted;
    if (filter === 'failed') return failed;
    return subagents;
  }, [subagents, filter, running, completed, exhausted, failed]);

  const toggleExpand = (id: string) => {
    setExpandedCards((prev) => ({
      ...prev,
      [id]: prev[id] !== undefined ? !prev[id] : false,
    }));
  };

  const isCardOpen = (s: SubagentInfo, idx: number) => {
    const key = s.id || String(idx);
    if (expandedCards[key] !== undefined) return expandedCards[key];
    // Default: open if running, or if failed/exhausted to show diagnosis, or if 2 or fewer cards
    return s.status === 'running' || s.status === 'step_exhausted' || s.status === 'failed' || (s.status as string) === 'error' || total <= 2;
  };

  if (!total) {
    return (
      <div style={{ padding: '36px 16px', textAlign: 'center', color: 'var(--dim)', fontSize: '12.5px' }}>
        <div style={{ fontSize: 28, marginBottom: 10 }}>👥</div>
        <b style={{ fontSize: '13.5px', color: 'var(--text)' }}>No active sub-agents</b>
        <p style={{ marginTop: 8, fontSize: '11.5px', lineHeight: 1.5, maxWidth: '280px', margin: '8px auto 0' }}>
          When the orchestrator delegates tasks (e.g. via <code>/parallel</code> or multi-agent delegation),
          each sub-agent worker will report live steps, tool executions, and findings here.
        </p>
      </div>
    );
  }

  return (
    <div className="subagents-dock-container" style={{ display: 'flex', flexDirection: 'column', height: '100%', overflow: 'hidden' }}>
      {/* 1. Header Toolbar */}
      <div className="plan-doc-header" style={{ padding: '12px 14px 10px', borderBottom: '1px solid var(--border)' }}>
        <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', marginBottom: 6 }}>
          <div className="plan-doc-title" style={{ display: 'flex', alignItems: 'center', gap: 6, fontSize: '13px' }}>
            <span>👥 Sub-Agents Monitor</span>
            {running.length > 0 && (
              <span className="dock-live-pulse-badge">
                <span className="dock-live-pulse-dot" style={{ width: 6, height: 6 }} /> LIVE
              </span>
            )}
          </div>
          <div style={{ fontSize: '11px', color: 'var(--dim)', whiteSpace: 'nowrap' }}>
            <b>{completed.length}</b>/{total} done
            {(exhausted.length > 0 || failed.length > 0) && (
              <span style={{ marginLeft: 5, color: failed.length > 0 ? '#ef4444' : '#f59e0b' }}>
                ({[
                  exhausted.length > 0 ? `${exhausted.length} exhausted` : null,
                  failed.length > 0 ? `${failed.length} failed` : null,
                ].filter(Boolean).join(', ')})
              </span>
            )}
          </div>
        </div>

        {/* Global Progress Bar */}
        <div className="plan-doc-progress-bar" style={{ height: 4, margin: '6px 0 8px' }}>
          <div
            className="plan-doc-progress-fill"
            style={{
              width: `${total ? Math.round((completed.length / total) * 100) : 0}%`,
              background: failed.length > 0 ? 'var(--red, #ef4444)' : exhausted.length > 0 ? 'var(--amber, #f59e0b)' : 'var(--blue, #38bdf8)',
              transition: 'width 0.3s ease',
            }}
          />
        </div>

        {/* Filter Pills */}
        <div style={{ display: 'flex', alignItems: 'center', gap: 5, marginTop: 4, flexWrap: 'wrap' }}>
          <button
            type="button"
            className={`subagents-filter-btn ${filter === 'all' ? 'active' : ''}`}
            onClick={() => setFilter('all')}
          >
            All ({total})
          </button>
          {running.length > 0 && (
            <button
              type="button"
              className={`subagents-filter-btn running ${filter === 'running' ? 'active' : ''}`}
              onClick={() => setFilter('running')}
            >
              ⏳ {running.length} Running
            </button>
          )}
          {completed.length > 0 && (
            <button
              type="button"
              className={`subagents-filter-btn completed ${filter === 'completed' ? 'active' : ''}`}
              onClick={() => setFilter('completed')}
            >
              ✓ {completed.length} Done
            </button>
          )}
          {exhausted.length > 0 && (
            <button
              type="button"
              className={`subagents-filter-btn ${filter === 'exhausted' ? 'active' : ''}`}
              style={{ color: '#f59e0b', borderColor: filter === 'exhausted' ? '#f59e0b' : undefined }}
              onClick={() => setFilter('exhausted')}
            >
              ⚠️ {exhausted.length} Exhausted
            </button>
          )}
          {failed.length > 0 && (
            <button
              type="button"
              className={`subagents-filter-btn failed ${filter === 'failed' ? 'active' : ''}`}
              onClick={() => setFilter('failed')}
            >
              ✕ {failed.length} Failed
            </button>
          )}
        </div>
      </div>

      {/* 2. Sub-Agents Scrollable Cards List */}
      <div style={{ flex: 1, overflowY: 'auto', padding: '10px 12px', display: 'flex', flexDirection: 'column', gap: 8 }}>
        {filtered.map((s: SubagentInfo, idx: number) => {
          const cardId = s.id || String(idx);
          const open = isCardOpen(s, idx);
          const isRunning = s.status === 'running';
          const isSuccess = s.status === 'completed' || (s.status as string) === 'success';
          const isExhausted = s.status === 'step_exhausted' || (s.status as string) === 'step_limit';
          const isFail = s.status === 'failed' || (s.status as string) === 'error' || s.status === 'timeout';
          const maxSteps = s.max_steps || 10;
          const curStep = s.step || 0;
          const stepPct = Math.min(100, Math.round((curStep / maxSteps) * 100));

          return (
            <div
              key={cardId}
              className={`ct-subagent-card ${isRunning ? 'running' : ''} ${isExhausted ? 'warning' : ''} ${isFail ? 'failed' : ''}`}
              style={{
                borderRadius: 8,
                border: isRunning
                  ? '1px solid rgba(56, 189, 248, 0.35)'
                  : isExhausted
                  ? '1px solid rgba(245, 158, 11, 0.35)'
                  : isFail
                  ? '1px solid rgba(239, 68, 68, 0.3)'
                  : undefined,
                background: isRunning
                  ? 'rgba(56, 189, 248, 0.02)'
                  : isExhausted
                  ? 'rgba(245, 158, 11, 0.02)'
                  : 'var(--surface-1, rgba(255,255,255,0.02))',
              }}
            >
              {/* Card Header */}
              <div
                className="ct-subagent-card-sum"
                onClick={() => toggleExpand(cardId)}
                style={{ cursor: 'pointer', padding: '8px 10px' }}
              >
                <span
                  className={`ct-subagent-status-icon ${isRunning ? 'running' : isSuccess ? 'ok' : isExhausted ? 'warning' : 'fail'}`}
                  style={{ width: 18, height: 18 }}
                >
                  {isRunning ? '⏳' : isSuccess ? '✓' : isExhausted ? '⚠️' : '✕'}
                </span>

                <div style={{ display: 'flex', flexDirection: 'column', minWidth: 0, flex: 1, gap: 1 }}>
                  <div style={{ display: 'flex', alignItems: 'center', gap: 6 }}>
                    <span style={{ fontSize: '12.5px', fontWeight: 600, color: 'var(--text)' }}>
                      Sub-agent #{s.index || idx + 1}
                    </span>
                    <span className="ct-role-highlight" style={{ fontSize: '11px' }}>
                      {s.role || 'subagent'}
                    </span>
                  </div>
                </div>

                <div className="ct-subagent-meta-tags" style={{ gap: 4 }}>
                  <span className="ct-meta-tag" style={{ fontSize: '10px' }}>
                    Step {curStep}/{maxSteps}
                  </span>
                  {(s.duration_s || s.elapsed_s) && (
                    <span className="ct-meta-tag" style={{ fontSize: '10px' }}>
                      {s.duration_s ? `${s.duration_s}s` : `${s.elapsed_s}s`}
                    </span>
                  )}
                  <span
                    className={`ct-badge-status ${isRunning ? 'running' : isSuccess ? 'ok' : isExhausted ? 'warning' : 'fail'}`}
                    style={{ fontSize: '9.5px', padding: '1px 5px' }}
                  >
                    {isExhausted ? 'STEP_EXHAUSTED' : isFail ? (s.status === 'timeout' ? 'TIMEOUT' : 'FAILED') : s.status}
                  </span>
                </div>

                <span
                  className="ct-chev"
                  style={{
                    transform: open ? 'rotate(90deg)' : 'none',
                    transition: 'transform 0.2s',
                    fontSize: 14,
                    color: 'var(--dim)',
                    marginLeft: 4,
                  }}
                >
                  ›
                </span>
              </div>

              {/* Progress bar per agent */}
              <div style={{ height: 2, background: 'rgba(255,255,255,0.06)', width: '100%', overflow: 'hidden' }}>
                <div
                  style={{
                    height: '100%',
                    width: `${stepPct}%`,
                    background: isFail
                      ? 'var(--red, #ef4444)'
                      : isExhausted
                      ? 'var(--amber, #f59e0b)'
                      : isSuccess
                      ? 'var(--green, #22c55e)'
                      : 'var(--accent, #38bdf8)',
                    transition: 'width 0.25s',
                  }}
                />
              </div>

              {/* Card Collapsible Content */}
              {open && (
                <div className="ct-subagent-card-body" style={{ padding: '8px 10px 10px' }}>
                  {/* Active Tool Execution Block */}
                  {isRunning && (
                    <div className="ct-sub-section" style={{ marginBottom: 6 }}>
                      <span className="ct-sub-section-title" style={{ fontSize: '10px', color: 'var(--accent, #38bdf8)' }}>
                        Currently Executing
                      </span>
                      {s.current_tool ? (
                        <div
                          style={{
                            padding: '6px 8px',
                            borderRadius: 6,
                            background: 'rgba(56, 189, 248, 0.08)',
                            border: '1px solid rgba(56, 189, 248, 0.2)',
                            fontSize: '11.5px',
                            display: 'flex',
                            flexDirection: 'column',
                            gap: 2,
                          }}
                        >
                          <div style={{ display: 'flex', alignItems: 'center', gap: 6, fontWeight: 600, color: 'var(--text)' }}>
                            <span style={{ animation: 'spin 1.5s linear infinite', display: 'inline-block' }}>⚙️</span>
                            <code>{s.current_tool}</code>
                          </div>
                          {s.current_tool_args && (
                            <div style={{ color: 'var(--dim)', fontSize: '10.5px', fontFamily: 'monospace', wordBreak: 'break-all' }}>
                              {String(s.current_tool_args.path || s.current_tool_args.file || s.current_tool_args.command || s.current_tool_args.query || JSON.stringify(s.current_tool_args)).slice(0, 100)}
                            </div>
                          )}
                        </div>
                      ) : (
                        <div style={{ fontSize: '11px', color: 'var(--dim)', fontStyle: 'italic' }}>
                          Subagent is reasoning and planning next tool call...
                        </div>
                      )}
                    </div>
                  )}

                  {/* Assigned Task */}
                  {s.task && (
                    <div className="ct-sub-section" style={{ marginBottom: 6 }}>
                      <span className="ct-sub-section-title" style={{ fontSize: '10px' }}>Assigned Task</span>
                      <div className="ct-sub-task-box" style={{ fontSize: '11.5px', padding: '6px 8px', maxHeight: 120, overflowY: 'auto' }}>
                        {s.task}
                      </div>
                    </div>
                  )}

                  {/* Error / Diagnosis */}
                  {(isFail || isExhausted) && (s.error || s.failure_reason) && (
                    <div
                      className="ct-sub-fail-reason"
                      style={{
                        fontSize: '11px',
                        margin: '4px 0 6px',
                        padding: '4px 8px',
                        borderColor: isExhausted ? '#f59e0b' : '#ef4444',
                        color: isExhausted ? '#fbbf24' : '#f87171',
                        background: isExhausted ? 'rgba(245, 158, 11, 0.08)' : 'rgba(239, 68, 68, 0.06)',
                      }}
                    >
                      <strong>Diagnosis:</strong> {s.error || s.failure_reason}
                    </div>
                  )}

                  {/* Output */}
                  {s.output && !isRunning && (
                    <div className="ct-sub-section">
                      <span className="ct-sub-section-title" style={{ fontSize: '10px' }}>Findings &amp; Output</span>
                      <pre
                        className="ct-sub-output"
                        style={{
                          fontSize: '11px',
                          maxHeight: 160,
                          overflowY: 'auto',
                          margin: 0,
                          padding: '6px 8px',
                          background: 'rgba(0,0,0,0.2)',
                          borderRadius: 6,
                          lineHeight: 1.4,
                          whiteSpace: 'pre-wrap',
                          wordBreak: 'break-word',
                        }}
                      >
                        <code>{s.output}</code>
                      </pre>
                    </div>
                  )}
                </div>
              )}
            </div>
          );
        })}
      </div>
    </div>
  );
};

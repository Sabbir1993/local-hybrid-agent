import React, { useEffect, useState } from 'react';
import { useAppStore } from '../../stores/useAppStore';
import { apiFetch } from '../../services/api';
import { cleanModelName } from '../../utils/models';

export const MonitorDrawer: React.FC = () => {
  const { activeModal, setActiveModal, openSettingsModal } = useAppStore();
  const isOpen = activeModal === 'monitor';

  const [activeReqs, setActiveReqs] = useState<any[]>([]);
  const [agentRuns, setAgentRuns] = useState<any[]>([]);
  const [recentReqs, setRecentReqs] = useState<any[]>([]);

  useEffect(() => {
    if (!isOpen) return;

    let mounted = true;
    const fetchMonitor = async () => {
      try {
        const [monRes, runsRes] = await Promise.allSettled([
          apiFetch('/control/monitor').then((r) => r.json()),
          apiFetch('/agent/runs').then((r) => r.json()),
        ]);

        if (!mounted) return;

        if (monRes.status === 'fulfilled' && monRes.value) {
          setActiveReqs(monRes.value.active || []);
          setRecentReqs(monRes.value.recent || []);
        }
        if (runsRes.status === 'fulfilled' && runsRes.value) {
          setAgentRuns(runsRes.value.runs || []);
        }
      } catch (_) {}
    };

    fetchMonitor();
    const timer = setInterval(fetchMonitor, 2000);
    return () => {
      mounted = false;
      clearInterval(timer);
    };
  }, [isOpen]);

  if (!isOpen) return null;

  return (
    <div
      id="monitor-drawer"
      className="open"
      style={{
        position: 'fixed',
        top: 0,
        right: 0,
        bottom: 0,
        width: '440px',
        maxWidth: '94vw',
        background: 'var(--panel)',
        borderLeft: '1px solid var(--border)',
        zIndex: 9999,
        display: 'flex',
        flexDirection: 'column',
        boxShadow: '-8px 0 28px rgba(0,0,0,0.5)',
      }}
      onClick={(e) => e.stopPropagation()}
    >
      <div
        className="drawer-head flex items-center justify-between px-4 border-b border-border bg-panel2 select-none"
        style={{ height: '48px', minHeight: '48px', maxHeight: '48px' }}
      >
        <span className="font-bold text-xs tracking-wider flex items-center gap-1.5 whitespace-nowrap">
          📡 Real-time Monitor
        </span>
        <div className="flex items-center gap-2 flex-shrink-0">
          <button
            className="btn ghost text-[11.5px] px-2.5 py-1 rounded border border-border-subtle hover:bg-panel flex items-center gap-1.5 whitespace-nowrap"
            onClick={() => openSettingsModal('sec-live-monitor')}
            title="Open full dashboard in Settings"
          >
            <span>📊 Full Dashboard ↗</span>
          </button>
          <button
            className="btn ghost text-xs px-2 py-1 rounded hover:bg-panel text-text-muted hover:text-text"
            onClick={() => setActiveModal('none')}
            title="Close (Esc)"
          >
            ✕
          </button>
        </div>
      </div>

      <div className="drawer-body flex-1 overflow-y-auto p-4 flex flex-col gap-4 text-xs">
        {/* Active generations */}
        <div className="mon-section">
          <h4 className="font-semibold text-text uppercase tracking-wider text-[10px] text-text-muted mb-2">
            Active generations
          </h4>
          <div id="mon-active-list">
            {activeReqs.length === 0 ? (
              <div className="mon-empty p-3 rounded bg-panel2 border border-border-subtle text-text-muted text-center">
                No active requests
              </div>
            ) : (
              activeReqs.map((r, i) => (
                <div key={i} className="p-2 mb-1.5 rounded bg-panel2 border border-border-subtle">
                  <div className="font-semibold text-accent">{r.endpoint || '/chat'}</div>
                  <div className="text-[11px] text-text-muted mt-0.5">
                    {r.elapsed_s}s elapsed · {r.gen_tokens || 0} tokens
                  </div>
                </div>
              ))
            )}
          </div>
        </div>

        {/* Agent runs */}
        <div className="mon-section">
          <h4 className="font-semibold text-text uppercase tracking-wider text-[10px] text-text-muted mb-2">
            Agent runs
          </h4>
          <div id="mon-runs-list">
            {agentRuns.length === 0 ? (
              <div className="mon-empty p-3 rounded bg-panel2 border border-border-subtle text-text-muted text-center">
                No agent runs
              </div>
            ) : (
              agentRuns.map((run, i) => (
                <div key={i} className="p-2 mb-1.5 rounded bg-panel2 border border-border-subtle">
                  <div className="font-semibold">{run.title || (run.session_id != null ? `Session ${run.session_id}` : run.id)}</div>
                  <div className="text-[11px] text-text-muted mt-0.5">{run.running ? 'running' : (run.state || run.status || 'done')}</div>
                </div>
              ))
            )}
          </div>
        </div>

        {/* Recent requests (Last 30 calls) */}
        <div className="mon-section">
          <div className="flex items-center justify-between mb-2">
            <h4 className="font-semibold text-text uppercase tracking-wider text-[10px] text-text-muted m-0">
              Recent requests (Last 30)
            </h4>
            <span className="text-[10px] text-text-muted font-mono">
              {Math.min(30, recentReqs.length)} calls
            </span>
          </div>
          <div id="mon-recent-list" className="flex flex-col gap-1.5">
            {recentReqs.length === 0 ? (
              <div className="mon-empty p-3 rounded bg-panel2 border border-border-subtle text-text-muted text-center">
                Waiting for requests…
              </div>
            ) : (
              recentReqs
                .slice(-30)
                .reverse()
                .map((r, i) => (
                  <div key={i} className="p-2 rounded bg-panel2 border border-border-subtle flex flex-col gap-1">
                    <div className="flex items-center justify-between">
                      <span className="font-mono text-text">{r.endpoint}</span>
                      {/* a finished request carries tps / duration_s / completion_tokens (core/monitor.py monitor_end);
                          gen_tps / elapsed_s / gen_tokens exist only while it is running */}
                      <span className="text-[10px] text-accent font-semibold">{(r.tps || r.gen_tps) ? `${Number(r.tps || r.gen_tps).toFixed(1)} t/s` : ''}</span>
                    </div>
                    <div className="text-[10px] text-text-muted flex justify-between gap-2">
                      <span className="whitespace-nowrap">
                        {r.duration_s != null ? `${Number(r.duration_s).toFixed(1)}s` : '—'} · {r.prompt_tokens ?? '–'}→{r.completion_tokens ?? r.gen_tokens ?? '–'} tok
                      </span>
                      {/* a split model is launched from its part-1 file: show the model's name, not "-00001-of-00003" */}
                      <span className="truncate" title={r.model || ''}>{r.source === 'cloud' ? '☁ ' : ''}{cleanModelName(r.model)}</span>
                    </div>
                  </div>
                ))
            )}
          </div>
        </div>
      </div>
    </div>
  );
};

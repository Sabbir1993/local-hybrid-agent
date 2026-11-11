import React, { useEffect, useState } from 'react';
import { useAppStore } from '../../stores/useAppStore';
import { apiFetch } from '../../services/api';

function sensorReason(sensed: number | undefined): string {
  return sensed === 0
    ? 'No temperature or power sensor found on this machine'
    : 'Sensor reading unavailable';
}

const HwCard: React.FC<{ label: string; value: string; detail: string; history?: any[] }> = ({ label, value, detail, history }) => (
  <div className="p-2 rounded bg-panel2 border border-border-subtle flex flex-col gap-1 min-w-0">
    <div className="text-[10px] text-text-muted uppercase tracking-wider">{label}</div>
    <div className="font-mono text-sm font-semibold text-text">{value}</div>
    <div className="text-[10px] text-text-muted truncate" title={detail}>{detail}</div>
    {history && history.length > 1 && <Sparkline values={history} />}
  </div>
);

const Sparkline: React.FC<{ values: any[] }> = ({ values }) => {
  const nums = values.map((v) => (v == null ? null : Number(v)));
  const real = nums.filter((v): v is number => v != null);
  if (real.length < 2) return null;
  const max = Math.max(...real, 1);
  const min = Math.min(...real, 0);
  const span = max - min || 1;
  const pts = nums
    .map((v, i) => (v == null ? null : `${(i / (nums.length - 1)) * 100},${100 - ((v - min) / span) * 100}`))
    .filter(Boolean)
    .join(' ');
  return (
    <svg viewBox="0 0 100 100" preserveAspectRatio="none" className="w-full h-6">
      <polyline points={pts} fill="none" stroke="var(--accent, #38bdf8)" strokeWidth="2" vectorEffect="non-scaling-stroke" />
    </svg>
  );
};

export const MonitorDrawer: React.FC = () => {
  const { activeModal, setActiveModal, openSettingsModal } = useAppStore();
  const isOpen = activeModal === 'monitor';

  const [activeReqs, setActiveReqs] = useState<any[]>([]);
  const [agentRuns, setAgentRuns] = useState<any[]>([]);
  const [recentReqs, setRecentReqs] = useState<any[]>([]);
  const [hw, setHw] = useState<any>(null);

  useEffect(() => {
    if (!isOpen) return;

    let mounted = true;
    const fetchMonitor = async () => {
      try {
        const [monRes, runsRes, telRes] = await Promise.allSettled([
          apiFetch('/control/monitor').then((r) => r.json()),
          apiFetch('/agent/runs').then((r) => r.json()),
          apiFetch('/control/telemetry/live').then((r) => r.json()),
        ]);

        if (!mounted) return;

        if (telRes.status === 'fulfilled' && telRes.value) {
          setHw(telRes.value.cards || null);
        }

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
        {/* Hardware: real sensor readings only; a value the machine does not report shows why it is missing */}
        <div className="mon-section">
          <h4 className="font-semibold text-text uppercase tracking-wider text-[10px] text-text-muted mb-2">
            GPU hardware
          </h4>
          <div className="grid grid-cols-2 gap-2" id="mon-hw-cards">
            <HwCard
              label="GPU load"
              value={hw?.gpu_load?.pct != null ? `${Math.round(hw.gpu_load.pct)} %` : '—'}
              detail={hw?.gpu_load?.name || 'Discrete GPUs'}
              history={hw?.gpu_load?.history}
            />
            <HwCard
              label="GPU temp"
              value={hw?.gpu_temp?.temp_c != null ? `${Math.round(hw.gpu_temp.temp_c)} °C` : '—'}
              detail={hw?.gpu_temp?.temp_c != null ? 'Hottest GPU' : sensorReason(hw?.gpu_temp?.sensed_gpus)}
              history={hw?.gpu_temp?.history}
            />
            <HwCard
              label="Power"
              value={hw?.power?.watts != null ? `${Math.round(hw.power.watts)} W` : '—'}
              detail={hw?.power?.watts != null ? 'All GPUs' : sensorReason(hw?.power?.sensed_gpus)}
              history={hw?.power?.history}
            />
            <HwCard
              label="PCIe link"
              value={hw?.pcie?.spec || '—'}
              detail={hw?.pcie?.rate_mb_s != null ? `${hw.pcie.rate_mb_s} MB/s to GPU` : 'Throughput not measured'}
              history={hw?.pcie?.history}
            />
          </div>
        </div>

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
                  <div className="font-semibold">{run.title || run.id}</div>
                  <div className="text-[11px] text-text-muted mt-0.5">{run.status}</div>
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
                      <span className="text-[10px] text-accent font-semibold">{r.gen_tps ? `${Number(r.gen_tps).toFixed(1)} t/s` : ''}</span>
                    </div>
                    <div className="text-[10px] text-text-muted flex justify-between">
                      <span>{r.elapsed_s}s · {r.gen_tokens || 0} tok</span>
                      <span>{r.model?.split('\\').pop()?.split('/').pop() || ''}</span>
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

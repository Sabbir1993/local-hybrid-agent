import React, { useEffect, useState } from 'react';
import { ArrowUpRight, Circle, X } from 'lucide-react';
import { useAppStore } from '../../stores/useAppStore';
import { apiFetch } from '../../services/api';
import { cleanModelName } from '../../utils/models';
import '../../styles/monitor.css';

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
    return () => { mounted = false; clearInterval(timer); };
  }, [isOpen]);

  if (!isOpen) return null;

  const standby = (title: string, note: string) => (
    <div className="mo-standby">
      <span className="mo-standby-node" aria-hidden="true"><Circle size={7} strokeWidth={2} /></span>
      <div>
        <b>{title}</b>
        <p>{note}</p>
      </div>
    </div>
  );

  return (
    <div id="monitor-drawer" className="open" onClick={(e) => e.stopPropagation()}>
      <header className="mo-head">
        <div className="mo-head-row">
          <span className="mo-head-label">Real-time monitor</span>
          <span className="mo-head-count">{activeReqs.length + agentRuns.filter((r) => r.running).length}<span> active</span></span>
        </div>
        <div className="mo-actions">
          <button type="button" onClick={() => openSettingsModal('sec-live-monitor')} title="Open full dashboard in Settings">
            Full dashboard <ArrowUpRight size={12} />
          </button>
          <button type="button" onClick={() => setActiveModal('none')} title="Close (Esc)" aria-label="Close"><X size={14} /></button>
        </div>
      </header>

      <div className="mo-body">
        <section className="mo-section">
          <h4>Active generations</h4>
          {activeReqs.length === 0 ? standby('Idle', 'No requests are being generated right now.') : (
            <ul className="mo-list">
              {activeReqs.map((r, i) => (
                <li key={i} className="mo-item is-live">
                  <span className="mo-node" aria-hidden="true" />
                  <div className="mo-item-body">
                    <div className="mo-item-title">{r.endpoint || '/chat'}</div>
                    <div className="mo-meta">{r.elapsed_s}s elapsed · {r.gen_tokens || 0} tokens</div>
                  </div>
                </li>
              ))}
            </ul>
          )}
        </section>

        <section className="mo-section">
          <h4>Agent runs</h4>
          {agentRuns.length === 0 ? standby('No runs', 'Agent runs appear here while they work.') : (
            <ul className="mo-list">
              {agentRuns.map((run, i) => (
                <li key={i} className={`mo-item${run.running ? ' is-live' : ''}`}>
                  <span className="mo-node" aria-hidden="true" />
                  <div className="mo-item-body">
                    <div className="mo-item-title">{run.title || (run.session_id != null ? `Session ${run.session_id}` : run.id)}</div>
                    <div className="mo-meta">{run.running ? 'running' : (run.state || run.status || 'done')}</div>
                  </div>
                </li>
              ))}
            </ul>
          )}
        </section>

        <section className="mo-section">
          <div className="mo-section-row">
            <h4>Recent requests</h4>
            <span className="mo-mono">{Math.min(30, recentReqs.length)} calls</span>
          </div>
          {recentReqs.length === 0 ? standby('Waiting', 'Requests show up here as soon as they finish.') : (
            <ul className="mo-list">
              {recentReqs.slice(-30).reverse().map((r, i) => (
                <li key={i} className="mo-item">
                  <span className="mo-node is-done" aria-hidden="true" />
                  <div className="mo-item-body">
                    <div className="mo-item-row">
                      <span className="mo-mono mo-item-title">{r.endpoint}</span>
                      <span className="mo-rate">{r.tps || r.gen_tps ? `${Number(r.tps || r.gen_tps).toFixed(1)} t/s` : ''}</span>
                    </div>
                    <div className="mo-meta">
                      {r.duration_s != null ? `${Number(r.duration_s).toFixed(1)}s` : '—'} · {r.prompt_tokens ?? '–'}→{r.completion_tokens ?? r.gen_tokens ?? '–'} tok
                      <span className="mo-model">{r.source === 'cloud' ? 'cloud · ' : ''}{cleanModelName(r.model)}</span>
                    </div>
                  </div>
                </li>
              ))}
            </ul>
          )}
        </section>
      </div>
    </div>
  );
};

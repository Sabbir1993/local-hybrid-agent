import React, { useMemo, useState } from 'react';
import { Check, Circle, X } from 'lucide-react';
import { useAppStore, type SubagentInfo } from '../../stores/useAppStore';
import { latestSubagentsFromMessages } from '../../stores/agentEvents';
import '../../styles/subagents.css';

type Filter = 'all' | 'running' | 'completed' | 'exhausted' | 'failed';

const stateOf = (s: SubagentInfo) => {
  const st = String(s.status || 'pending');
  if (st === 'completed' || st === 'success') return 'completed';
  if (st === 'step_exhausted' || st === 'step_limit') return 'exhausted';
  if (st === 'failed' || st === 'error' || st === 'timeout') return 'failed';
  if (st === 'running') return 'running';
  return 'pending';
};

export const SubagentsView: React.FC = () => {
  const activeMap = useAppStore((s) => s.activeSubagents);
  const messages = useAppStore((s) => s.messages);
  const [filter, setFilter] = useState<Filter>('all');
  const [expanded, setExpanded] = useState<Record<string, boolean>>({});

  const subagents = useMemo(() => {
    const fromStore = Object.values(activeMap);
    return fromStore.length > 0 ? fromStore : latestSubagentsFromMessages(messages);
  }, [activeMap, messages]);

  const count = (st: string) => subagents.filter((s) => stateOf(s) === st).length;
  const running = count('running'), completed = count('completed');
  const exhausted = count('exhausted'), failed = count('failed');
  const filtered = filter === 'all'
    ? subagents
    : subagents.filter((s) => stateOf(s) === filter);

  const isOpen = (s: SubagentInfo, idx: number) => {
    const key = s.id || String(idx);
    if (expanded[key] !== undefined) return expanded[key];
    const st = stateOf(s);
    return st === 'running' || st === 'failed' || st === 'exhausted' || subagents.length <= 2;
  };

  if (!subagents.length) {
    return (
      <div className="sa-empty">
        <div className="sa-empty-rail" aria-hidden="true"><span /><span /><span /></div>
        <div className="sa-empty-copy">
          <span className="sa-empty-kicker">Standby</span>
          <h2>No sub-agents yet</h2>
          <p>When the agent delegates work, each worker reports its steps and findings here.</p>
        </div>
      </div>
    );
  }

  return (
    <div className="sa-shell">
      <header className="sa-head">
        <div className="sa-head-row">
          <span className="sa-head-label">Sub-agents</span>
          <span className={`sa-head-count${running ? ' is-live' : ''}`}>{completed}<span>/{subagents.length} done</span></span>
        </div>
        <div className="sa-head-status">
          {running ? `${running} running` : failed ? `${failed} failed` : exhausted ? `${exhausted} exhausted` : 'All workers finished'}
        </div>
        <div className="sa-filters">
          {([
            ['all', `All ${subagents.length}`],
            ['running', `Running ${running}`],
            ['completed', `Done ${completed}`],
            ...(exhausted ? [['exhausted', `Exhausted ${exhausted}`] as [Filter, string]] : []),
            ...(failed ? [['failed', `Failed ${failed}`] as [Filter, string]] : []),
          ] as [Filter, string][]).map(([key, label]) => (
            <button key={key} type="button" className={`sa-filter ${filter === key ? 'active' : ''}`} onClick={() => setFilter(key)}>
              {label}
            </button>
          ))}
        </div>
      </header>

      <ul className="sa-list">
        {filtered.map((s, idx) => {
          const st = stateOf(s);
          const key = s.id || String(idx);
          const open = isOpen(s, idx);
          const cur = s.step || 0, max = s.max_steps || 10;
          const pct = Math.min(100, Math.round((cur / max) * 100));
          return (
            <li key={key} className={`sa-item is-${st}`}>
              <button type="button" className="sa-sum" onClick={() => setExpanded((e) => ({ ...e, [key]: !open }))}>
                <span className="sa-node" aria-hidden="true">
                  {st === 'completed' ? <Check size={12} strokeWidth={2.6} />
                    : st === 'failed' || st === 'exhausted' ? <X size={12} strokeWidth={2.6} />
                      : st === 'running' ? null : <Circle size={7} strokeWidth={2} />}
                </span>
                <span className="sa-name">{s.role || 'subagent'}</span>
                <span className="sa-meta">{cur}/{max}</span>
                <span className="sa-chev" aria-hidden="true">{open ? '⌄' : '›'}</span>
              </button>
              <div className="sa-progress"><div className="sa-progress-fill" style={{ width: `${pct}%` }} /></div>
              {open ? (
                <div className="sa-detail">
                  {st === 'running' && (
                    <div className="sa-detail-row">
                      <span className="sa-label">Now</span>
                      <span className="sa-value">{s.current_tool ? <code>{s.current_tool}</code> : 'reasoning…'}</span>
                    </div>
                  )}
                  {s.task ? (
                    <div className="sa-detail-row">
                      <span className="sa-label">Task</span>
                      <span className="sa-value">{s.task}</span>
                    </div>
                  ) : null}
                  {(st === 'failed' || st === 'exhausted') && (s.error || s.failure_reason) ? (
                    <div className="sa-detail-row is-failed">
                      <span className="sa-label">Issue</span>
                      <span className="sa-value">{s.error || s.failure_reason}</span>
                    </div>
                  ) : null}
                  {s.output && st !== 'running' ? (
                    <div className="sa-detail-row">
                      <span className="sa-label">Output</span>
                      <pre className="sa-output">{s.output}</pre>
                    </div>
                  ) : null}
                </div>
              ) : null}
            </li>
          );
        })}
      </ul>
    </div>
  );
};

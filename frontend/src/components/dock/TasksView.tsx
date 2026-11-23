import React, { useCallback, useEffect, useState } from 'react';
import { Check, Circle, Square, X } from 'lucide-react';
import { useAppStore } from '../../stores/useAppStore';
import { Spinner } from '../common/AppLoader';
import '../../styles/tasks.css';
import { fetchTasks, stopTask, type Task } from '../../services/tasks';

const POLL_MS = 3000;

function ago(ts: number | null): string {
  if (!ts) return '';
  const s = Math.max(0, Math.round(Date.now() / 1000 - ts));
  if (s < 60) return `${s}s ago`;
  if (s < 3600) return `${Math.round(s / 60)}m ago`;
  if (s < 86400) return `${Math.round(s / 3600)}h ago`;
  return `${Math.round(s / 86400)}d ago`;
}

const node = (state: Task['state']) => {
  if (state === 'completed') return <Check size={12} strokeWidth={2.6} />;
  if (state === 'failed' || state === 'interrupted') return <X size={12} strokeWidth={2.6} />;
  if (state === 'stopped' || state === 'cancelled') return <Square size={7} strokeWidth={2} />;
  if (state === 'running') return null;
  return <Circle size={7} strokeWidth={2} />;
};

/** Everything running (or recently run) in the background for this user, with Stop. Polls only while shown. */
export const TasksView: React.FC = () => {
  const showToast = useAppStore((s) => s.showToast);
  const [tasks, setTasks] = useState<Task[] | null>(null);
  const [failed, setFailed] = useState<string | false>(false);
  const [stopping, setStopping] = useState<string | null>(null);

  const load = useCallback(async (alive: () => boolean) => {
    try {
      const rows = await fetchTasks();
      if (alive()) { setTasks(rows); setFailed(false); }
    } catch (e: any) {
      if (alive()) setFailed(String(e?.message || 'network error'));   // shown, so a failing poll says why
    }
  }, []);

  useEffect(() => {
    let live = true;
    const alive = () => live;
    load(alive);
    const t = window.setInterval(() => load(alive), POLL_MS);
    return () => { live = false; window.clearInterval(t); };
  }, [load]);

  const onStop = async (t: Task) => {
    setStopping(t.id);
    try {
      const r = await stopTask(t.id);
      showToast(r.stopped ? `Stopping: ${t.title}` : r.note || 'Nothing to stop', r.stopped ? 'success' : 'info');
      await load(() => true);
    } catch (e: any) {
      showToast(e?.message || 'Could not stop the task', 'error');
    } finally {
      setStopping(null);
    }
  };

  if (tasks === null) {
    return failed ? (
      <div className="tk-error">Could not load tasks ({failed}). Retrying…</div>
    ) : (
      <Spinner label="Checking tasks…" />
    );
  }

  const active = tasks.filter((t) => t.state === 'running');
  const recent = tasks.filter((t) => t.state !== 'running');

  if (!tasks.length) {
    return (
      <div className="tk-empty">
        <div className="tk-empty-rail" aria-hidden="true"><span /><span /><span /></div>
        <div className="tk-empty-copy">
          <span className="tk-empty-kicker">Idle</span>
          <h2>No background tasks</h2>
          <p>Agent runs, media jobs and knowledge ingests show up here while they work.</p>
        </div>
      </div>
    );
  }

  const row = (t: Task, isLast: boolean) => (
    <li key={t.id} className={`tk-row is-${t.state}${isLast ? ' is-last' : ''}`} data-task-id={t.id}>
      <div className="tk-rail" aria-hidden="true">
        <span className="tk-node">{node(t.state)}</span>
      </div>
      <div className="tk-body">
        <div className="tk-title" title={t.title}>{t.title}</div>
        <div className="tk-meta">
          {t.state}{t.stage ? ` · ${t.stage}` : ''}{t.step ? ` · step ${t.step}` : ''}{t.reason ? ` · ${t.reason}` : ''}
          {' · '}{t.state === 'running' ? `running ${ago(t.created_at).replace(' ago', '')}` : ago(t.updated_at)}
        </div>
      </div>
      {t.can_stop && (
        <button type="button" className="tk-stop" title="Stop this task" disabled={stopping === t.id} onClick={() => onStop(t)}>
          {stopping === t.id ? '…' : 'Stop'}
        </button>
      )}
    </li>
  );

  return (
    <div className="tk-shell">
      <header className="tk-head">
        <span className="tk-head-label">Background tasks</span>
        <span className={`tk-head-count${active.length ? ' is-live' : ''}`}>{active.length}<span> running</span></span>
      </header>
      {failed ? <div className="tk-error">Refresh failed ({failed}) — showing the last known state.</div> : null}

      {active.length > 0 && (
        <section className="tk-section-wrap">
          <h4 className="tk-section">Running</h4>
          <ul className="tk-list">{active.map((t, i) => row(t, i === active.length - 1))}</ul>
        </section>
      )}
      {recent.length > 0 && (
        <section className="tk-section-wrap">
          <h4 className="tk-section">Recent</h4>
          <ul className="tk-list">{recent.map((t, i) => row(t, i === recent.length - 1))}</ul>
        </section>
      )}
    </div>
  );
};

import React, { useCallback, useEffect, useState } from 'react';
import { useAppStore } from '../../stores/useAppStore';
import { Spinner } from '../common/AppLoader';
import { fetchTasks, stopTask, type Task } from '../../services/tasks';

const POLL_MS = 3000;

const MARK: Record<string, string> = {
  running: '⏳', completed: '✅', failed: '❌', cancelled: '⏹', stopped: '⏹', interrupted: '⚠️',
};
const CLS: Record<string, string> = {
  running: 'st-in_progress', completed: 'st-done', failed: 'st-failed', interrupted: 'st-failed',
};

function ago(ts: number | null): string {
  if (!ts) return '';
  const s = Math.max(0, Math.round(Date.now() / 1000 - ts));
  if (s < 60) return `${s}s ago`;
  if (s < 3600) return `${Math.round(s / 60)}m ago`;
  if (s < 86400) return `${Math.round(s / 3600)}h ago`;
  return `${Math.round(s / 86400)}d ago`;
}

/** Everything running (or recently run) in the background for this user, with Stop. Polls only while shown. */
export const TasksView: React.FC = () => {
  const showToast = useAppStore((s) => s.showToast);
  const [tasks, setTasks] = useState<Task[] | null>(null);
  const [failed, setFailed] = useState(false);
  const [stopping, setStopping] = useState<string | null>(null);

  const load = useCallback(async (alive: () => boolean) => {
    try {
      const rows = await fetchTasks();
      if (alive()) { setTasks(rows); setFailed(false); }
    } catch {
      if (alive()) setFailed(true);
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
      <div style={{ padding: '32px 16px', textAlign: 'center', color: 'var(--dim)', fontSize: '12.5px' }}>Could not load tasks. Retrying…</div>
    ) : (
      <Spinner label="Loading tasks…" />
    );
  }

  const active = tasks.filter((t) => t.state === 'running');
  const recent = tasks.filter((t) => t.state !== 'running');

  if (!tasks.length) {
    return (
      <div style={{ padding: '32px 16px', textAlign: 'center', color: 'var(--dim)', fontSize: '12.5px' }}>
        <div style={{ fontSize: 24, marginBottom: 8 }}>🗂</div>
        <b>No background tasks</b>
        <p style={{ marginTop: 6, fontSize: '11.5px' }}>Agent runs, media jobs and knowledge ingests show up here while they work.</p>
      </div>
    );
  }

  const row = (t: Task) => (
    <div key={t.id} className={`plan-doc-step-item ${CLS[t.state] || ''}`} data-task-id={t.id}>
      <span className="plan-doc-step-mark">{MARK[t.state] || '☐'}</span>
      <div style={{ flex: 1, minWidth: 0 }}>
        <div style={{ fontWeight: t.state === 'running' ? 600 : 'normal', overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }} title={t.title}>
          {t.title}
        </div>
        <div style={{ fontSize: 11, color: 'var(--dim)', marginTop: 2 }}>
          {t.state}{t.stage ? ` · ${t.stage}` : ''}{t.step ? ` · step ${t.step}` : ''}{t.reason ? ` · ${t.reason}` : ''}
          {' · '}{t.state === 'running' ? `running ${ago(t.created_at).replace(' ago', '')}` : ago(t.updated_at)}
        </div>
      </div>
      {t.can_stop && (
        <button
          type="button"
          className="dock-icon-btn"
          title="Stop this task"
          disabled={stopping === t.id}
          onClick={() => onStop(t)}
          style={{ cursor: 'pointer', padding: '2px 8px', border: '1px solid var(--border)', borderRadius: 4 }}
        >
          {stopping === t.id ? '…' : 'Stop'}
        </button>
      )}
    </div>
  );

  return (
    <>
      <div className="plan-doc-header">
        <div className="plan-doc-title">Background tasks</div>
        <div style={{ fontSize: 12, color: 'var(--dim)' }}>
          <b>{active.length}</b> running{failed ? <> · <span style={{ color: 'var(--amber)' }}>refresh failed</span></> : null}
        </div>
      </div>
      {active.length > 0 && (
        <>
          <div className="plan-doc-section-title">Running</div>
          <div className="plan-doc-steps-list">{active.map(row)}</div>
        </>
      )}
      {recent.length > 0 && (
        <>
          <div className="plan-doc-section-title">Recent</div>
          <div className="plan-doc-steps-list">{recent.map(row)}</div>
        </>
      )}
    </>
  );
};

import { apiFetch } from './api';

export interface Task {
  id: string;
  kind: 'chat' | 'agent' | 'subagent' | 'deck' | 'kb_ingest' | 'media';
  title: string;
  state: 'running' | 'completed' | 'stopped' | 'failed' | 'cancelled' | 'interrupted';
  stage: string | null;
  step: number;
  reason: string | null;
  session_id: string | null;
  parent_id: string | null;
  created_at: number | null;
  updated_at: number | null;
  age_s: number;
  resumable: boolean;
  can_stop: boolean;
  can_attach: boolean;
  attach: { events: string } | null;
}

export interface StopResult {
  ok: boolean;
  stopped: boolean;
  note: string;
}

/** This user's background work, running first. Polled, so it does not slide the idle sign-out. */
export async function fetchTasks(limit = 50): Promise<Task[]> {
  const r = await apiFetch(`/tasks?limit=${limit}`, { background: true });
  if (!r.ok) throw new Error(`tasks: HTTP ${r.status}`);
  const d = await r.json();
  return Array.isArray(d?.tasks) ? d.tasks : [];
}

export async function stopTask(id: string): Promise<StopResult> {
  const r = await apiFetch(`/tasks/${encodeURIComponent(id)}/stop`, { method: 'POST' });
  if (r.status === 404) throw new Error('That task is gone');
  if (!r.ok) throw new Error(`Could not stop the task (HTTP ${r.status})`);
  return r.json();
}

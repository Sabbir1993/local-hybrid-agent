// Which folder the Code tab and file previews read: in Agent mode the active agent's own work folder, in Code mode the project.
// One never stands in for the other (the server enforces it too: an agent without a folder is an error, not the project).
import { useAppStore } from '../stores/useAppStore';

export interface WsTarget {
  /** 'agent' | 'project' */
  kind: 'agent' | 'project';
  /** the agent's id when kind is 'agent' and one is selected */
  agentId: string | null;
  /** the folder on the user's machine (agent work_dir or project path), '' when there is none */
  dir: string;
  /** why there is nothing to show, in words for the person; '' when there is a folder */
  problem: string;
}

export function wsTarget(): WsTarget {
  const st = useAppStore.getState();
  if (st.appMode === 'agent') {
    const a = st.activeCustomAgent;
    if (!a || a.id == null) return { kind: 'agent', agentId: null, dir: '', problem: 'Select an agent to see its files.' };
    const dir = String(a.work_dir || '').trim();
    return {
      kind: 'agent', agentId: String(a.id), dir,
      problem: dir ? '' : 'This agent has no work folder. Set one in Settings → Custom Agents.',
    };
  }
  const p = st.projects.find((x) => x.id === st.activeProjectId);
  const ok = Boolean(p && p.name && p.name !== 'scratch' && p.name !== 'default');
  return { kind: 'project', agentId: null, dir: ok ? String(p!.workspace_dir || p!.path || '') : '', problem: ok ? '' : 'No project active' };
}

/** "&agent=<id>" for the requests that read workspace files, '' outside Agent mode. */
export function agentWsParam(): string {
  const t = wsTarget();
  return t.kind === 'agent' && t.agentId ? `&agent=${encodeURIComponent(t.agentId)}` : '';
}

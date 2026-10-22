import React, { useCallback, useEffect, useRef, useState } from 'react';
import { apiFetch } from '../../services/api';
import { useAppStore } from '../../stores/useAppStore';

interface GitFile {
  path: string;
  index_status: string;
  worktree_status?: string;
}

interface GitStatus {
  branch?: string;
  detached?: boolean;
  ahead?: number;
  behind?: number;
  files: GitFile[];
  error?: string;
}

async function gitJson(url: string, body?: any, background = false): Promise<any> {
  const r = await apiFetch(url, body === undefined ? { background } : { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) });
  const d = await r.json().catch(() => ({}));
  if (!r.ok) {
    const e = d && d.error;
    throw new Error(typeof e === 'string' && e.trim() ? e : e ? JSON.stringify(e) : `request failed (HTTP ${r.status})`);
  }
  return d;
}

function useGitStatus() {
  const { appMode, activeProjectId, projects, isStreaming } = useAppStore();
  const [status, setStatus] = useState<GitStatus | null>(null);
  const project = projects.find((p) => p.id === activeProjectId);
  const eligible = appMode === 'code' && Boolean(project && project.name && project.name !== 'scratch' && project.name !== 'default');

  // A project that is not a git repo answers 400; keep quiet instead of re-polling it every 20 s
  const notRepo = useRef(false);

  const refresh = useCallback(async (force = false) => {
    if (!eligible) {
      setStatus(null);
      return;
    }
    if (notRepo.current && !force) return;
    try {
      const d = await gitJson('/git/status', undefined, true);   // a poll is not user activity
      notRepo.current = false;
      setStatus(d.error || !d.branch ? null : d);
    } catch (_) {
      notRepo.current = true;
      setStatus(null);
    }
  }, [eligible, activeProjectId]);

  // on mount / project change, every 20 s while idle, and again when an agent run ends
  useEffect(() => {
    notRepo.current = false;
    refresh(true);
    const t = window.setInterval(() => refresh(), 20000);
    return () => window.clearInterval(t);
  }, [refresh]);
  useEffect(() => {
    if (!isStreaming) refresh(true);
  }, [isStreaming]);

  return { status, project, eligible, refresh };
}

const DiffText: React.FC<{ text: string; onCounts: (add: number, del: number) => void }> = ({ text, onCounts }) => {
  let add = 0;
  let del = 0;
  const lines = text.split('\n').map((line, i) => {
    let color = '';
    if (line.startsWith('+') && !line.startsWith('+++')) { add++; color = '#4ade80'; }
    else if (line.startsWith('-') && !line.startsWith('---')) { del++; color = '#f87171'; }
    else if (line.startsWith('@@')) color = '#60a5fa';
    else if (/^(diff |index |--- |\+\+\+ |new file|deleted file)/.test(line)) color = 'var(--dim)';
    return <div key={i} style={color ? { color } : undefined}>{line || ' '}</div>;
  });
  useEffect(() => { onCounts(add, del); }, [text]);
  return <>{lines}</>;
};

const GitFileRow: React.FC<{ file: GitFile; staged: boolean; forceOpen: boolean | null; onChanged: () => void }> = ({ file, staged, forceOpen, onChanged }) => {
  const { showToast } = useAppStore();
  const untracked = file.index_status === '?' || file.worktree_status === '?';
  const code = untracked ? 'U' : staged ? file.index_status : file.worktree_status;
  const [open, setOpen] = useState(forceOpen ?? false);
  const [diff, setDiff] = useState<string | null>(null);
  const [err, setErr] = useState('');
  const [counts, setCounts] = useState<[number, number] | null>(null);

  useEffect(() => { if (forceOpen !== null) setOpen(forceOpen); }, [forceOpen]);
  useEffect(() => {
    if (!open || diff !== null) return;
    gitJson(`/git/diff?path=${encodeURIComponent(file.path)}&staged=${staged}&untracked=${untracked}`)
      .then((d) => setDiff(d.diff || ''))
      .catch((e) => setErr(e.message));
  }, [open]);

  const toggleStage = async () => {
    try {
      await gitJson(`/git/${staged ? 'unstage' : 'stage'}`, { paths: [file.path] });
      onChanged();
    } catch (e: any) {
      showToast('Failed: ' + e.message, 'error');
    }
  };

  return (
    <div className="git-file" data-path={file.path}>
      <div className="git-file-row" style={{ display: 'flex', alignItems: 'center', gap: 6, fontSize: 11 }}>
        <button className="btn ghost git-toggle" style={{ width: 'auto', margin: 0, padding: '1px 6px', fontSize: 10 }} title={staged ? 'Unstage' : 'Stage'} onClick={toggleStage}>
          {staged ? '−' : '+'}
        </button>
        <span className="mono git-file-link" style={{ cursor: 'pointer', flex: 1, wordBreak: 'break-all' }} title="Show changes" onClick={() => setOpen((o) => !o)}>
          <span className="git-chev dim">{open ? '▾' : '▸'}</span> {file.path}
        </span>
        <span className="mono git-counts dim" style={{ fontSize: 10 }}>
          {counts && (<><span style={{ color: '#4ade80' }}>+{counts[0]}</span> <span style={{ color: '#f87171' }}>−{counts[1]}</span></>)}
        </span>
        <span className="mono dim" style={{ fontSize: 10 }} title={untracked ? 'New file (not tracked yet)' : 'Status'}>{code}</span>
      </div>
      {open && (
        <pre className="git-inline-diff" style={{ maxHeight: 260, overflow: 'auto', fontSize: 10.5, lineHeight: 1.45, background: 'var(--bg-input)', border: '1px solid var(--border)', borderRadius: 6, padding: '6px 8px', margin: '3px 0 4px', whiteSpace: 'pre' }}>
          {err ? 'Failed: ' + err : diff === null ? 'Loading…' : diff.trim() ? <DiffText text={diff} onCounts={(a, d) => setCounts([a, d])} /> : '(no changes)'}
        </pre>
      )}
    </div>
  );
};

const FileGroup: React.FC<{ label: string; files: GitFile[]; staged: boolean; onChanged: () => void }> = ({ label, files, staged, onChanged }) => {
  // a few changed files show their lines right away; "Expand all" overrides
  const [forceOpen, setForceOpen] = useState<boolean | null>(files.length <= 4 ? true : null);
  if (!files.length) return null;
  const allOpen = forceOpen === true;
  return (
    <div className="cap-item git-group">
      <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center' }}>
        <b style={{ fontSize: 10.5, textTransform: 'uppercase', letterSpacing: 0.5 }}>{label} ({files.length})</b>
        <button className="btn ghost git-expand-all" style={{ width: 'auto', margin: 0, padding: '1px 8px', fontSize: 10 }} onClick={() => setForceOpen(!allOpen)}>
          {allOpen ? 'Collapse all' : 'Expand all'}
        </button>
      </div>
      <div style={{ display: 'flex', flexDirection: 'column', gap: 2, marginTop: 3 }}>
        {files.map((f) => <GitFileRow key={f.path + staged} file={f} staged={staged} forceOpen={forceOpen} onChanged={onChanged} />)}
      </div>
    </div>
  );
};

const inputStyle: React.CSSProperties = { width: '100%', marginTop: 4, background: 'var(--bg-input)', color: 'var(--text)', border: '1px solid var(--border)', borderRadius: 6, padding: '5px 8px', fontSize: 11, fontFamily: 'inherit' };

const GitModal: React.FC<{ onClose: () => void; onChanged: () => void }> = ({ onClose, onChanged }) => {
  const { showToast } = useAppStore();
  const [st, setSt] = useState<GitStatus | null>(null);
  const [branches, setBranches] = useState<string[]>([]);
  const [defaultBase, setDefaultBase] = useState('main');
  const [checkpoints, setCheckpoints] = useState<any[]>([]);
  const [error, setError] = useState('');
  const [commitMsg, setCommitMsg] = useState('');
  const [prTitle, setPrTitle] = useState('');
  const [prBody, setPrBody] = useState('');
  const [prBase, setPrBase] = useState('main');
  const [prStatus, setPrStatus] = useState('');
  const [busy, setBusy] = useState('');
  const [loadKey, setLoadKey] = useState(0);

  const reload = () => { setLoadKey((k) => k + 1); onChanged(); };

  useEffect(() => {
    let alive = true;
    (async () => {
      try {
        const [s, b, c] = await Promise.all([
          gitJson('/git/status'),
          gitJson('/git/branches'),
          gitJson('/git/checkpoints' + (useAppStore.getState().activeSessionId ? `?session_id=${encodeURIComponent(String(useAppStore.getState().activeSessionId))}` : '')).catch(() => ({ checkpoints: [] })),
        ]);
        if (!alive) return;
        if (s.error) { setError(s.error); return; }
        setError('');
        setSt(s);
        setBranches(b.branches || []);
        setDefaultBase(b.default || 'main');
        setPrBase((cur) => {
          const list: string[] = b.branches || [];
          if (list.includes(cur)) return cur;
          return b.default && list.includes(b.default) ? b.default : list[0] || b.default || 'main';
        });
        setCheckpoints(c.checkpoints || []);
      } catch (e: any) {
        if (alive) setError('Failed: ' + e.message);
      }
    })();
    return () => { alive = false; };
  }, [loadKey]);

  const staged = (st?.files || []).filter((f) => f.index_status !== ' ' && f.index_status !== '?');
  const unstaged = (st?.files || []).filter((f) => f.worktree_status !== ' ' && f.worktree_status !== undefined);

  const commit = async () => {
    if (!commitMsg.trim()) { showToast('Commit message required', 'error'); return; }
    try {
      await gitJson('/git/commit', { message: commitMsg.trim() });
      showToast('Committed ✓', 'success');
      setCommitMsg('');
      reload();
    } catch (e: any) { showToast('Commit failed: ' + e.message, 'error'); }
  };
  const push = async () => {
    if (!window.confirm('Push committed changes to the remote now?')) return;
    try { await gitJson('/git/push', {}); showToast('Pushed ✓', 'success'); reload(); }
    catch (e: any) { showToast('Push failed: ' + e.message, 'error'); }
  };
  const suggestCommit = async () => {
    setBusy('commit');
    try {
      const d = await gitJson('/git/suggest_commit_message', {});
      if (!d.message || !d.message.trim()) throw new Error('model returned no text — it may have used its whole budget on reasoning; try again');
      setCommitMsg(d.message);
    } catch (e: any) { showToast('Generate failed: ' + e.message, 'error'); }
    setBusy('');
  };
  const suggestPr = async () => {
    setBusy('pr');
    try {
      const d = await gitJson('/git/suggest_pr', { base: prBase.trim() || 'main' });
      if (!d.title && !d.body) throw new Error('model returned no text — it may have used its whole budget on reasoning; try again');
      setPrTitle(d.title || '');
      setPrBody(d.body || '');
    } catch (e: any) { showToast('Generate failed: ' + e.message, 'error'); }
    setBusy('');
  };
  const createPr = async () => {
    const base = prBase.trim() || 'main';
    if (!prTitle.trim()) { showToast('PR title required', 'error'); return; }
    if (!window.confirm(`Open a pull request "${prTitle.trim()}" against ${base}?`)) return;
    setPrStatus('Creating…');
    try {
      await gitJson('/git/pr', { title: prTitle.trim(), body: prBody.trim(), base });
      setPrStatus('PR created ✓');
      showToast('PR created ✓', 'success');
    } catch (e: any) {
      setPrStatus('Failed: ' + e.message);
      showToast('PR failed: ' + e.message, 'error');
    }
  };
  const rewind = async (c: any) => {
    if (!window.confirm('Rewind the workspace to the checkpoint at step ' + c.step + '? Tracked files go back to how they were then. Your current state is saved first (the toast shows how to get it back), and new untracked files are left alone.')) return;
    try {
      const d = await gitJson('/git/rewind', { run_id: c.run_id, step: Number(c.step || 0) });
      const left = (d.left_untracked || []).length;
      showToast('Rewound to ' + String(d.rewound_to || '').slice(0, 8)
        + (d.backup ? ' - previous state saved; to undo: git restore --source=' + String(d.backup).slice(0, 12) + ' --staged --worktree -- .' : '')
        + (left ? ' - ' + left + ' new file(s) kept' : ''), 'info', 9000);
      reload();
    } catch (e: any) { showToast('rewind failed: ' + e.message, 'error'); }
  };

  return (
    <div id="git-modal" onClick={(e) => { if ((e.target as HTMLElement).id === 'git-modal') onClose(); }}>
      <div id="git-box">
        <h2>
          <span style={{ display: 'flex', alignItems: 'center', gap: 6 }}>
            <svg width="18" height="18" viewBox="0 0 16 16" fill="currentColor" aria-hidden="true"><path d="M8 0c-4.42 0-8 3.58-8 8a8.013 8.013 0 0 0 5.47 7.59c.4.08.55-.17.55-.38 0-.19-.01-.82-.01-1.49-2.01.37-2.53-.49-2.69-.94-.09-.23-.48-.94-.82-1.13-.28-.15-.68-.52-.01-.53.63-.01 1.08.58 1.23.82.72 1.21 1.87.87 2.33.66.07-.52.28-.87.51-1.07-1.78-.2-3.64-.89-3.64-3.95 0-.87.31-1.59.82-2.15-.08-.2-.36-1.02.08-2.12 0 0 .67-.21 2.2.82.64-.18 1.32-.27 2-.27.68 0 1.36.09 2 .27 1.53-1.04 2.2-.82 2.2-.82.44 1.1.16 1.92.08 2.12.51.56.82 1.27.82 2.15 0 3.07-1.87 3.75-3.65 3.95.29.25.54.73.54 1.48 0 1.07-.01 1.93-.01 2.2 0 .21.15.46.55.38A8.013 8.013 0 0 0 16 8c0-4.42-3.58-8-8-8z" /></svg>
            <span>Source Control</span>
          </span>
          <button className="rep-close" id="git-modal-close" onClick={onClose}>✕</button>
        </h2>
        <div className="cfg-note" style={{ margin: '4px 0 10px' }}>
          Operates on the active project's workspace directory. Push and PR creation are side-effecting — you'll be asked to confirm. PR creation needs GitHub connected under Settings → Capabilities → MCP Servers.
        </div>
        <div id="git-content">
          {error ? <div className="mon-empty">{error}</div> : !st ? <div className="mon-empty">Loading…</div> : (
            <>
              <div className="cap-item" style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center' }}>
                <span>
                  {st.detached ? '⚠ detached at ' : ''}<b>{st.branch || '?'}</b>{' '}
                  {st.ahead ? <span className="dim">↑{st.ahead}</span> : null}
                  {st.behind ? <span className="dim">↓{st.behind}</span> : null}
                </span>
                <button className="btn ghost" id="git-refresh" style={{ width: 'auto', margin: 0, padding: '2px 10px', fontSize: 10.5 }} onClick={reload}>⟳ Refresh</button>
              </div>
              <div className="cap-item" style={{ marginBottom: 6 }}>
                <b style={{ fontSize: 10.5, textTransform: 'uppercase', letterSpacing: 0.5 }}>Checkpoints</b>
                {checkpoints.length === 0 && <div className="dim" style={{ fontSize: 10 }}>No checkpoints yet.</div>}
                {checkpoints.slice(0, 5).map((c) => (
                  <div key={c.ref || c.run_id + c.step} style={{ display: 'flex', alignItems: 'center', gap: 6, padding: '2px 0' }}>
                    <span className="dim mono" style={{ fontSize: 10, flex: 1 }} title={c.ref}>run {String(c.run_id).slice(0, 8)} · step {c.step}</span>
                    <button className="btn ghost" style={{ width: 'auto', margin: 0, padding: '1px 8px', fontSize: 10 }} onClick={() => rewind(c)}>◀ Rewind</button>
                  </div>
                ))}
              </div>
              <FileGroup key={`s${loadKey}`} label="Staged" files={staged} staged onChanged={reload} />
              <FileGroup key={`u${loadKey}`} label="Changes" files={unstaged} staged={false} onChanged={reload} />
              <div className="cap-item" style={{ marginTop: 8 }}>
                <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center' }}>
                  <span className="dim" style={{ fontSize: 10.5 }}>Commit message</span>
                  <button className="btn ghost" id="git-suggest-commit-btn" disabled={busy === 'commit'} style={{ width: 'auto', margin: 0, padding: '1px 8px', fontSize: 10 }} title="Generate from the diff using the app's own model" onClick={suggestCommit}>
                    {busy === 'commit' ? '⏳ …' : '✨ Generate'}
                  </button>
                </div>
                <textarea id="git-commit-msg" rows={2} placeholder="Commit message" value={commitMsg} onChange={(e) => setCommitMsg(e.target.value)} style={{ ...inputStyle, fontSize: 11.5, resize: 'vertical', marginTop: 3 }} />
                <div style={{ display: 'flex', gap: 6, marginTop: 6 }}>
                  <button className="btn accent" id="git-commit-btn" style={{ width: 'auto', margin: 0, padding: '4px 12px', fontSize: 11 }} onClick={commit}>✅ Commit</button>
                  <button className="btn ghost" id="git-push-btn" style={{ width: 'auto', margin: 0, padding: '4px 12px', fontSize: 11 }} onClick={push}>⬆ Push</button>
                </div>
              </div>
              <div className="cap-item" style={{ marginTop: 6, borderTop: '1px solid var(--border)', paddingTop: 8 }}>
                <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center' }}>
                  <b style={{ fontSize: 11 }}>Create Pull Request <span className="dim" style={{ fontWeight: 'normal' }}>from <span className="mono">{st.branch || '?'}</span></span></b>
                  <button className="btn ghost" id="git-suggest-pr-btn" disabled={busy === 'pr'} style={{ width: 'auto', margin: 0, padding: '1px 8px', fontSize: 10 }} title="Generate title + description from the diff against base" onClick={suggestPr}>
                    {busy === 'pr' ? '⏳ …' : '✨ Generate'}
                  </button>
                </div>
                <input type="text" id="git-pr-title" placeholder="PR title" value={prTitle} onChange={(e) => setPrTitle(e.target.value)} style={inputStyle} />
                <textarea id="git-pr-body" rows={2} placeholder="Description (optional)" value={prBody} onChange={(e) => setPrBody(e.target.value)} style={{ ...inputStyle, resize: 'vertical' }} />
                <div style={{ display: 'flex', gap: 6, marginTop: 4, alignItems: 'center' }}>
                  <span className="dim" style={{ fontSize: 10.5 }}>base:</span>
                  <select id="git-pr-base" value={prBase} onChange={(e) => setPrBase(e.target.value)} style={{ background: 'var(--bg-input)', color: 'var(--text)', border: '1px solid var(--border)', borderRadius: 6, padding: '4px 6px', fontSize: 11 }}>
                    {(branches.length ? branches : [defaultBase]).map((b) => <option key={b} value={b}>{b}</option>)}
                  </select>
                  <button className="btn ghost" id="git-pr-btn" style={{ width: 'auto', marginLeft: 'auto', padding: '4px 12px', fontSize: 11 }} onClick={createPr}>🔀 Create PR</button>
                </div>
                <div id="git-pr-status" className="dim" style={{ fontSize: 10.5, marginTop: 4 }}>{prStatus}</div>
              </div>
            </>
          )}
        </div>
      </div>
    </div>
  );
};

/** Branch + change summary above the composer (Coding Agent mode, active project) and the Source Control panel it opens. */
export const GitStrip: React.FC = () => {
  const { status, project, eligible, refresh } = useGitStatus();
  const [dismissed, setDismissed] = useState(false);
  const [open, setOpen] = useState(false);

  // a different project brings the bar back
  useEffect(() => setDismissed(false), [project?.id]);

  if (!eligible || !status) return null;
  const changes = (status.files || []).length;

  return (
    <>
      {!dismissed && (
        <div className="claude-git-status-strip" id="claude-git-status-strip" style={{ display: 'flex' }}>
          <div className="claude-git-status-left">
            <span className="claude-git-branch" id="claude-git-branch">{project?.name} · {status.branch}</span>
            <span className="claude-git-diffs" id="claude-git-diffs">
              {changes > 0 ? <span className="claude-diff-add">+{changes} modified</span> : <span style={{ color: 'var(--dim)' }}>clean</span>}
            </span>
          </div>
          <div style={{ display: 'flex', alignItems: 'center', gap: 6 }}>
            <button className="claude-git-action-btn" id="claude-git-action-btn" onClick={() => setOpen(true)}>Source Control</button>
            <button className="claude-icon-tool" id="claude-git-dismiss-btn" title="Dismiss bar" onClick={() => setDismissed(true)}>✕</button>
          </div>
        </div>
      )}
      {open && <GitModal onClose={() => setOpen(false)} onChanged={() => refresh(true)} />}
    </>
  );
};

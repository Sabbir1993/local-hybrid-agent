import React, { useEffect, useState } from 'react';
import { apiFetch, getDeviceDisplayLabel } from '../../services/api';
import { useAppStore } from '../../stores/useAppStore';

async function browse(cur: string): Promise<string> {
  try {
    const w = window as any;
    if (w.electronAPI && typeof w.electronAPI.browseFolder === 'function') return (await w.electronAPI.browseFolder(cur || '')) || '';
    // the server asks the paired companion (or, on localhost, the OS dialog)
    const r = await apiFetch('/control/browse_folder', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ initial_dir: cur || '' }),
    });
    const d = await r.json().catch(() => ({}));
    if (!r.ok) throw new Error(d.error || `HTTP ${r.status}`);
    return d.path || '';
  } catch (e: any) {
    throw new Error(e?.message || 'Could not open the folder picker');
  }
}

/** Register a project (name + folder on this device) or change an existing project's folder. */
export const ProjectModal: React.FC = () => {
  const { projectModal, closeProjectModal, fetchProjects, setActiveProjectId, showToast } = useAppStore();
  const editing = projectModal?.mode === 'folder' ? projectModal.project : null;
  const [name, setName] = useState('');
  const [dir, setDir] = useState('');
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');

  useEffect(() => {
    if (!projectModal) return;
    setName(editing?.name || '');
    setDir(editing?.workspace_dir || editing?.path || '');
    setError('');
    const onKey = (e: KeyboardEvent) => { if (e.key === 'Escape') closeProjectModal(); };
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  }, [projectModal]);

  if (!projectModal) return null;

  const pick = async () => {
    try {
      const p = await browse(dir);
      if (p) setDir(p);
    } catch (e: any) {
      setError(e.message);
    }
  };

  const save = async (e: React.FormEvent) => {
    e.preventDefault();
    if (!name.trim() || !dir.trim()) return setError('Give the project a name and choose its folder.');
    setBusy(true);
    setError('');
    try {
      const res = editing
        ? await apiFetch(`/control/projects/${editing.id}/workspace`, {
            method: 'PATCH',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ workspace_dir: dir.trim() }),
          })
        : await apiFetch('/control/projects', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ name: name.trim(), workspace_dir: dir.trim() }),
          });
      const d = await res.json().catch(() => ({}));
      if (!res.ok) throw new Error(d.error || `HTTP ${res.status}`);
      await fetchProjects();
      if (!editing && d.project?.id != null) setActiveProjectId(String(d.project.id));
      showToast(editing ? 'Project folder updated' : `Project "${name.trim()}" registered`, 'success');
      closeProjectModal();
    } catch (err: any) {
      setError(err.message);
    }
    setBusy(false);
  };

  return (
    <div className="ca-modal-overlay" style={{ display: 'flex' }} onMouseDown={(e) => { if (e.target === e.currentTarget) closeProjectModal(); }}>
      <form className="ca-modal-box" style={{ maxWidth: 520 }} onSubmit={save}>
        <div className="ca-modal-head">
          <div className="ca-modal-title-wrap">
            <div className="ca-modal-device-badge"><span>💻</span><span>Device: {getDeviceDisplayLabel()}</span></div>
            <span>{editing ? `Change folder: ${editing.name}` : 'Register a project'}</span>
          </div>
          <button type="button" className="rep-close" title="Close (Esc)" onClick={closeProjectModal}>✕</button>
        </div>
        <div className="ca-modal-body" style={{ padding: '16px 20px' }}>
          <div className="ca-field" style={{ marginBottom: 12 }}>
            <label htmlFor="proj-name">Project name</label>
            <input id="proj-name" type="text" autoFocus={!editing} disabled={Boolean(editing)} placeholder="e.g. Settlement reports" value={name} onChange={(e) => setName(e.target.value)} />
          </div>
          <div className="ca-field">
            <label htmlFor="proj-dir">Folder on this device</label>
            <div style={{ display: 'flex', gap: 6 }}>
              <input id="proj-dir" type="text" autoFocus={Boolean(editing)} spellCheck={false} placeholder="e.g. D:\Projects\Settlement" style={{ flex: 1 }} value={dir} onChange={(e) => setDir(e.target.value)} />
              <button type="button" className="btn secondary" style={{ width: 'auto', padding: '6px 12px', whiteSpace: 'nowrap' }} onClick={pick}>Browse…</button>
            </div>
            <span className="ca-field-hint">The agent reads and writes inside this folder only. Files stay on your machine and are never copied to the server.</span>
          </div>
          {error && <div role="alert" style={{ marginTop: 12, fontSize: 12.5, color: '#fca5a5' }}>{error}</div>}
        </div>
        <div className="ca-modal-foot" style={{ display: 'flex', justifyContent: 'flex-end', gap: 8, padding: '12px 20px', borderTop: '1px solid var(--border-subtle, rgba(255,255,255,0.08))' }}>
          <button type="button" className="btn ghost" onClick={closeProjectModal}>Cancel</button>
          <button type="submit" className="btn blue" disabled={busy}>{busy ? 'Saving…' : editing ? 'Save folder' : 'Register project'}</button>
        </div>
      </form>
    </div>
  );
};

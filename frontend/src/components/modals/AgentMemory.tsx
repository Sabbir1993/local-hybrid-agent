import React, { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { create } from 'zustand';
import { apiFetch } from '../../services/api';

// Open/close state lives here so the store in useAppStore.ts stays untouched.
interface AgentMemoryUi {
  open: boolean;
  setOpen: (open: boolean) => void;
}
const useAgentMemoryUi = create<AgentMemoryUi>((set) => ({
  open: false,
  setOpen: (open) => set({ open }),
}));

export const openAgentMemory = () => useAgentMemoryUi.getState().setOpen(true);

interface MemoryFile {
  path: string;
  description: string;
  bytes: number;
  version: string;
  updated_at?: number;
  text?: string;
}

interface Editing {
  file: MemoryFile | null; // null = new file
  path: string;
  text: string;
  original?: string;
}

const NEW_TEMPLATE = '---\ndescription: what this file covers and when to read it\n---\n- ';

const memUrl = (path = '') => '/agent/memory' + (path ? '/' + path.split('/').map(encodeURIComponent).join('/') : '');

// Mirrors core/agent_memory.py PATH_RX (at most one folder level).
const PATH_RX = /^[a-z0-9][a-z0-9_-]*(?:\/[a-z0-9][a-z0-9_-]*)?\.md$/;

class HttpError extends Error {
  status: number;
  constructor(message: string, status: number) { super(message); this.status = status; }
}

async function errorOf(r: Response): Promise<string> {
  try {
    const j = await r.json();
    return j.error || j.detail || String(r.status);
  } catch {
    return String(r.status);
  }
}

// The signed-in user's own long-term agent memory (the backend scopes it per user; there is no per-project scope).
export const AgentMemoryModal: React.FC = () => {
  const { open, setOpen } = useAgentMemoryUi();
  const [files, setFiles] = useState<MemoryFile[]>([]);
  const [limits, setLimits] = useState<{ file_max_bytes?: number; max_files?: number }>({});
  const [loading, setLoading] = useState(false);
  const [search, setSearch] = useState('');
  const [error, setError] = useState('');
  const [editing, setEditing] = useState<Editing | null>(null);
  const [saving, setSaving] = useState(false);
  // a pending delete: a single path, or 'all'
  const [confirmDel, setConfirmDel] = useState<string | null>(null);

  const [conflict, setConflict] = useState(false);
  const dialogRef = useRef<HTMLDivElement>(null);
  const downOnBackdrop = useRef(false);
  const listReq = useRef(0);
  const fileReq = useRef(0);
  const openRef = useRef(open);
  openRef.current = open;
  const editingRef = useRef(editing);
  editingRef.current = editing;

  const refresh = useCallback(async () => {
    const id = ++listReq.current;
    setLoading(true);
    try {
      const r = await apiFetch(memUrl());
      if (!r.ok) throw new Error(await errorOf(r));
      const j = await r.json();
      if (id !== listReq.current || !openRef.current) return;
      setFiles(j.files || []);
      setLimits(j.limits || {});
      setError('');
    } catch (e: any) {
      if (id !== listReq.current || !openRef.current) return;
      setError('Could not load memory: ' + (e?.message || e));
    } finally {
      if (id === listReq.current) setLoading(false);
    }
  }, []);

  const isDirty = () => {
    const ed = editingRef.current;
    if (!ed) return false;
    if (!ed.file) return ed.text !== NEW_TEMPLATE || ed.path.trim() !== '';
    return ed.text !== (ed.original ?? ed.file.text ?? '');
  };

  const requestClose = useCallback(() => {
    if (isDirty() && !window.confirm('Discard your unsaved edits?')) return;
    setOpen(false);
  }, [setOpen]);

  useEffect(() => {
    if (!open) { listReq.current++; fileReq.current++; return; }
    const prevFocus = document.activeElement as HTMLElement | null;
    const t = setTimeout(() => {
      const first = dialogRef.current?.querySelector<HTMLElement>('input, textarea, button:not([disabled])');
      first?.focus();
    }, 0);
    return () => { clearTimeout(t); prevFocus?.focus?.(); };
  }, [open]);

  useEffect(() => {
    if (!open) return;
    setEditing(null);
    setConflict(false);
    setConfirmDel(null);
    setSearch('');
    refresh();
  }, [open, refresh]);

  useEffect(() => {
    if (!open) return;
    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape') {
        e.preventDefault();
        e.stopPropagation(); // do not also end a voice conversation
        requestClose();
        return;
      }
      if (e.key === 'Tab' && dialogRef.current) {
        const els = Array.from(dialogRef.current.querySelectorAll<HTMLElement>(
          'a[href], button:not([disabled]), input:not([disabled]), textarea:not([disabled]), [tabindex]:not([tabindex="-1"])'));
        if (!els.length) return;
        const first = els[0], last = els[els.length - 1];
        const act = document.activeElement as HTMLElement | null;
        if (!act || !dialogRef.current.contains(act)) { e.preventDefault(); first.focus(); }
        else if (e.shiftKey && act === first) { e.preventDefault(); last.focus(); }
        else if (!e.shiftKey && act === last) { e.preventDefault(); first.focus(); }
      }
    };
    window.addEventListener('keydown', onKey, true);
    return () => window.removeEventListener('keydown', onKey, true);
  }, [open, requestClose]);

  const shown = useMemo(() => {
    const q = search.trim().toLowerCase();
    if (!q) return files;
    return files.filter((f) => f.path.toLowerCase().includes(q) || (f.description || '').toLowerCase().includes(q));
  }, [files, search]);

  if (!open) return null;

  const openFile = async (path: string) => {
    setError('');
    setConflict(false);
    const id = ++fileReq.current;
    try {
      const r = await apiFetch(memUrl(path));
      if (!r.ok) throw new Error(await errorOf(r));
      const f: MemoryFile = await r.json();
      if (id !== fileReq.current || !openRef.current) return;
      setEditing({ file: f, path: f.path, text: f.text || '', original: f.text || '' });
    } catch (e: any) {
      if (id !== fileReq.current || !openRef.current) return;
      setError('Could not open file: ' + (e?.message || e));
    }
  };

  // After a version conflict: fetch the latest version token but keep what the user typed.
  const reloadLatest = async () => {
    const ed = editing;
    if (!ed?.file) return;
    const id = ++fileReq.current;
    try {
      const r = await apiFetch(memUrl(ed.file.path));
      if (!r.ok) throw new Error(await errorOf(r));
      const f: MemoryFile = await r.json();
      if (id !== fileReq.current || !openRef.current) return;
      setEditing((cur) => (cur && cur.file ? { ...cur, file: f, original: f.text || '' } : cur));
      setConflict(false);
      setError('Latest version loaded. Your text is kept; review it and save again.');
    } catch (e: any) {
      setError('Could not reload: ' + (e?.message || e));
    }
  };

  const save = async () => {
    if (!editing) return;
    const path = editing.file ? editing.file.path : editing.path.trim().toLowerCase();
    if (!path) { setError('Enter a file path'); return; }
    if (!editing.file && (path.includes('..') || !PATH_RX.test(path))) {
      setError('Invalid path. Use a short lowercase name ending in .md, optionally one folder, e.g. preferences.md or projects/my-app.md');
      return;
    }
    setSaving(true);
    setError('');
    setConflict(false);
    try {
      if (!editing.file) {
        // the server upserts: refuse to silently overwrite an existing file
        const ex = await apiFetch(memUrl(path));
        if (ex.ok) { setError(`${path} already exists. Open it from the list to edit it, or pick another name.`); return; }
        if (ex.status !== 404) throw new HttpError(await errorOf(ex), ex.status);
      }
      const r = await apiFetch(memUrl(path), {
        method: 'PUT',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ content: editing.text, if_version: editing.file ? editing.file.version : null }),
      });
      if (!r.ok) throw new HttpError(await errorOf(r), r.status);
      setEditing(null);
      await refresh();
    } catch (e: any) {
      if (e instanceof HttpError && e.status === 409) {
        setConflict(true);
        setError('This file changed since you opened it, so your save was not applied.');
      } else {
        setError('Save failed: ' + (e?.message || e));
      }
    } finally {
      setSaving(false);
    }
  };

  const doDelete = async () => {
    const target = confirmDel;
    if (!target) return;
    setConfirmDel(null);
    setError('');
    try {
      const r = await apiFetch(target === 'all' ? memUrl() : memUrl(target), { method: 'DELETE' });
      if (!r.ok) throw new Error(await errorOf(r));
      setEditing(null);
      await refresh();
    } catch (e: any) {
      setError('Delete failed: ' + (e?.message || e));
    }
  };

  const btn = 'text-xs px-2.5 py-1 rounded border border-border hover:bg-panel text-text-muted hover:text-text transition-colors';

  return (
    <div
      style={{ zIndex: 10005 }} className="fixed inset-0 z-50 flex items-center justify-center bg-black/60 backdrop-blur-sm p-4"
      onMouseDown={(e) => { downOnBackdrop.current = e.target === e.currentTarget; }}
      onClick={(e) => { if (e.target === e.currentTarget && downOnBackdrop.current) requestClose(); downOnBackdrop.current = false; }}
    >
      <div
        ref={dialogRef}
        role="dialog"
        aria-modal="true"
        aria-label="My agent memory"
        className="w-full max-w-3xl bg-panel2 border border-border rounded-2xl shadow-2xl overflow-hidden flex flex-col max-h-[85vh]"
        onClick={(e) => e.stopPropagation()}
      >
        <div className="flex items-center justify-between px-5 py-4 border-b border-border bg-panel">
          <div>
            <h3 className="font-bold text-sm text-text">🧠 My agent memory</h3>
            <p className="text-[11px] text-text-muted mt-0.5">
              What the agent remembers about you, as small files. Only you can see them; they are never used for anyone else.
              Passwords, keys, card and ID numbers are refused.
            </p>
          </div>
          <button
            onClick={requestClose}
            className="w-7 h-7 rounded-lg border border-border flex items-center justify-center text-text-muted hover:text-text hover:bg-panel2 transition-colors"
            aria-label="Close"
          >
            ✕
          </button>
        </div>

        <div className="p-5 overflow-y-auto flex flex-col gap-3 text-xs">
          {error && (
            <div className="px-3 py-2 rounded-lg border border-rose-500/30 bg-rose-500/10 text-rose-400">{error}
              {conflict && editing?.file && (
                <button className="ml-2 underline text-text" onClick={reloadLatest}>Reload latest</button>
              )}
            </div>
          )}

          {editing ? (
            <div className="flex flex-col gap-2">
              {editing.file ? (
                <b className="font-mono text-text">{editing.file.path}</b>
              ) : (
                <input
                  value={editing.path}
                  onChange={(e) => setEditing({ ...editing, path: e.target.value })}
                  placeholder="path, e.g. projects/my-app.md"
                  className="w-full bg-panel border border-border rounded-lg px-2.5 py-1.5 text-xs text-text outline-none focus:border-accent"
                />
              )}
              <textarea
                value={editing.text}
                onChange={(e) => setEditing({ ...editing, text: e.target.value })}
                rows={14}
                className="w-full bg-bg border border-border rounded-lg p-2 font-mono text-[11px] text-text outline-none focus:border-accent"
              />
              {limits.file_max_bytes ? (
                <div className="text-[10px] text-text-muted">
                  {new Blob([editing.text]).size} / {limits.file_max_bytes} bytes
                </div>
              ) : null}
              <div className="flex gap-2">
                <button
                  className="px-4 py-1.5 bg-accent text-white font-semibold text-xs rounded-lg hover:brightness-110 transition-all disabled:opacity-50"
                  disabled={saving}
                  onClick={save}
                >
                  {saving ? 'Saving…' : 'Save'}
                </button>
                <button className={btn} onClick={() => { if (isDirty() && !window.confirm('Discard your unsaved edits?')) return; setEditing(null); setError(''); setConflict(false); }}>Cancel</button>
              </div>
            </div>
          ) : (
            <>
              <div className="flex items-center gap-2 flex-wrap">
                <input
                  type="search"
                  value={search}
                  onChange={(e) => setSearch(e.target.value)}
                  placeholder="Search files by path or description…"
                  className="flex-1 min-w-[180px] bg-panel border border-border rounded-lg px-2.5 py-1.5 text-xs text-text outline-none focus:border-accent"
                />
                <button className={btn} onClick={() => { setEditing({ file: null, path: '', text: NEW_TEMPLATE }); setError(''); }}>
                  ＋ New file
                </button>
                <button
                  className={btn + ' !text-rose-400'}
                  disabled={!files.length}
                  onClick={() => setConfirmDel('all')}
                >
                  Erase all my memory
                </button>
              </div>

              {confirmDel && (
                <div className="px-3 py-2 rounded-lg border border-amber-500/30 bg-amber-500/10 text-text flex items-center justify-between gap-3">
                  <span>
                    {confirmDel === 'all'
                      ? 'Erase everything the agent remembers about you? This cannot be undone.'
                      : <>Delete <code>{confirmDel}</code>?</>}
                  </span>
                  <span className="flex gap-2 flex-shrink-0">
                    <button className={btn + ' !text-rose-400'} onClick={doDelete}>Delete</button>
                    <button className={btn} onClick={() => setConfirmDel(null)}>Cancel</button>
                  </span>
                </div>
              )}

              {loading && !files.length ? (
                <div className="text-text-muted">Loading…</div>
              ) : !files.length ? (
                <div className="text-text-muted">Nothing remembered yet. Tell the agent a preference and it will be saved here.</div>
              ) : !shown.length ? (
                <div className="text-text-muted">No files match the search.</div>
              ) : (
                <div className="flex flex-col divide-y divide-border/40">
                  {shown.map((f) => (
                    <div key={f.path} className="flex items-center gap-3 py-1.5">
                      <code className="min-w-[150px] text-text break-all">{f.path}</code>
                      <span className="flex-1 text-[11px] text-text-muted">{f.description}</span>
                      <span className="text-[10px] text-text-muted whitespace-nowrap">{f.bytes} B</span>
                      <button className={btn} onClick={() => openFile(f.path)}>View / edit</button>
                      <button className={btn + ' !text-rose-400'} onClick={() => setConfirmDel(f.path)}>Delete</button>
                    </div>
                  ))}
                </div>
              )}
              {limits.max_files ? (
                <div className="text-[10px] text-text-muted">{files.length} / {limits.max_files} files</div>
              ) : null}
            </>
          )}
        </div>
      </div>
    </div>
  );
};

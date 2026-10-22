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

// "Add" is a plain form: pick what it is about, type points in your own words. The file name, description and
// "- " bullets are built here, so nobody has to know about paths or the file format.
type Topic = 'about' | 'prefs' | 'lessons' | 'project' | 'other';
const TOPICS: { key: Topic; label: string; hint: string; path?: string; desc?: string }[] = [
  { key: 'about', label: 'About me', hint: 'who you are, your role, your team', path: 'profile.md', desc: 'Who the user is and what they do' },
  { key: 'prefs', label: 'How I like things done', hint: 'tone, language, format, tools you prefer', path: 'preferences.md', desc: 'How the user likes answers and work done' },
  { key: 'lessons', label: 'Lessons and corrections', hint: 'mistakes the agent should not repeat', path: 'lessons.md', desc: 'Corrections and lessons from earlier work' },
  { key: 'project', label: 'A project', hint: 'facts about one project or product' },
  { key: 'other', label: 'Something else', hint: 'any other topic' },
];
const slugOf = (name: string) => name.toLowerCase().replace(/[^a-z0-9]+/g, '-').replace(/^-+|-+$/g, '');
const bulletLines = (notes: string) =>
  notes.split('\n').map((l) => l.trim().replace(/^[-*•]\s*/, '')).filter(Boolean).map((l) => '- ' + l);

interface Adding { topic: Topic; name: string; notes: string }

/** A friendly name for a memory file in the list (the real path stays visible in small print). */
function friendly(path: string): string {
  const t = TOPICS.find((x) => x.path === path);
  if (t) return t.label;
  const m = path.match(/^projects\/(.+)\.md$/);
  const raw = m ? m[1] : path.replace(/\.md$/, '');
  const nice = raw.replace(/[-_]+/g, ' ');
  return (m ? 'Project: ' : '') + nice.charAt(0).toUpperCase() + nice.slice(1);
}

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
  const [adding, setAdding] = useState<Adding | null>(null);
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

  const addingRef = useRef(adding);
  addingRef.current = adding;

  const isDirty = () => {
    if (addingRef.current && addingRef.current.notes.trim()) return true;
    const ed = editingRef.current;
    if (!ed) return false;
    if (!ed.file) return false;
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
    setAdding(null);
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

  const saveAdded = async () => {
    if (!adding) return;
    const topic = TOPICS.find((t) => t.key === adding.topic)!;
    const lines = bulletLines(adding.notes);
    if (!lines.length) { setError('Type at least one thing for the agent to remember.'); return; }
    let path = topic.path || '';
    let desc = topic.desc || '';
    if (!path) {
      const slug = slugOf(adding.name);
      if (!slug) { setError(topic.key === 'project' ? 'Give the project a name.' : 'Give this topic a name.'); return; }
      path = (topic.key === 'project' ? 'projects/' : '') + slug + '.md';
      desc = topic.key === 'project' ? `Notes about the ${adding.name.trim()} project` : `Notes: ${adding.name.trim()}`;
    }
    if (!PATH_RX.test(path)) { setError('Use letters and numbers for the name.'); return; }
    setSaving(true);
    setError('');
    try {
      // an existing note gets the new points added at the end instead of being replaced
      const ex = await apiFetch(memUrl(path));
      let content = `---\ndescription: ${desc}\n---\n${lines.join('\n')}`;
      let version: string | null = null;
      if (ex.ok) {
        const f: MemoryFile = await ex.json();
        content = (f.text || '').replace(/\s*$/, '') + '\n' + lines.join('\n');
        version = f.version;
      } else if (ex.status !== 404) {
        throw new HttpError(await errorOf(ex), ex.status);
      }
      const r = await apiFetch(memUrl(path), {
        method: 'PUT',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ content, if_version: version }),
      });
      if (!r.ok) throw new HttpError(await errorOf(r), r.status);
      setAdding(null);
      await refresh();
    } catch (e: any) {
      setError(e instanceof HttpError && e.status === 409
        ? 'That note changed a moment ago. Press Save again to add your points to the latest version.'
        : 'Save failed: ' + (e?.message || e));
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
              Things the agent keeps in mind about you between chats, so you do not have to repeat yourself.
              Only you can see them. Passwords, keys, card and ID numbers are refused.
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

          {adding ? (
            <div className="flex flex-col gap-3">
              <div className="text-text-muted">The agent reads these notes at the start of a chat and uses them to answer the way you like.</div>
              <div>
                <div className="font-semibold text-text mb-1.5">What is it about?</div>
                <div className="flex flex-wrap gap-1.5">
                  {TOPICS.map((t) => (
                    <button
                      key={t.key}
                      type="button"
                      title={t.hint}
                      onClick={() => setAdding({ ...adding, topic: t.key })}
                      className={btn + (adding.topic === t.key ? ' !border-accent !text-text bg-accent/10' : '')}
                    >
                      {t.label}
                    </button>
                  ))}
                </div>
                <div className="text-[11px] text-text-muted mt-1">{TOPICS.find((t) => t.key === adding.topic)!.hint}</div>
              </div>
              {(adding.topic === 'project' || adding.topic === 'other') && (
                <input
                  value={adding.name}
                  onChange={(e) => setAdding({ ...adding, name: e.target.value })}
                  placeholder={adding.topic === 'project' ? 'Project name, e.g. Payment portal' : 'Topic name, e.g. Travel'}
                  className="w-full bg-panel border border-border rounded-lg px-2.5 py-1.5 text-xs text-text outline-none focus:border-accent"
                />
              )}
              <div>
                <div className="font-semibold text-text mb-1.5">What should the agent remember?</div>
                <textarea
                  value={adding.notes}
                  onChange={(e) => setAdding({ ...adding, notes: e.target.value })}
                  rows={7}
                  placeholder={'One point per line, in your own words. For example:\nI work in the payments team\nKeep answers short and use simple English'}
                  className="w-full bg-bg border border-border rounded-lg p-2 text-xs text-text outline-none focus:border-accent"
                />
                {limits.file_max_bytes ? (
                  <div className="text-[10px] text-text-muted mt-1">{new Blob([adding.notes]).size} / {limits.file_max_bytes} characters in one note</div>
                ) : null}
              </div>
              <div className="flex gap-2">
                <button
                  className="px-4 py-1.5 bg-accent text-white font-semibold text-xs rounded-lg hover:brightness-110 transition-all disabled:opacity-50"
                  disabled={saving}
                  onClick={saveAdded}
                >
                  {saving ? 'Saving…' : 'Save'}
                </button>
                <button className={btn} onClick={() => { if (isDirty() && !window.confirm('Discard what you typed?')) return; setAdding(null); setError(''); }}>Cancel</button>
              </div>
            </div>
          ) : editing ? (
            <div className="flex flex-col gap-2">
              {editing.file ? (
                <div>
                  <b className="text-text">{friendly(editing.file.path)}</b> <span className="font-mono text-[10px] text-text-muted">{editing.file.path}</span>
                  <div className="text-[11px] text-text-muted mt-0.5">One point per line, starting with “- ”. The agent reads this note when it is relevant.</div>
                </div>
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
                  placeholder="Search your notes…"
                  className="flex-1 min-w-[180px] bg-panel border border-border rounded-lg px-2.5 py-1.5 text-xs text-text outline-none focus:border-accent"
                />
                <button className={btn} onClick={() => { setAdding({ topic: 'about', name: '', notes: '' }); setError(''); }}>
                  ＋ Add something to remember
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
                <div className="text-text-muted">Nothing remembered yet. Press “Add something to remember”, or just tell the agent a preference in a chat and it will be saved here.</div>
              ) : !shown.length ? (
                <div className="text-text-muted">No files match the search.</div>
              ) : (
                <div className="flex flex-col divide-y divide-border/40">
                  {shown.map((f) => (
                    <div key={f.path} className="flex items-center gap-3 py-1.5">
                      <div className="min-w-[150px] max-w-[220px]">
                        <div className="text-text font-medium">{friendly(f.path)}</div>
                        <code className="text-[10px] text-text-muted break-all">{f.path}</code>
                      </div>
                      <span className="flex-1 text-[11px] text-text-muted">{f.description}</span>
                      <button className={btn} onClick={() => openFile(f.path)}>Edit</button>
                      <button className={btn + ' !text-rose-400'} onClick={() => setConfirmDel(f.path)}>Delete</button>
                    </div>
                  ))}
                </div>
              )}
              {limits.max_files ? (
                <div className="text-[10px] text-text-muted">{files.length} of {limits.max_files} notes</div>
              ) : null}
            </>
          )}
        </div>
      </div>
    </div>
  );
};

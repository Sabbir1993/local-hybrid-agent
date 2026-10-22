import React, { useCallback, useEffect, useState } from 'react';
import { apiFetch } from '../../services/api';
import { hlCode, hlLangFor } from '../../utils/highlight';
import { useAppStore } from '../../stores/useAppStore';

interface WsNode {
  name: string;
  path: string;
  dir?: boolean;
  size?: number;
  changed?: boolean;
  status?: string;
}

interface WsChange {
  path: string;
  status?: string;
}

interface WsFile {
  path: string;
  content: string;
  changed?: boolean;
  status?: string;
  diff?: Array<{ t: string; s: string }>;
}

function fmtSize(n: number): string {
  if (n < 1024) return `${n} B`;
  if (n < 1024 * 1024) return `${(n / 1024).toFixed(1)} KB`;
  return `${(n / 1024 / 1024).toFixed(1)} MB`;
}

async function loadDir(path: string): Promise<{ root?: string; nodes: WsNode[]; changes: WsChange[]; error?: string }> {
  try {
    const r = await apiFetch('/agent/ws/tree?path=' + encodeURIComponent(path));
    const d = await r.json().catch(() => ({ error: `Could not load project files (server error ${r.status})` }));
    if (!r.ok || d.error || !d.root) return { nodes: [], changes: [], error: d.error || 'No project workspace active' };
    return { root: d.root, nodes: d.nodes || [], changes: d.changes || [] };
  } catch (e: any) {
    return { nodes: [], changes: [], error: e?.message || 'Failed to load files' };
  }
}

interface LevelProps {
  path: string;
  filter: string;
  reloadKey: number;
  onOpenFile: (p: string) => void;
  depth: number;
  onMeta?: (root: string, changes: WsChange[]) => void;
  onNewFolder?: (parent: string) => void;
}

const Dir: React.FC<Omit<LevelProps, 'path' | 'onMeta'> & { node: WsNode }> = ({ node, filter, reloadKey, onOpenFile, depth, onNewFolder }) => {
  const [open, setOpen] = useState(false);
  const [hover, setHover] = useState(false);
  return (
    <>
      <div
        onMouseEnter={() => setHover(true)}
        onMouseLeave={() => setHover(false)}
        className={`ws-node dir ${open ? 'expanded' : ''} ${node.changed ? 'changed' : ''}`}
        style={{ paddingLeft: 4 + depth * 12 }}
        title={node.path}
        onClick={() => setOpen((o) => !o)}
      >
        <span className="caret">▶</span>
        <span>📁</span>
        <span className="nm">{node.name}</span>
        {onNewFolder && (
          <button
            className="btn ghost"
            title="New folder inside this folder"
            style={{ width: 'auto', margin: '0 0 0 auto', padding: '0 6px', fontSize: 11, opacity: hover ? 1 : 0 }}
            onClick={(e) => {
              e.stopPropagation();
              setOpen(true);
              onNewFolder(node.path);
            }}
          >
            + 📁
          </button>
        )}
      </div>
      {open && <Level path={node.path} filter={filter} reloadKey={reloadKey} onOpenFile={onOpenFile} depth={depth + 1} onNewFolder={onNewFolder} />}
    </>
  );
};

const Level: React.FC<LevelProps> = ({ path, filter, reloadKey, onOpenFile, depth, onMeta, onNewFolder }) => {
  const [state, setState] = useState<{ nodes: WsNode[]; error?: string } | null>(null);

  useEffect(() => {
    let alive = true;
    loadDir(path).then((d) => {
      if (!alive) return;
      setState({ nodes: d.nodes, error: d.error });
      if (onMeta && d.root) onMeta(d.root, d.changes);
    });
    return () => {
      alive = false;
    };
  }, [path, reloadKey]);

  if (!state) return <div className="ws-empty" style={{ paddingLeft: 14 + depth * 12 }}>Loading…</div>;
  if (state.error) return <div className="ws-empty">{state.error}</div>;
  if (!state.nodes.length) return <div className="ws-empty" style={{ padding: `6px 0 0 ${14 + depth * 12}px` }}>(empty)</div>;

  // Filtering matches file paths; folders stay so nested matches remain reachable.
  const q = filter.trim().toLowerCase();
  const shown = q ? state.nodes.filter((n) => n.dir || n.path.toLowerCase().includes(q)) : state.nodes;

  return (
    <div className="ws-children">
      {shown.map((n) =>
        n.dir ? (
          <Dir key={n.path} node={n} filter={filter} reloadKey={reloadKey} onOpenFile={onOpenFile} depth={depth} onNewFolder={onNewFolder} />
        ) : (
          <div
            key={n.path}
            className={`ws-node ${n.changed ? 'changed' : ''}`}
            style={{ paddingLeft: 4 + depth * 12 }}
            title={n.path + (n.size != null ? ` · ${fmtSize(n.size)}` : '') + (n.changed ? ` · ${n.status || 'modified'} this session` : '')}
            draggable
            onDragStart={(e) => {
              e.dataTransfer.setData('application/x-agent-file', n.path);
              e.dataTransfer.setData('text/plain', '@' + n.path);
              e.dataTransfer.effectAllowed = 'copy';
            }}
            onClick={() => onOpenFile(n.path)}
          >
            <span className="caret" style={{ visibility: 'hidden' }}>▶</span>
            <span>📄</span>
            <span className="nm">{n.name}</span>
            {n.size != null && <span className="ws-size">{fmtSize(n.size)}</span>}
          </div>
        ),
      )}
    </div>
  );
};

export const WorkspaceTree: React.FC = () => {
  const { activeProjectId, projects, dockTab, isStreaming } = useAppStore();
  const [filter, setFilter] = useState('');
  const [reloadKey, setReloadKey] = useState(0);
  const [root, setRoot] = useState('');
  const [changes, setChanges] = useState<WsChange[]>([]);
  const [changesOpen, setChangesOpen] = useState(false);
  const [file, setFile] = useState<WsFile | null>(null);
  const [fileError, setFileError] = useState('');
  // Folder creation: the server only supports mkdir (via the user's companion); parent is a root-relative path.
  const [mkParent, setMkParent] = useState<string | null>(null);
  const [mkName, setMkName] = useState('');
  const [mkBusy, setMkBusy] = useState(false);
  const [opError, setOpError] = useState('');

  const project = projects.find((p) => p.id === activeProjectId);
  const hasProject = Boolean(project && project.name && project.name !== 'scratch' && project.name !== 'default');

  useEffect(() => {
    setFile(null);   // a file of the previous project must not stay on screen
    setChanges([]);
    setRoot('');
  }, [activeProjectId]);

  useEffect(() => {
    if (dockTab === 'files') setReloadKey((k) => k + 1);
  }, [dockTab, activeProjectId]);

  // the agent may have written files: show the final state when a run ends
  useEffect(() => {
    if (!isStreaming) setReloadKey((k) => k + 1);
  }, [isStreaming]);

  const onMeta = useCallback((r: string, c: WsChange[]) => {
    setRoot(r);
    setChanges(c);
  }, []);

  const startMkdir = (parent: string) => {
    setOpError('');
    setMkName('');
    setMkParent(parent);
  };

  const submitMkdir = async () => {
    const name = mkName.trim();
    if (mkParent === null || !name || mkBusy) return;
    if (name.includes('/') || name.includes('\\') || name === '.' || name === '..') {
      setOpError('Folder name cannot contain slashes or be . / ..');
      return;
    }
    // the tree reports only the folder's name as its root: the project's full path is the real base
    const base = project?.workspace_dir || project?.path || root;
    const sep = base.includes('\\') && !base.includes('/') ? '\\' : '/';
    const parentAbs = mkParent ? base.replace(/[\\/]+$/, '') + sep + mkParent.split('/').join(sep) : base;
    setMkBusy(true);
    setOpError('');
    try {
      const r = await apiFetch('/control/fs/mkdir', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ path: parentAbs, name }),
      });
      const d = await r.json().catch(() => ({ error: `server error ${r.status}` }));
      if (!r.ok || d.ok === false || d.error) {
        setOpError('Create folder failed: ' + (d.error || r.status));
      } else {
        setMkParent(null);
        setReloadKey((k) => k + 1);
      }
    } catch (e: any) {
      setOpError('Create folder failed: ' + (e?.message || 'network error'));
    } finally {
      setMkBusy(false);
    }
  };

  const openFile = async (path: string) => {
    setFileError('');
    try {
      const r = await apiFetch('/agent/ws/file?path=' + encodeURIComponent(path));
      const d = await r.json().catch(() => ({ error: `server error ${r.status}` }));
      if (!r.ok || d.error) {
        setFileError('File view failed: ' + (d.error || r.status));
        return;
      }
      setFile({ path, content: d.content || '', changed: d.changed, status: d.status, diff: d.diff });
    } catch (e: any) {
      setFileError('File view failed: ' + (e?.message || 'network error'));
    }
  };

  if (!hasProject) {
    return (
      <div className="ws-empty" style={{ padding: '32px 16px', textAlign: 'center', color: 'var(--dim)', fontSize: '12.5px' }}>
        <div style={{ fontSize: 26, marginBottom: 8 }}>📁</div>
        <b>No project active</b>
        <p style={{ marginTop: 6, fontSize: '11.5px' }}>Select or create a project to explore workspace files, diffs, and code.</p>
      </div>
    );
  }

  if (file) {
    const lines = file.content ? file.content.replace(/\n$/, '').split('\n').length : 0;
    const marker: Record<string, string> = { '+': 'wsdiff-add', '-': 'wsdiff-del', ' ': 'wsdiff-ctx' };
    const badgeClass = file.changed ? (file.status === 'created' ? 'created' : 'modified') : 'unchanged';
    return (
      <div id="ws-file-view" style={{ display: 'flex' }}>
        <div id="ws-file-head">
          <button
            className="btn ghost"
            id="ws-back"
            style={{ width: 'auto', margin: 0, padding: '3px 10px', fontSize: 11 }}
            title="Back to file tree"
            onClick={() => setFile(null)}
          >
            ← Files
          </button>
          <span id="ws-file-path" className="mono" title={file.path}>{file.path}</span>
          <span id="ws-file-badge" className={badgeClass} title="Session change status">
            {file.changed ? '● ' + (file.status === 'created' ? 'created this session' : 'modified this session') : 'unchanged'}
          </span>
        </div>
        <pre id="ws-file-code">
          {file.changed && file.diff ? (
            file.diff.map((l, i) => (
              <span key={i} className={marker[l.t] || 'wsdiff-ctx'}>
                {(l.t === ' ' ? '  ' : l.t + ' ') + l.s + '\n'}
              </span>
            ))
          ) : (
            <>
              {lines > 0 && (
                <span className="ws-gutter" aria-hidden="true">
                  {Array.from({ length: lines }, (_, i) => i + 1).join('\n')}
                </span>
              )}
              {file.content ? <span dangerouslySetInnerHTML={{ __html: hlCode(file.content, hlLangFor(file.path)) }} /> : '(empty file)'}
            </>
          )}
        </pre>
      </div>
    );
  }

  return (
    <div id="ws-tree-view">
      <div id="ws-root-path" className="mono" title="Workspace root directory on disk" style={{ padding: 8, fontSize: 11, color: 'var(--dim)' }}>
        📁 {root || project?.path || ''}
      </div>
      {changes.length > 0 && (
        <div id="ws-changes" style={{ display: 'block' }}>
          <div className="ws-changes-head" id="ws-changes-toggle" title="Files the agent created or modified this session" onClick={() => setChangesOpen((o) => !o)}>
            <span className="caret" style={{ transform: changesOpen ? 'rotate(90deg)' : undefined }}>▶</span> Session Changes{' '}
            <span id="ws-changes-count" className="mono">{changes.length}</span>
          </div>
          {changesOpen && (
            <div id="ws-changes-list">
              {changes.map((c) => (
                <div key={c.path} className="ws-node changed" onClick={() => openFile(c.path)} title={c.path}>
                  <span className="nm">{c.path}</span>
                </div>
              ))}
            </div>
          )}
        </div>
      )}
      <div id="ws-filter-bar" style={{ display: 'flex', gap: 4, padding: '0 8px 8px' }}>
        <input
          type="text"
          id="ws-filter"
          placeholder="Filter files…"
          title="Filter tree by name"
          value={filter}
          onChange={(e) => setFilter(e.target.value)}
          style={{ flex: 1, padding: '4px 8px', fontSize: 11, background: 'var(--bg)', border: '1px solid var(--border)', borderRadius: 4, color: 'inherit' }}
        />
        <button className="btn ghost" id="ws-refresh" title="Refresh file tree" onClick={() => setReloadKey((k) => k + 1)} style={{ width: 'auto', margin: 0, padding: '3px 8px', fontSize: 11 }}>
          ⟳
        </button>
        <button className="btn ghost" id="ws-new" title="New folder in workspace root" onClick={() => startMkdir('')} style={{ width: 'auto', margin: 0, padding: '3px 8px', fontSize: 11 }}>
          +
        </button>
      </div>
      {mkParent !== null && (
        <div style={{ display: 'flex', gap: 4, padding: '0 8px 8px' }}>
          <input
            type="text"
            autoFocus
            placeholder={`New folder in ${mkParent || 'root'}…`}
            value={mkName}
            disabled={mkBusy}
            onChange={(e) => setMkName(e.target.value)}
            onKeyDown={(e) => {
              if (e.key === 'Enter') submitMkdir();
              else if (e.key === 'Escape') setMkParent(null);
            }}
            style={{ flex: 1, padding: '4px 8px', fontSize: 11, background: 'var(--bg)', border: '1px solid var(--border)', borderRadius: 4, color: 'inherit' }}
          />
          <button className="btn ghost" disabled={mkBusy || !mkName.trim()} onClick={submitMkdir} style={{ width: 'auto', margin: 0, padding: '3px 8px', fontSize: 11 }}>
            Create
          </button>
          <button className="btn ghost" onClick={() => setMkParent(null)} style={{ width: 'auto', margin: 0, padding: '3px 8px', fontSize: 11 }}>
            ✕
          </button>
        </div>
      )}
      {opError && <div className="ws-empty" style={{ padding: '0 8px 6px', color: 'var(--red, #f87171)' }}>{opError}</div>}
      {fileError && <div className="ws-empty" style={{ padding: '0 8px 6px', color: 'var(--red, #f87171)' }}>{fileError}</div>}
      <div id="ws-tree" style={{ padding: 8, fontSize: 12 }}>
        <Level path="" filter={filter} reloadKey={reloadKey} onOpenFile={openFile} depth={0} onMeta={onMeta} onNewFolder={startMkdir} />
      </div>
    </div>
  );
};

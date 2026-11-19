import React, { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { useAppStore } from '../../stores/useAppStore';
import { apiFetch } from '../../services/api';

/**
 * Agent / Code mode sidebar: each agent (or project) of THIS device is a folder, with its own newest chats under it
 * ("See all (N)" loads the rest). Sessions carry no device of their own: a chat is on this device because its agent is
 * bound here, or its project is registered here. One request (/control/session-groups) fills the whole tree.
 */
export interface TreeGroup {
  id: string;
  name: string;
  icon?: string;          // agents show their emoji; projects a folder
  hint?: string;          // tooltip (folder path, description)
}

export interface GroupMenuItem {
  label: string;
  danger?: boolean;
  onClick: () => void;
}

interface TreeSession {
  id: number;
  title: string;
  created_at: number;
  last_at: number;
}

interface Props {
  kind: 'agent' | 'project';
  title: string;
  groups: TreeGroup[];
  activeGroupId: string | null;
  emptyText: React.ReactNode;
  headerActions: React.ReactNode;
  onPickGroup: (g: TreeGroup) => void;           // make this agent / project the active one (no new chat)
  onNewSession: (g: TreeGroup) => void;
  groupMenu: (g: TreeGroup) => GroupMenuItem[];
  onSessionMenu: (e: React.MouseEvent, s: { id: number; title: string }) => void;
  menuOpenFor: string | number | null;
  onOpened?: () => void;                         // after a chat opens (closes the drawer on phones)
}

const PER_GROUP = 5;
const ALL = 500;

/** "now", "5m", "3h", "4d", "2mo", "1y" */
function relAge(ts?: number): string {
  if (!ts) return '';
  const ms = ts < 1e11 ? ts * 1000 : ts;
  const s = Math.max(0, (Date.now() - ms) / 1000);
  if (s < 60) return 'now';
  if (s < 3600) return `${Math.floor(s / 60)}m`;
  if (s < 86400) return `${Math.floor(s / 3600)}h`;
  if (s < 86400 * 30) return `${Math.floor(s / 86400)}d`;
  if (s < 86400 * 365) return `${Math.floor(s / (86400 * 30))}mo`;
  return `${Math.floor(s / (86400 * 365))}y`;
}

const cleanTitle = (kind: string, t: string) =>
  kind === 'agent' ? (t || '').replace(/^[\p{Emoji}\u2000-\u3300]\s*/u, '') : (t || '');

const FolderIcon = () => (
  <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round">
    <path d="M22 19a2 2 0 0 1-2 2H4a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h5l2 3h9a2 2 0 0 1 2 2z" />
  </svg>
);

export const SessionTree: React.FC<Props> = ({
  kind, title, groups, activeGroupId, emptyText, headerActions, onPickGroup, onNewSession, groupMenu,
  onSessionMenu, menuOpenFor, onOpened,
}) => {
  const { sessions, activeSessionId, openSession, runningSessions, sessionFilter } = useAppStore();
  const [data, setData] = useState<Record<string, { total: number; sessions: TreeSession[] }>>({});
  const [expanded, setExpanded] = useState<Record<string, boolean>>({});    // "See all" opened
  const storeKey = `sidebar_tree_collapsed_${kind}`;
  const [collapsed, setCollapsed] = useState<Record<string, boolean>>(() => {
    try { return JSON.parse(localStorage.getItem(storeKey) || '{}') || {}; } catch (_) { return {}; }
  });
  const [groupMenuFor, setGroupMenuFor] = useState<{ id: string; x: number; y: number } | null>(null);
  const menuRef = useRef<HTMLDivElement>(null);

  const ids = useMemo(() => groups.map((g) => g.id).join(','), [groups]);

  const load = useCallback(async (only?: string, per?: number) => {
    const want = only || ids;
    if (!want) { setData({}); return; }
    try {
      const r = await apiFetch(`/control/session-groups?kind=${kind}&ids=${encodeURIComponent(want)}&per=${per || PER_GROUP}`,
        { background: true });
      if (!r.ok) return;
      const d = await r.json();
      setData((prev) => (only ? { ...prev, ...(d.groups || {}) } : (d.groups || {})));
    } catch (_) { /* the tree keeps what it had */ }
  }, [kind, ids]);

  // reload when the groups change, and whenever the store's chat list moves (a chat was created, renamed or deleted)
  useEffect(() => {
    let alive = true;
    (async () => {
      await load();
      if (!alive) return;
      // groups showing "See all" keep their full list
      for (const gid of Object.keys(expanded)) if (expanded[gid] && ids.split(',').includes(gid)) await load(gid, ALL);
    })();
    return () => { alive = false; };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [load, sessions, activeSessionId]);

  useEffect(() => {
    if (!groupMenuFor) return;
    const close = (e: MouseEvent) => { if (menuRef.current && !menuRef.current.contains(e.target as Node)) setGroupMenuFor(null); };
    document.addEventListener('click', close);
    return () => document.removeEventListener('click', close);
  }, [groupMenuFor]);

  const toggle = (gid: string) => {
    setCollapsed((c) => {
      const next = { ...c, [gid]: !c[gid] };
      try { localStorage.setItem(storeKey, JSON.stringify(next)); } catch (_) {}
      return next;
    });
  };

  const q = (sessionFilter || '').trim().toLowerCase();

  return (
    <div className="sidebar-section stree" style={{ flex: 1, minHeight: 0, display: 'flex', flexDirection: 'column' }}>
      <div className="sidebar-section-head">
        <span className="sidebar-section-title stree-title">{title}</span>
        <div className="stree-actions">{headerActions}</div>
      </div>
      <div className="claude-sessions-scroll stree-scroll" id="session-list">
        {groups.length === 0 ? (
          <div className="dim stree-empty">{emptyText}</div>
        ) : groups.map((g) => {
          const gd = data[g.id];
          const list = (gd?.sessions || []).filter((s) => !q || cleanTitle(kind, s.title).toLowerCase().includes(q));
          if (q && list.length === 0 && !g.name.toLowerCase().includes(q)) return null;
          const isOpen = !collapsed[g.id];
          const isActive = String(activeGroupId) === g.id;
          const total = gd?.total || 0;
          return (
            <div key={g.id} className={`stree-group ${isActive ? 'active' : ''}`}>
              <div className="stree-ghead" title={g.hint || g.name} onClick={() => toggle(g.id)}>
                <span className="stree-gicon">
                  {g.icon ? <span className="stree-emoji">{g.icon}</span> : <FolderIcon />}
                </span>
                <span className="stree-gname">{g.name}</span>
                <span className="stree-gbtns">
                  <button type="button" className="stree-ibtn" title="More" aria-label={`${g.name} options`}
                    onClick={(e) => {
                      e.stopPropagation();
                      const rect = (e.currentTarget as HTMLElement).getBoundingClientRect();
                      setGroupMenuFor(groupMenuFor?.id === g.id ? null
                        : { id: g.id, x: Math.min(rect.left, window.innerWidth - 170), y: rect.bottom + 4 });
                    }}>⋮</button>
                  <button type="button" className="stree-ibtn" title={kind === 'agent' ? 'New chat with this agent' : 'New task in this project'}
                    aria-label="New session" onClick={(e) => { e.stopPropagation(); onNewSession(g); onOpened?.(); }}>+</button>
                </span>
              </div>
              {isOpen && (
                <div className="stree-items">
                  {gd && list.length === 0 && !q && <div className="stree-none">No chats yet</div>}
                  {list.map((s) => (
                    <div key={s.id}
                      className={`session-row stree-row ${String(s.id) === String(activeSessionId) ? 'cur' : ''}`}
                      data-sid={String(s.id)}
                      onClick={() => {
                        if (!isActive) onPickGroup(g);
                        openSession(s.id);
                        onOpened?.();
                      }}>
                      <span className="stree-stitle" title={cleanTitle(kind, s.title)}>{cleanTitle(kind, s.title)}</span>
                      {runningSessions[String(s.id)] && (
                        <span title="Still working in the background" aria-label="Running" className="stree-running" />
                      )}
                      <span className="stree-age">{relAge(s.last_at || s.created_at)}</span>
                      <span className="s-menu-wrap" onClick={(e) => onSessionMenu(e, { id: s.id, title: s.title })}>
                        <span className={`s-menu ${menuOpenFor === s.id ? 'open' : ''}`} title="Session options">⋮</span>
                      </span>
                    </div>
                  ))}
                  {!q && total > PER_GROUP && (
                    <div className="stree-more" onClick={() => {
                      const open = !expanded[g.id];
                      setExpanded((x) => ({ ...x, [g.id]: open }));
                      load(g.id, open ? ALL : PER_GROUP);
                    }}>
                      {expanded[g.id] ? 'Show less' : `See all (${total})`}
                    </div>
                  )}
                </div>
              )}
            </div>
          );
        })}
      </div>
      {groupMenuFor && (() => {
        const g = groups.find((x) => x.id === groupMenuFor.id);
        if (!g) return null;
        return (
          <div ref={menuRef} className="session-context-menu stree-menu" style={{ position: 'fixed', left: groupMenuFor.x, top: groupMenuFor.y, zIndex: 9999 }}>
            {groupMenu(g).map((it) => (
              <div key={it.label} className={`stree-menu-item ${it.danger ? 'danger' : ''}`}
                onClick={() => { setGroupMenuFor(null); it.onClick(); }}>{it.label}</div>
            ))}
          </div>
        );
      })()}
    </div>
  );
};

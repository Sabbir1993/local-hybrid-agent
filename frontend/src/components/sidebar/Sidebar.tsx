import React, { useState, useEffect, useRef, useMemo } from 'react';
import { useAppStore, type SessionItem } from '../../stores/useAppStore';
import { DeviceRename } from './DeviceRename';
import { UserMenu } from './UserMenu';
import { SessionTree, type TreeGroup } from './SessionTree';
import { api, apiFetch, getClientDeviceId } from '../../services/api';
import { useMediaQuery, NARROW_QUERY } from '../../hooks/useMediaQuery';
import { env } from '../../config/env';
import { Pin, PinOff, Pencil, Share2, Download, Trash2, AlertTriangle } from 'lucide-react';

export const Sidebar: React.FC = () => {
  const {
    appMode,
    setAppMode,
    sidebarCollapsed,
    sessions,
    hasMoreSessions,
    isLoadingMoreSessions,
    activeSessionId,
    openSession,
    createSession,
    deleteSession,
    sessionFilter,
    setSessionFilter,
    currentUser,
    fetchSessions,
    fetchMoreSessions,
    projects,
    activeProjectId,
    setActiveProjectId,
    fetchProjects,
  } = useAppStore();
  const isNarrow = useMediaQuery(NARROW_QUERY);
  // on a phone / tablet the drawer closes once a chat is picked or started
  const closeIfNarrow = () => { if (isNarrow) useAppStore.setState({ sidebarCollapsed: true }); };

  const isNative = typeof window !== 'undefined' && Boolean(
    (window as any).electronAPI?.isNativeApp ||
    (window as any).isCompanionApp ||
    navigator.userAgent.includes('Electron') ||
    navigator.userAgent.includes('A770NativeApp') ||
    window.location.search.includes('companion=true')
  );

  const isAgentOnCurrentDevice = (agent: any): boolean => {
    if (!agent) return true;
    if (agent.scope === 'template') return false;
    if (!isNative) return false;

    // Bound to a device = that device's own id, compared exactly. (A name check - "Windows PC" on a Windows
    // browser - made every Windows machine look like the owner, so other PCs could open and edit the agent.)
    const agentDevId = agent.device_id ? String(agent.device_id).trim().toLowerCase() : '';
    if (!agentDevId) return true;          // never bound to a device
    return agentDevId === getClientDeviceId().trim().toLowerCase();
  };

  const [customAgents, setCustomAgents] = useState<any[]>([]);
  const [showSearch, setShowSearch] = useState(false);
  const [showUserMenu, setShowUserMenu] = useState(false);
  const [menuState, setMenuState] = useState<{ x: number; y: number; sid: string | number; title: string } | null>(null);
  const [confirmDelete, setConfirmDelete] = useState<{ sid: string | number; title: string } | null>(null);
  const [searchResults, setSearchResults] = useState<SessionItem[] | null>(null);
  const [isSearching, setIsSearching] = useState(false);
  const userMenuPillRef = useRef<HTMLDivElement>(null);
  const sessionMenuRef = useRef<HTMLDivElement>(null);

  const {
    activeCustomAgent,
    setActiveCustomAgent,
    openAgentModal,
    runningSessions,
    openProjectModal,
    agentsVersion,
    bumpAgents,
    showToast,
    openSettingsModal,
    pinSession,
  } = useAppStore();

  useEffect(() => {
    fetchSessions();
  }, [fetchSessions]);

  // Debounced search across content & titles
  useEffect(() => {
    const q = (sessionFilter || '').trim();
    if (!q) {
      setSearchResults(null);
      setIsSearching(false);
      return;
    }
    setIsSearching(true);
    const timer = setTimeout(async () => {
      try {
        const raw = await api.sessions.search(q);
        const mapped: SessionItem[] = raw.map((s: any) => ({
          id: s.id,
          title: s.title,
          updatedAt: s.last_at ? new Date(s.last_at * 1000).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' }) : 'Recent',
          createdAt: s.last_at ? s.last_at * 1000 : (s.created_at ? s.created_at * 1000 : undefined),
          created_at: s.created_at,
          active: s.id === activeSessionId,
          model: s.model || null,
          is_pinned: Boolean(s.is_pinned),
        }));
        setSearchResults(mapped);
      } catch (_) {
        setSearchResults([]);
      } finally {
        setIsSearching(false);
      }
    }, 250);
    return () => clearTimeout(timer);
  }, [sessionFilter, activeSessionId]);

  useEffect(() => {
    apiFetch('/custom-agents')
      .then((r) => r.json())
      .then((data) => {
        const list = Array.isArray(data) ? data : (data.agents || []);
        const mine = list.filter((a: any) => a.scope === 'mine');
        setCustomAgents(mine);
        // If in agent mode and no active agent, auto-pick first or saved
        const savedAgentId = localStorage.getItem('active_custom_agent_id');
        if (savedAgentId) {
          const match = mine.find((a: any) => String(a.id) === String(savedAgentId));
          if (match && (!activeCustomAgent || String(activeCustomAgent.id) !== String(match.id))) {
            setActiveCustomAgent(match);
          }
        } else if (mine.length > 0 && !activeCustomAgent && appMode === 'agent') {
          setActiveCustomAgent(mine[0]);
        }
      })
      .catch(() => {});
  }, [appMode, setActiveCustomAgent, agentsVersion]);

  // the Settings page (iframe) calls this after it saves, forks or deletes an agent
  useEffect(() => {
    (window as any).loadCustomAgents = bumpAgents;
    return () => { delete (window as any).loadCustomAgents; };
  }, [bumpAgents]);

  // Close menus on outside click and keyboard shortcuts (P for Pin, R for Rename)
  useEffect(() => {
    const handleOutsideClick = (e: MouseEvent) => {
      if (userMenuPillRef.current && !userMenuPillRef.current.contains(e.target as Node)) {
        setShowUserMenu(false);
      }
      if (sessionMenuRef.current && !sessionMenuRef.current.contains(e.target as Node)) {
        setMenuState(null);
      }
    };
    const handleKeyDown = (e: KeyboardEvent) => {
      if (!menuState) return;
      if (e.key === 'p' || e.key === 'P') {
        e.preventDefault();
        const activeItem = (sessions || []).find((s) => s.id === menuState.sid) || (searchResults || []).find((s) => s.id === menuState.sid);
        handleTogglePin(menuState.sid, Boolean(activeItem?.is_pinned));
      } else if (e.key === 'r' || e.key === 'R') {
        e.preventDefault();
        handleRename(menuState.sid, menuState.title);
      } else if (e.key === 'Escape') {
        setMenuState(null);
      }
    };
    document.addEventListener('click', handleOutsideClick);
    window.addEventListener('keydown', handleKeyDown);
    return () => {
      document.removeEventListener('click', handleOutsideClick);
      window.removeEventListener('keydown', handleKeyDown);
    };
  }, [menuState, sessions, searchResults]);

  const handleMenuClick = (e: React.MouseEvent, s: any) => {
    e.stopPropagation();
    if (menuState?.sid === s.id) {
      setMenuState(null);
      return;
    }
    const rect = (e.currentTarget as HTMLElement).getBoundingClientRect();
    const width = 170;
    let left = rect.right - width;
    left = Math.max(6, Math.min(left, window.innerWidth - width - 6));
    let top = rect.bottom + 4;
    setMenuState({ x: left, y: top, sid: s.id, title: s.title });
  };

  const handleTogglePin = async (sid: string | number, isPinned: boolean) => {
    setMenuState(null);
    const ok = await pinSession(sid, !isPinned);
    if (ok) {
      showToast(isPinned ? 'Chat unpinned' : 'Chat pinned', 'info');
      fetchSessions();
    } else {
      showToast('Could not update pin status', 'error');
    }
  };

  const handleRename = async (sid: string | number, currentTitle: string) => {
    setMenuState(null);
    const next = window.prompt('Rename chat:', currentTitle);
    if (!next || !next.trim() || next.trim() === currentTitle) return;
    const r = await apiFetch(`/control/sessions/${sid}`, {
      method: 'PATCH',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ title: next.trim() }),
    });
    if (r.ok) {
      fetchSessions();
      showToast('Chat renamed', 'success');
    } else {
      showToast('Rename failed', 'error');
    }
  };

  const handleShare = (sid: string | number) => {
    setMenuState(null);
    const url = `${window.location.origin}/?session=${sid}&share=1`;
    navigator.clipboard.writeText(url).then(
      () => showToast('Shareable link copied to clipboard!', 'success'),
      () => showToast(`Share URL: ${url}`, 'info'),
    );
  };

  const handleExportSession = async (sid: string | number, titleHint: string) => {
    setMenuState(null);
    try {
      const resp = await apiFetch(`/control/sessions/${sid}/messages`);
      const data = await resp.json();
      const msgs = (data.messages || []).filter((m: any) => m.role === 'user' || m.role === 'assistant');
      if (!msgs.length) {
        showToast('Nothing to export in this session', 'info');
        return;
      }
      let md = `# ${titleHint || 'Chat Session'}\n\n`;
      msgs.forEach((m: any) => {
        const role = m.role === 'user' ? 'User' : 'Assistant';
        md += `### ${role}\n\n${m.content || ''}\n\n---\n\n`;
      });
      const blob = new Blob([md], { type: 'text/markdown;charset=utf-8' });
      const url = URL.createObjectURL(blob);
      const a = document.createElement('a');
      a.href = url;
      a.download = `${(titleHint || 'chat').replace(/[^a-zA-Z0-9_-]/g, '_')}.md`;
      document.body.appendChild(a);
      a.click();
      document.body.removeChild(a);
      URL.revokeObjectURL(url);
    } catch (e: any) {
      showToast('Export failed: ' + e.message, 'error');
    }
  };

  const handleDeleteSession = async (sid: string | number) => {
    setMenuState(null);
    await deleteSession(sid);
    showToast('Chat deleted', 'info');
  };

  const handleSessionScroll = (e: React.UIEvent<HTMLDivElement>) => {
    const el = e.currentTarget;
    if (hasMoreSessions && !isLoadingMoreSessions && (el.scrollHeight - el.scrollTop - el.clientHeight <= 60)) {
      fetchMoreSessions();
    }
  };

  const nameToUse = currentUser?.display_name || currentUser?.username || 'Sabbir Hossain';
  const initials = nameToUse
    .split(/\s+/)
    .map((n) => n[0])
    .join('')
    .slice(0, 2)
    .toUpperCase() || 'SH';

  const isSuperAdmin = currentUser?.is_super_admin ?? true;
  const userRoleDisplay = currentUser?.designation
    ? `${currentUser.designation}${currentUser.department ? ` · ${currentUser.department}` : ''}`
    : (isSuperAdmin ? 'SUPER ADMIN' : (currentUser?.roles?.join(', ') || 'USER'));

  const dateGroups = useMemo(() => {
    const now = new Date();
    const todayStart = new Date(now.getFullYear(), now.getMonth(), now.getDate()).getTime();
    const yesterdayStart = todayStart - 86400 * 1000;
    const last7DaysStart = todayStart - 7 * 86400 * 1000;

    const pinned: SessionItem[] = [];
    const today: SessionItem[] = [];
    const yesterday: SessionItem[] = [];
    const previousDays: SessionItem[] = [];
    const older: SessionItem[] = [];

    (sessions || []).forEach((s) => {
      if (s.is_pinned) {
        pinned.push(s);
        return;
      }
      const ts = s.createdAt || (typeof s.created_at === 'number' ? (s.created_at < 1e11 ? s.created_at * 1000 : s.created_at) : 0);
      if (!ts || ts >= todayStart) {
        today.push(s);
      } else if (ts >= yesterdayStart) {
        yesterday.push(s);
      } else if (ts >= last7DaysStart) {
        previousDays.push(s);
      } else {
        older.push(s);
      }
    });

    const groups: { label: string; items: SessionItem[] }[] = [];
    if (pinned.length) groups.push({ label: 'Pinned', items: pinned });
    if (today.length) groups.push({ label: 'Today', items: today });
    if (yesterday.length) groups.push({ label: 'Yesterday', items: yesterday });
    if (previousDays.length) groups.push({ label: 'Previous 7 Days', items: previousDays });
    if (older.length) groups.push({ label: 'Older', items: older });
    return groups;
  }, [sessions]);

  const filterBar = (
    <div className="session-filter-bar" id="session-filter-bar">
      <input
        type="text"
        id="session-filter-input"
        placeholder={appMode === 'chat' ? 'Filter chats…' : 'Filter by chat or name…'}
        value={sessionFilter}
        onChange={(e) => setSessionFilter(e.target.value)}
        autoFocus
      />
      {sessionFilter && (
        <button className="claude-icon-tool" id="session-filter-clear" title="Clear filter" onClick={() => setSessionFilter('')}>
          <svg width="11" height="11" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.5">
            <path d="M18 6 6 18M6 6l12 12" />
          </svg>
        </button>
      )}
    </div>
  );

  // Agent / Code mode show the tree; only agents bound to this device (or to none) and this device's projects
  const treeMode = appMode === 'agent' || appMode === 'code';
  const deviceAgents = customAgents.filter((a) => isAgentOnCurrentDevice(a));
  const agentGroups: TreeGroup[] = deviceAgents.map((a) => ({
    id: String(a.id), name: a.name, icon: a.icon || '🤖',
    hint: `${a.name}${a.work_dir ? `
Folder: ${a.work_dir}` : ''}${a.description ? `
${a.description}` : ''}`,
  }));
  const projectGroups: TreeGroup[] = (projects || [])
    .filter((p: any) => p.is_current_device !== false)
    .map((p: any) => ({ id: String(p.id), name: p.name, hint: `${p.name}
Folder: ${p.workspace_dir || p.path || '(none)'}` }));
  const iconBtn = (title: string, onClick: () => void, path: React.ReactNode, extra: Record<string, any> = {}) => (
    <button type="button" className="claude-icon-tool" title={title} aria-label={title} onClick={onClick} {...extra}>
      <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">{path}</svg>
    </button>
  );
  const treeHeaderActions = (
    <>
      {iconBtn('Filter', () => setShowSearch(!showSearch),
        <path d="M3 6h18M7 12h10M10 18h4" />, { 'aria-expanded': showSearch })}
      {appMode === 'agent' && iconBtn('My Agents: all your agents, on every device', () => openSettingsModal('sec-custom-agents'),
        <><rect x="3" y="7" width="18" height="13" rx="2" /><path d="M8 7V5a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2" /></>)}
      {appMode === 'agent'
        ? iconBtn('New agent', () => openAgentModal('create'), <path d="M12 5v14M5 12h14" />)
        : iconBtn('Add project', () => openProjectModal('create'),
          <><path d="M22 19a2 2 0 0 1-2 2H4a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h5l2 3h9a2 2 0 0 1 2 2z" /><path d="M12 11v6M9 14h6" /></>)}
    </>
  );

  if (sidebarCollapsed) {
    return null;
  }

  return (
    <>
    {/* narrow screens: the sidebar is a drawer over the chat; tapping outside it closes it */}
    {isNarrow && <div className="sidebar-backdrop" aria-hidden="true" onClick={closeIfNarrow} />}
    <aside id="sidebar">
      {/* Brand Header */}
      <div
        className="sidebar-brand-header"
        id="sidebar-brand-header"
        title={`${env.projectName} — Control Center (Click for New Chat)`}
        style={{ cursor: 'pointer' }}
        onClick={() => { createSession(); closeIfNarrow(); }}
      >
        <img src="/static/logo/logo.jpg" alt="Logo" className="sidebar-brand-logo" />
        <span className="sidebar-brand-title">{env.projectName}</span>
      </div>

      {/* Mode Switcher: ONLY in Desktop Companion App (Agent & Code only - NO Chat button) */}
      {isNative && (
        <div className="claude-sidebar-top-block px-3 py-2 border-b border-border/40 flex flex-col items-center justify-center">
          <div
            id="app-mode-switch"
            className="mode-switch full tri-mode-switch"
            title="Switch Mode"
            style={{
              display: 'flex',
              alignItems: 'center',
              justifyContent: 'center',
              width: '100%',
              margin: '0 auto',
            }}
          >
            <button
              type="button"
              id="mode-agent"
              className={`mode-btn ${appMode === 'agent' ? 'active' : ''}`}
              style={{
                flex: 1,
                display: 'inline-flex',
                alignItems: 'center',
                justifyContent: 'center',
                textAlign: 'center',
                gap: '6px',
              }}
              onClick={() => {
                setAppMode('agent');
              }}
              title="Custom Agents (Persona & tools)"
            >
              <svg className="ui-icon" width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2">
                <rect width="16" height="12" x="4" y="8" rx="2" />
                <path d="M12 8V4H8" />
                <path d="M2 14h2" />
                <path d="M20 14h2" />
                <circle cx="9" cy="13" r="1" fill="currentColor" />
                <circle cx="15" cy="13" r="1" fill="currentColor" />
                <path d="M9 17h6" />
              </svg>
              <span>Agent</span>
            </button>
            <button
              type="button"
              id="mode-code"
              className={`mode-btn ${appMode === 'code' ? 'active' : ''}`}
              style={{
                flex: 1,
                display: 'inline-flex',
                alignItems: 'center',
                justifyContent: 'center',
                textAlign: 'center',
                gap: '6px',
              }}
              onClick={() => {
                setAppMode('code');
              }}
              title="Coding Agent (Project workspace & tools)"
            >
              <svg className="ui-icon" width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2">
                <polyline points="16 18 22 12 16 6" />
                <polyline points="8 6 2 12 8 18" />
              </svg>
              <span>Code</span>
            </button>
          </div>
          {appMode === 'code' && (
            <div style={{ width: '100%', marginTop: 6 }}>
              <DeviceRename />
            </div>
          )}
        </div>
      )}

      {/* Agent / Code mode: this device's agents or projects as folders, each with its own chats (SessionTree) */}
      {!isNative && appMode === 'code' && (
        <div style={{ padding: '6px 12px 0' }}>
          <DeviceRename />
        </div>
      )}
      {/* Sessions Section (Tasks for Code, Agent Tasks for Agent, Recent Chats for Web) */}
      <div className="sidebar-section" style={{ flex: 1, minHeight: 0, display: 'flex', flexDirection: 'column' }}>
        {treeMode && showSearch && filterBar}
        {treeMode && (appMode === 'agent' ? (
          <SessionTree
            kind="agent"
            title="My Agents"
            groups={agentGroups}
            activeGroupId={activeCustomAgent ? String(activeCustomAgent.id) : null}
            emptyText={<>No agents on this device yet.<br />Create one with +, or fork one from <a href="#" onClick={(e) => { e.preventDefault(); openSettingsModal('sec-custom-agents'); }}>My Agents</a>.</>}
            headerActions={treeHeaderActions}
            onPickGroup={(g) => { const a = deviceAgents.find((x) => String(x.id) === g.id); if (a) setActiveCustomAgent(a); }}
            onNewSession={(g) => { const a = deviceAgents.find((x) => String(x.id) === g.id); if (a) setActiveCustomAgent(a); createSession(); }}
            groupMenu={(g) => {
              const a = deviceAgents.find((x) => String(x.id) === g.id);
              if (!a) return [];
              return [
                { label: 'Edit agent', onClick: () => openAgentModal('edit', a) },
                { label: 'Fork agent', onClick: () => openAgentModal('fork', a) },
                { label: 'Manage in My Agents', onClick: () => openSettingsModal('sec-custom-agents') },
              ];
            }}
            onSessionMenu={handleMenuClick}
            menuOpenFor={menuState?.sid ?? null}
            onOpened={closeIfNarrow}
          />
        ) : (
          <SessionTree
            kind="project"
            title="Projects"
            groups={projectGroups}
            activeGroupId={activeProjectId ? String(activeProjectId) : null}
            emptyText="No projects on this device yet. Add one with +."
            headerActions={treeHeaderActions}
            onPickGroup={(g) => setActiveProjectId(g.id)}
            onNewSession={(g) => { if (String(activeProjectId) !== g.id) setActiveProjectId(g.id); createSession(); }}
            groupMenu={(g) => {
              const p = projects.find((x) => String(x.id) === g.id);
              if (!p) return [];
              return [
                { label: 'Change folder', onClick: () => openProjectModal('folder', p) },
                { label: 'Unregister project', danger: true, onClick: () => {
                  if (window.confirm(`Unregister project "${p.name}"? Its files stay on disk.`)) {
                    api.projects.delete(p.id).then(() => fetchProjects());
                  }
                } },
              ];
            }}
            onSessionMenu={handleMenuClick}
            menuOpenFor={menuState?.sid ?? null}
            onOpened={closeIfNarrow}
          />
        ))}
        {!treeMode && (<>
        <div className="sidebar-section-head">
          <span className="sidebar-section-title" id="session-title">
            RECENT CHATS
          </span>
          <div style={{ display: 'flex', alignItems: 'center', gap: '2px', marginLeft: 'auto' }}>
            <button
              className="claude-icon-tool"
              id="btn-new-session"
              title="New Chat (+)"
              aria-label="New Session"
              onClick={() => { createSession(); closeIfNarrow(); }}
            >
              <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.5">
                <path d="M12 5v14M5 12h14" />
              </svg>
            </button>
            <button
              className="claude-icon-tool"
              id="btn-sidebar-search"
              title="Filter sessions"
              aria-label="Filter sessions"
              aria-expanded={showSearch}
              onClick={() => setShowSearch(!showSearch)}
            >
              <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.5">
                <circle cx="11" cy="11" r="8" />
                <path d="m21 21-4.3-4.3" />
              </svg>
            </button>
          </div>
        </div>

        {showSearch && filterBar}

        {/* Sessions list with native .session-row classes */}
        <div
          className="claude-sessions-scroll"
          id="session-list"
          onScroll={handleSessionScroll}
          style={{ position: 'relative' }}
        >
          {sessionFilter.trim() ? (
            isSearching ? (
              <div className="dim" style={{ fontSize: '12px', padding: '14px 6px', textAlign: 'center' }}>
                Searching chats…
              </div>
            ) : (searchResults || []).length === 0 ? (
              <div className="dim" style={{ fontSize: '12px', padding: '14px 6px', textAlign: 'center' }}>
                No matching chats
              </div>
            ) : (
              (searchResults || []).map((s) => (
                <div
                  key={String(s.id)}
                  className={`session-row ${s.id === activeSessionId ? 'cur' : ''}`}
                  data-sid={String(s.id)}
                  onClick={() => { openSession(s.id); closeIfNarrow(); }}
                  style={{ position: 'relative' }}
                >
                  <span
                    style={{
                      flex: 1,
                      overflow: 'hidden',
                      textOverflow: 'ellipsis',
                      whiteSpace: 'nowrap',
                      display: 'flex',
                      alignItems: 'center',
                    }}
                    title={s.title}
                  >
                    {s.is_pinned ? (
                      <Pin size={12} style={{ flexShrink: 0, marginRight: '6px', opacity: 0.9, color: 'var(--accent, #818cf8)' }} />
                    ) : (
                      <svg
                        className="session-icon"
                        width="13"
                        height="13"
                        viewBox="0 0 24 24"
                        fill="none"
                        stroke="currentColor"
                        strokeWidth="2"
                        strokeLinecap="round"
                        strokeLinejoin="round"
                        style={{ flexShrink: 0, verticalAlign: '-2px', marginRight: '6px', opacity: 0.85 }}
                      >
                        <path d="M21 15a2 2 0 0 1-2 2H7l-4 4V5a2 2 0 0 1 2-2h14a2 2 0 0 1 2 2z" />
                      </svg>
                    )}
                    <span style={{ overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>
                      {s.title}
                    </span>
                  </span>

                  {runningSessions[String(s.id)] && (
                    <span title="Still working in the background" aria-label="Running" style={{ width: 7, height: 7, borderRadius: '50%', background: 'var(--warm, #ffb454)', marginRight: 6, flexShrink: 0, animation: 'pulse-op 1.6s ease-in-out infinite' }} />
                  )}
                  <span
                    className="s-menu-wrap"
                    onClick={(e) => handleMenuClick(e, s)}
                  >
                    <span className={`s-menu ${menuState?.sid === s.id ? 'open' : ''}`} title="Session options">⋮</span>
                  </span>
                </div>
              ))
            )
          ) : (
            (sessions || []).length === 0 ? (
              <div className="dim" style={{ fontSize: '12px', padding: '14px 6px', textAlign: 'center' }}>
                No chats yet
              </div>
            ) : (
              dateGroups.map((group) => (
                <div key={group.label} className="session-date-group" style={{ marginBottom: 4 }}>
                  <div
                    style={{
                      fontSize: '10px',
                      fontWeight: 600,
                      color: 'var(--dim)',
                      padding: '8px 10px 4px',
                      textTransform: 'uppercase',
                      letterSpacing: '0.05em',
                      opacity: 0.75,
                    }}
                  >
                    {group.label}
                  </div>
                  {group.items.map((s) => (
                    <div
                      key={String(s.id)}
                      className={`session-row ${s.id === activeSessionId ? 'cur' : ''}`}
                      data-sid={String(s.id)}
                      onClick={() => { openSession(s.id); closeIfNarrow(); }}
                      style={{ position: 'relative' }}
                    >
                      <span
                        style={{
                          flex: 1,
                          overflow: 'hidden',
                          textOverflow: 'ellipsis',
                          whiteSpace: 'nowrap',
                          display: 'flex',
                          alignItems: 'center',
                        }}
                        title={s.title}
                      >
                        {s.is_pinned ? (
                          <Pin size={12} style={{ flexShrink: 0, marginRight: '6px', opacity: 0.9, color: 'var(--accent, #818cf8)' }} />
                        ) : (
                          <svg
                            className="session-icon"
                            width="13"
                            height="13"
                            viewBox="0 0 24 24"
                            fill="none"
                            stroke="currentColor"
                            strokeWidth="2"
                            strokeLinecap="round"
                            strokeLinejoin="round"
                            style={{ flexShrink: 0, verticalAlign: '-2px', marginRight: '6px', opacity: 0.85 }}
                          >
                            <path d="M21 15a2 2 0 0 1-2 2H7l-4 4V5a2 2 0 0 1 2-2h14a2 2 0 0 1 2 2z" />
                          </svg>
                        )}
                        <span style={{ overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>
                          {s.title}
                        </span>
                      </span>

                      {runningSessions[String(s.id)] && (
                        <span title="Still working in the background" aria-label="Running" style={{ width: 7, height: 7, borderRadius: '50%', background: 'var(--warm, #ffb454)', marginRight: 6, flexShrink: 0, animation: 'pulse-op 1.6s ease-in-out infinite' }} />
                      )}
                      <span
                        className="s-menu-wrap"
                        onClick={(e) => handleMenuClick(e, s)}
                      >
                        <span className={`s-menu ${menuState?.sid === s.id ? 'open' : ''}`} title="Session options">⋮</span>
                      </span>
                    </div>
                  ))}
                </div>
              ))
            )
          )}
          {isLoadingMoreSessions && (
            <div className="dim" style={{ fontSize: '11px', padding: '10px 6px', textAlign: 'center', opacity: 0.7 }}>
              Loading older sessions…
            </div>
          )}
        </div>

        </>)}

        {/* Floating Session Options Context Menu (Matching Image 6) */}
        {menuState && (() => {
          const currentItem = (sessions || []).find((s) => s.id === menuState.sid) || (searchResults || []).find((s) => s.id === menuState.sid);
          const isPinned = Boolean(currentItem?.is_pinned);
          return (
            <div
              ref={sessionMenuRef}
              className="s-menu-pop"
              style={{
                position: 'fixed',
                left: `${menuState.x}px`,
                top: `${menuState.y}px`,
                zIndex: 9999,
                background: 'var(--panel2, #18181b)',
                border: '1px solid var(--border-subtle, rgba(255, 255, 255, 0.12))',
                borderRadius: '10px',
                padding: '5px',
                boxShadow: '0 12px 30px rgba(0, 0, 0, 0.5)',
                minWidth: '155px',
                display: 'flex',
                flexDirection: 'column',
                animation: 'claudePopIn 0.12s cubic-bezier(0.16, 1, 0.3, 1)',
              }}
              onClick={(e) => e.stopPropagation()}
            >
              <button
                type="button"
                className="s-menu-item-row"
                onClick={() => handleTogglePin(menuState.sid, isPinned)}
              >
                {isPinned ? <PinOff size={14} className="s-icon" /> : <Pin size={14} className="s-icon" />}
                <span className="s-label">{isPinned ? 'Unpin' : 'Pin'}</span>
                <span className="s-shortcut">P</span>
              </button>
              <button
                type="button"
                className="s-menu-item-row"
                onClick={() => handleRename(menuState.sid, menuState.title)}
              >
                <Pencil size={14} className="s-icon" />
                <span className="s-label">Rename</span>
                <span className="s-shortcut">R</span>
              </button>
              <button
                type="button"
                className="s-menu-item-row"
                onClick={() => handleShare(menuState.sid)}
              >
                <Share2 size={14} className="s-icon" />
                <span className="s-label">Share</span>
              </button>
              <button
                type="button"
                className="s-menu-item-row"
                onClick={() => handleExportSession(menuState.sid, menuState.title)}
              >
                <Download size={14} className="s-icon" />
                <span className="s-label">Export .md</span>
              </button>
              <div style={{ height: '1px', background: 'var(--border-subtle, rgba(255, 255, 255, 0.08))', margin: '4px 2px' }} />
              <button
                type="button"
                className="s-menu-item-row danger"
                onClick={() => {
                  const toDel = { sid: menuState.sid, title: menuState.title };
                  setMenuState(null);
                  setConfirmDelete(toDel);
                }}
              >
                <Trash2 size={14} className="s-icon" />
                <span className="s-label">Delete</span>
              </button>
            </div>
          );
        })()}

        {/* Delete Confirmation Modal */}
        {confirmDelete && (
          <div className="ca-modal-overlay" style={{ display: 'flex' }} onClick={() => setConfirmDelete(null)}>
            <div className="ca-modal-box" style={{ maxWidth: 380, padding: '20px 22px' }} onClick={(e) => e.stopPropagation()}>
              <div style={{ display: 'flex', alignItems: 'center', gap: 10, color: '#f87171', marginBottom: 12 }}>
                <AlertTriangle size={20} />
                <h3 style={{ margin: 0, fontSize: 16, fontWeight: 600, color: 'var(--text)' }}>Delete chat?</h3>
              </div>
              <p style={{ fontSize: 13, color: 'var(--dim)', margin: '0 0 20px 0', lineHeight: 1.5 }}>
                Are you sure you want to delete <strong style={{ color: 'var(--text)' }}>"{confirmDelete.title}"</strong>? This will permanently delete this conversation and its history.
              </p>
              <div style={{ display: 'flex', justifyContent: 'flex-end', gap: 10 }}>
                <button
                  type="button"
                  className="btn ghost"
                  onClick={() => setConfirmDelete(null)}
                  style={{ padding: '6px 14px', fontSize: 13 }}
                >
                  Cancel
                </button>
                <button
                  type="button"
                  className="btn danger"
                  style={{ background: '#ef4444', color: '#fff', border: 'none', padding: '6px 16px', borderRadius: 6, fontWeight: 500, cursor: 'pointer', fontSize: 13 }}
                  onClick={() => {
                    const sid = confirmDelete.sid;
                    setConfirmDelete(null);
                    handleDeleteSession(sid);
                  }}
                >
                  Delete
                </button>
              </div>
            </div>
          </div>
        )}
      </div>

      {/* Claude User Profile Pill at Bottom with User Menu */}
      <div
        ref={userMenuPillRef}
        className="claude-profile-pill user-menu"
        id="btn-claude-user-profile"
        title="Account & Preferences"
        style={{ marginTop: 'auto', position: 'relative', cursor: 'pointer' }}
        onClick={(e) => {
          if ((e.target as HTMLElement).closest('.user-menu-dropdown')) return;
          setShowUserMenu(!showUserMenu);
        }}
      >
        <div className="claude-avatar" id="claude-avatar-badge" style={{ overflow: 'hidden' }}>
          {currentUser?.image ? (
            <img src={currentUser.image} alt="Avatar" style={{ width: '100%', height: '100%', objectFit: 'cover', borderRadius: '50%' }} />
          ) : (
            initials
          )}
        </div>
        <div className="claude-profile-info">
          <span className="claude-profile-name" id="claude-user-name">
            {nameToUse}
          </span>
          <span className="claude-profile-sub" id="claude-user-org">
            {userRoleDisplay}
          </span>
        </div>
        <span style={{ fontSize: '10px', color: 'var(--dim)', marginLeft: 'auto' }}>▾</span>

        {/* Floating User Menu Popup */}
        {showUserMenu && (
          <UserMenu
            user={currentUser}
            onClose={() => setShowUserMenu(false)}
          />
        )}
      </div>
    </aside>
    </>
  );
};

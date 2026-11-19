import React, { useState, useEffect, useRef } from 'react';
import { useAppStore } from '../../stores/useAppStore';
import { DeviceRename } from './DeviceRename';
import { UserMenu } from './UserMenu';
import { SessionTree, type TreeGroup } from './SessionTree';
import { api, apiFetch, getClientDeviceId } from '../../services/api';
import { useMediaQuery, NARROW_QUERY } from '../../hooks/useMediaQuery';
import { env } from '../../config/env';

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
  } = useAppStore();

  useEffect(() => {
    fetchSessions();
  }, [fetchSessions]);

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

  // Close menus on outside click
  useEffect(() => {
    const handleOutsideClick = (e: MouseEvent) => {
      if (userMenuPillRef.current && !userMenuPillRef.current.contains(e.target as Node)) {
        setShowUserMenu(false);
      }
      if (sessionMenuRef.current && !sessionMenuRef.current.contains(e.target as Node)) {
        setMenuState(null);
      }
    };
    document.addEventListener('click', handleOutsideClick);
    return () => {
      document.removeEventListener('click', handleOutsideClick);
    };
  }, []);

  const handleMenuClick = (e: React.MouseEvent, s: any) => {
    e.stopPropagation();
    if (menuState?.sid === s.id) {
      setMenuState(null);
      return;
    }
    const rect = (e.currentTarget as HTMLElement).getBoundingClientRect();
    const width = 140;
    let left = rect.right - width;
    left = Math.max(6, Math.min(left, window.innerWidth - width - 6));
    let top = rect.bottom + 4;
    setMenuState({ x: left, y: top, sid: s.id, title: s.title });
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
  };

  const handleSessionScroll = (e: React.UIEvent<HTMLDivElement>) => {
    const el = e.currentTarget;
    if (hasMoreSessions && !isLoadingMoreSessions && (el.scrollHeight - el.scrollTop - el.clientHeight <= 60)) {
      fetchMoreSessions();
    }
  };

  const filteredSessions = (sessions || []).filter((s) => {
    const title = s?.title || '';
    const q = sessionFilter || '';
    return title.toLowerCase().includes(q.toLowerCase());
  });

  const nameToUse = currentUser?.display_name || currentUser?.username || 'Sabbir Hossain';
  const initials = nameToUse
    .split(/\s+/)
    .map((n) => n[0])
    .join('')
    .slice(0, 2)
    .toUpperCase() || 'SH';

  const isSuperAdmin = currentUser?.is_super_admin ?? true;
  const userRoleDisplay = isSuperAdmin ? 'SUPER ADMIN' : (currentUser?.roles?.join(', ') || 'USER');

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
        <div className="claude-sidebar-top-block px-3 py-2 border-b border-border/40 flex items-center justify-center">
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
        </div>
      )}

      {/* Agent / Code mode: this device's agents or projects as folders, each with its own chats (SessionTree) */}
      {appMode === 'code' && (
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
          {filteredSessions.length === 0 ? (
            <div className="dim" style={{ fontSize: '12px', padding: '12px 6px', textAlign: 'center' }}>
              no chats yet
            </div>
          ) : (
            filteredSessions.map((s) => (
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
                  {(
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
          )}
          {isLoadingMoreSessions && (
            <div className="dim" style={{ fontSize: '11px', padding: '10px 6px', textAlign: 'center', opacity: 0.7 }}>
              Loading older sessions…
            </div>
          )}
        </div>

        </>)}

        {/* Floating Session Options Context Menu (Matching Image 1) */}
        {menuState && (
          <div
            ref={sessionMenuRef}
            className="s-menu-pop"
            style={{
              position: 'fixed',
              left: `${menuState.x}px`,
              top: `${menuState.y}px`,
              zIndex: 9999,
              background: 'var(--panel, #1a1917)',
              border: '1px solid var(--border-subtle, rgba(255, 255, 255, 0.12))',
              borderRadius: '8px',
              padding: '4px',
              boxShadow: '0 8px 24px rgba(0, 0, 0, 0.45)',
              minWidth: '140px',
              display: 'flex',
              flexDirection: 'column',
              animation: 'claudePopIn 0.12s cubic-bezier(0.16, 1, 0.3, 1)',
            }}
            onClick={(e) => e.stopPropagation()}
          >
            <button
              type="button"
              onClick={() => handleExportSession(menuState.sid, menuState.title)}
            >
              <span className="mi">📄</span>
              <span>Export .md</span>
            </button>
            <button
              type="button"
              onClick={async () => {
                const sid = menuState.sid;
                const next = window.prompt('Rename chat:', menuState.title);
                setMenuState(null);
                if (!next || !next.trim() || next.trim() === menuState.title) return;
                const r = await apiFetch(`/control/sessions/${sid}`, {
                  method: 'PATCH',
                  headers: { 'Content-Type': 'application/json' },
                  body: JSON.stringify({ title: next.trim() }),
                });
                if (r.ok) fetchSessions();
                else showToast('Rename failed', 'error');
              }}
            >
              <span className="mi">✏️</span>
              <span>Rename</span>
            </button>
            <button
              type="button"
              className="danger"
              onClick={() => handleDeleteSession(menuState.sid)}
            >
              <span className="mi">🗑</span>
              <span>Delete</span>
            </button>
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
        <div className="claude-avatar" id="claude-avatar-badge">{initials}</div>
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

import React, { useState, useEffect, useRef } from 'react';
import { useAppStore } from '../../stores/useAppStore';
import { DeviceRename } from './DeviceRename';
import { UserMenu } from './UserMenu';
import { api, apiFetch } from '../../services/api';

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

    const agentDevId = agent.device_id ? String(agent.device_id).trim().toLowerCase() : '';
    const agentDevName = agent.device_name ? String(agent.device_name).trim().toLowerCase() : '';
    if (!agentDevId && !agentDevName) return true;

    let myDevId = '';
    try {
      if (typeof window !== 'undefined') {
        myDevId = (window as any).electronAPI?.deviceId || localStorage.getItem('antigravity_device_id') || '';
      }
    } catch (_) {}

    if (agentDevId && myDevId && agentDevId === String(myDevId).trim().toLowerCase()) return true;

    if (myDevId) {
      const shortId = myDevId.replace(/^dev_/, '').replace(/_/g, '').slice(0, 12).toLowerCase();
      if (shortId && agentDevName.includes(shortId)) return true;
    }

    const ua = typeof navigator !== 'undefined' ? navigator.userAgent : '';
    if (ua.includes('Windows') && (agentDevName.includes('windows') || agentDevName.startsWith('windows pc'))) {
      return true;
    }
    if ((ua.includes('Macintosh') || ua.includes('Mac OS')) && agentDevName.includes('mac')) {
      return true;
    }
    if (ua.includes('Linux') && agentDevName.includes('linux')) {
      return true;
    }

    return false;
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
    showDialog,
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

  if (sidebarCollapsed) {
    return null;
  }

  return (
    <aside id="sidebar">
      {/* Brand Header */}
      <div
        className="sidebar-brand-header"
        id="sidebar-brand-header"
        title="Local Agent — Control Center (Click for New Chat)"
        style={{ cursor: 'pointer' }}
        onClick={() => createSession()}
      >
        <img src="/static/logo/logo.jpg" alt="Logo" className="sidebar-brand-logo" />
        <span className="sidebar-brand-title">Local Agent</span>
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

      {/* Projects Section (Companion Code Mode) */}
      {appMode === 'code' && (
        <div id="projects-card" className="sidebar-section px-3 py-2 border-b border-border/40">
          <div className="sidebar-section-head flex items-center justify-between pb-1.5">
            <span className="sidebar-section-title text-[11px] font-bold uppercase tracking-wider text-text-muted">
              PROJECTS
            </span>
            <button
              id="btn-newproject"
              className="claude-icon-tool hover:text-accent transition-colors"
              title="Create / Register New Project"
              aria-label="New Project"
              onClick={() => openProjectModal('create')}
            >
              <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.5">
                <path d="M12 5v14M5 12h14" />
              </svg>
            </button>
          </div>
          <div style={{ padding: '0 2px 6px' }}>
            <DeviceRename />
          </div>
          <div id="projects-list" className="claude-projects-scroll flex flex-col gap-0.5 max-h-48 overflow-y-auto pr-0.5">
            {projects.length === 0 ? (
              <div className="text-[11px] text-text-muted py-2 text-center italic">No projects registered</div>
            ) : (
              projects.map((p) => {
                const isSelected = String(p.id) === String(activeProjectId);
                const ws = p.workspace_dir || p.path || '(no folder set)';
                const devLabel = p.device_name || 'Windows PC';
                return (
                  <div
                    key={p.id}
                    className={`project-row ${isSelected ? 'cur' : ''}`}
                    title={`Project: ${p.name}\nDirectory: ${ws}\nDevice: ${devLabel}`}
                    onClick={() => {
                      setActiveProjectId(String(p.id));
                    }}
                  >
                    <div className="proj-icon-wrapper">
                      <svg className="proj-dir-icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round">
                        <path d="M22 19a2 2 0 0 1-2 2H4a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h5l2 3h9a2 2 0 0 1 2 2z"></path>
                      </svg>
                    </div>
                    <div className="proj-meta">
                      <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', gap: '4px' }}>
                        <div className="proj-name">{p.name}</div>
                        <span className="proj-dev-badge this-dev" title={`Device: ${devLabel}`}>
                          {devLabel}
                        </span>
                      </div>
                      <div className="proj-path" title={ws}>{ws}</div>
                    </div>
                    <span
                      className="p-del"
                      style={{ marginRight: 6 }}
                      title="Change folder"
                      onClick={(e) => {
                        e.stopPropagation();
                        openProjectModal('folder', p);
                      }}
                    >
                      ✏️
                    </span>
                    <span
                      className="p-del"
                      title="Remove project registration (files preserved)"
                      onClick={(e) => {
                        e.stopPropagation();
                        if (window.confirm(`Unregister project "${p.name}"?`)) {
                          api.projects.delete(p.id).then(() => fetchProjects());
                        }
                      }}
                    >
                      ✕
                    </span>
                  </div>
                );
              })
            )}
          </div>
        </div>
      )}

      {/* Custom Agents Section (Companion Agent Mode) */}
      {appMode === 'agent' && (
        <div id="custom-agents-card" className="sidebar-section px-3 py-1.5 border-b border-border/40">
          <div className="sidebar-section-head flex items-center justify-between pb-1">
            <span className="sidebar-section-title text-[11px] font-bold uppercase tracking-wider text-text-muted">
              My Agents
            </span>
            <button
              id="btn-new-custom-agent"
              className="claude-icon-tool hover:text-accent transition-colors"
              title="Create New Custom Agent (+)"
              aria-label="New Custom Agent"
              onClick={() => openAgentModal('create')}
            >
              <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.5">
                <path d="M12 5v14M5 12h14" />
              </svg>
            </button>
          </div>
          <div id="custom-agents-sidebar-list" className="claude-projects-scroll flex flex-col gap-0.5 max-h-48 overflow-y-auto pr-0.5">
            {customAgents.length === 0 ? (
              <div className="text-[11px] text-text-muted py-2 text-center italic">No personal agents configured</div>
            ) : (
              customAgents.map((a) => {
                const isSelected = String(activeCustomAgent?.id) === String(a.id);
                const isCurDev = isAgentOnCurrentDevice(a);
                const devLabel = a.device_name || (isCurDev ? 'Windows PC' : 'Other PC');
                const folderName = a.work_dir ? a.work_dir.split(/[/\\]/).pop() || a.work_dir : '';
                return (
                  <div
                    key={a.id}
                    className={`sidebar-agent-row ${isSelected ? 'selected' : ''} ${!isCurDev ? 'opacity-80' : ''}`}
                    title={`${a.name}: ${a.description || ''}${!isCurDev ? ' (Bound to ' + devLabel + '. Fork to use on this machine)' : ''}`}
                    onClick={() => {
                      if (!isNative) {
                        showToast('Companion native desktop app is required to use this feature.', 'warn');
                        return;
                      }
                      if (!isCurDev) {
                        showDialog({
                          title: 'Agent Machine Binding',
                          message: `Agent "${a.name}" is bound to "${devLabel}". Please Fork it to bind a copy to this machine.`,
                          confirmLabel: 'Fork Agent',
                          cancelLabel: 'Cancel',
                          onConfirm: () => {
                            openAgentModal('fork', a);
                          },
                        });
                        return;
                      }
                      setActiveCustomAgent(a);
                      createSession();
                    }}
                  >
                    <span className="sidebar-agent-icon" style={{ fontSize: '15px' }}>{a.icon || '🤖'}</span>
                    <div className="sidebar-agent-info" style={{ flex: 1, minWidth: 0 }}>
                      <div className="sidebar-agent-device-tag" style={{ fontSize: '9px', color: isCurDev ? 'var(--accent, #38bdf8)' : 'var(--amber, #f59e0b)', fontWeight: 700, display: 'flex', alignItems: 'center', gap: '4px', marginBottom: '1px', lineHeight: 1.2 }}>
                        <span>{isCurDev ? '💻' : '🔒'}</span>
                        <span style={{ overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>{devLabel}</span>
                        {!isCurDev && (
                          <span style={{ fontSize: '7.5px', background: 'rgba(245,158,11,0.18)', color: 'var(--amber, #f59e0b)', padding: '1px 4px', borderRadius: '3px', marginLeft: 'auto', textTransform: 'uppercase', fontWeight: 700 }}>Fork only</span>
                        )}
                      </div>
                      <div className="sidebar-agent-name" style={{ fontWeight: 600, fontSize: '11.5px', color: 'var(--text)' }}>{a.name}</div>
                      <div className="sidebar-agent-desc" style={{ fontSize: '9.5px', color: 'var(--dim)', overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>
                        {folderName && <span style={{ background: 'var(--panel)', padding: '1px 4px', borderRadius: '3px', border: '1px solid var(--border)', marginRight: '4px' }}>📁 {folderName}</span>}
                        {a.description || a.preferred_lane || ''}
                      </div>
                    </div>
                    <div className="sidebar-agent-actions" style={{ display: 'flex', gap: '2px', alignItems: 'center' }}>
                      <button
                        type="button"
                        className="btn ghost btn-icon-xs edit-agent-btn"
                        title={isCurDev ? 'Edit Agent' : `Bound to ${devLabel}. Only that PC can edit it - fork it to get your own copy.`}
                        disabled={!isCurDev}
                        style={!isCurDev ? { opacity: 0.4, cursor: 'not-allowed' } : undefined}
                        onClick={(e) => {
                          e.stopPropagation();
                          if (!isCurDev) return;
                          openAgentModal('edit', a);
                        }}
                      >
                        ✏️
                      </button>
                      <button
                        type="button"
                        className="btn ghost btn-icon-xs fork-agent-btn"
                        title={isCurDev ? 'Fork Agent' : 'Fork Agent to use on this PC'}
                        onClick={(e) => {
                          e.stopPropagation();
                          openAgentModal('fork', a);
                        }}
                      >
                        🍴
                      </button>
                    </div>
                  </div>
                );
              })
            )}
          </div>
        </div>
      )}

      {/* Sessions Section (Tasks for Code, Agent Tasks for Agent, Recent Chats for Web) */}
      <div className="sidebar-section" style={{ flex: 1, minHeight: 0, display: 'flex', flexDirection: 'column' }}>
        <div className="sidebar-section-head">
          <span className="sidebar-section-title" id="session-title">
            {appMode === 'code' ? 'PROJECT TASKS' : appMode === 'agent' ? 'AGENT TASKS' : 'RECENT CHATS'}
          </span>
          <div style={{ display: 'flex', alignItems: 'center', gap: '2px', marginLeft: 'auto' }}>
            <button
              className="claude-icon-tool"
              id="btn-new-session"
              title={appMode === 'code' ? 'New Project Task (+)' : appMode === 'agent' ? 'New Agent Task (+)' : 'New Chat (+)'}
              aria-label="New Session"
              onClick={() => createSession()}
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

        {/* Filter search bar */}
        {showSearch && (
          <div className="session-filter-bar" id="session-filter-bar">
            <input
              type="text"
              id="session-filter-input"
              placeholder="Filter tasks…"
              value={sessionFilter}
              onChange={(e) => setSessionFilter(e.target.value)}
              autoFocus
            />
            {sessionFilter && (
              <button
                className="claude-icon-tool"
                id="session-filter-clear"
                title="Clear filter"
                onClick={() => setSessionFilter('')}
              >
                <svg width="11" height="11" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.5">
                  <path d="M18 6 6 18M6 6l12 12" />
                </svg>
              </button>
            )}
          </div>
        )}

        {/* Sessions list with native .session-row classes */}
        <div
          className="claude-sessions-scroll"
          id="session-list"
          onScroll={handleSessionScroll}
          style={{ position: 'relative' }}
        >
          {filteredSessions.length === 0 ? (
            <div className="dim" style={{ fontSize: '12px', padding: '12px 6px', textAlign: 'center' }}>
              no tasks yet
            </div>
          ) : (
            filteredSessions.map((s) => (
              <div
                key={String(s.id)}
                className={`session-row ${s.id === activeSessionId ? 'cur' : ''}`}
                data-sid={String(s.id)}
                onClick={() => openSession(s.id)}
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
                  {appMode === 'code' ? (
                    <svg
                      className="session-icon"
                      width="13"
                      height="13"
                      viewBox="0 0 24 24"
                      fill="none"
                      stroke="currentColor"
                      strokeWidth="2.2"
                      strokeLinecap="round"
                      strokeLinejoin="round"
                      style={{ flexShrink: 0, verticalAlign: '-1px', marginRight: '7px', opacity: 0.85 }}
                    >
                      <polyline points="16 18 22 12 16 6" />
                      <polyline points="8 6 2 12 8 18" />
                    </svg>
                  ) : appMode === 'agent' ? (
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
                      style={{ flexShrink: 0, verticalAlign: '-1px', marginRight: '7px', opacity: 0.85 }}
                    >
                      <rect width="16" height="12" x="4" y="8" rx="2" />
                      <path d="M12 8V4H8" />
                      <path d="M2 14h2" />
                      <path d="M20 14h2" />
                      <circle cx="9" cy="13" r="1" fill="currentColor" />
                      <circle cx="15" cy="13" r="1" fill="currentColor" />
                      <path d="M9 17h6" />
                    </svg>
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
                    {appMode === 'agent' ? (s.title || '').replace(/^[\p{Emoji}\u2000-\u3300]\s*/u, '') : s.title}
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
  );
};

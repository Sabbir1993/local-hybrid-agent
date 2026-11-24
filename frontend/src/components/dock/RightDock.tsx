import React, { useState, useEffect, useRef } from 'react';
import { Code2, Globe, ListChecks, ListTodo, Terminal, Users } from 'lucide-react';
import { useAppStore, type DockTab } from '../../stores/useAppStore';
import { useMediaQuery } from '../../hooks/useMediaQuery';
import { agentWsParam } from '../../utils/wsTarget';
import { PlanView } from './PlanView';
import { TasksView } from './TasksView';
import { SubagentsView } from './SubagentsView';
import { TerminalPanel } from './TerminalPanel';
import { WorkspaceTree } from './WorkspaceTree';

export const RightDock: React.FC = () => {
  const {
    appMode,
    dockCollapsed,
    dockExpanded,
    dockTab,
    setDockTab,
    setDockCollapsed,
    setDockExpanded,
    previewModal,
    activeSubagents,
  } = useAppStore();

  const hasRunningSubagents = Object.values(activeSubagents || {}).some((s) => s.status === 'running');
  const subagentsCount = Object.keys(activeSubagents || {}).length;

  // Preview tab states
  const [previewText, setPreviewText] = useState<string | null>(null);
  type BrowserTab = { id: number; url: string; input: string; title: string; reloadKey: number };
  const [browserTabs, setBrowserTabs] = useState<BrowserTab[]>([{ id: 1, url: '', input: '', title: 'New tab', reloadKey: 0 }]);
  const [activeBrowserTab, setActiveBrowserTab] = useState(1);
  const webviewRefs = useRef<Record<number, any>>({});
  const tabSeq = useRef(1);

  const isLocalPreviewUrl = (u: string) => /^https?:\/\/(127\.0\.0\.1|localhost|\[::1\])([: /]|$)/i.test(u);
  const activeTab = browserTabs.find((t) => t.id === activeBrowserTab) || browserTabs[0];
  const browserUrl = activeTab?.url || '';
  const browserInput = activeTab?.input || '';

  const patchTab = (id: number, patch: Partial<BrowserTab>) =>
    setBrowserTabs((tabs) => tabs.map((t) => t.id === id ? { ...t, ...patch } : t));

  const navigateTab = (raw: string, id = activeBrowserTab) => {
    let u = raw.trim();
    if (!u) return;
    if (!/^[a-z][a-z0-9+.-]*:\/\//i.test(u)) u = 'http://' + u;
    if (/^https?:\/\//i.test(u) || /^\/(agent|assets|static)\//i.test(u)) {
      let title = u;
      try { title = new URL(u, window.location.origin).hostname || u; } catch (_) {}
      patchTab(id, { url: u, input: u, title });
    }
  };

  const addBrowserTab = () => {
    const id = ++tabSeq.current;
    setBrowserTabs((tabs) => [...tabs, { id, url: '', input: '', title: 'New tab', reloadKey: 0 }]);
    setActiveBrowserTab(id);
  };

  const closeBrowserTab = (id: number) => {
    setBrowserTabs((tabs) => {
      const next = tabs.filter((t) => t.id !== id);
      if (!next.length) {
        const fresh = { id: ++tabSeq.current, url: '', input: '', title: 'New tab', reloadKey: 0 };
        setActiveBrowserTab(fresh.id);
        return [fresh];
      }
      if (id === activeBrowserTab) setActiveBrowserTab(next[Math.max(0, tabs.findIndex((t) => t.id === id) - 1)].id);
      return next;
    });
  };

  const browserBack = () => {
    const wv = webviewRefs.current[activeBrowserTab];
    if (wv && typeof wv.goBack === 'function') { wv.goBack(); return; }
    try { (document.getElementById('prev-dock-iframe') as HTMLIFrameElement | null)?.contentWindow?.history.back(); } catch (_) {}
  };
  const browserForward = () => {
    const wv = webviewRefs.current[activeBrowserTab];
    if (wv && typeof wv.goForward === 'function') { wv.goForward(); return; }
    try { (document.getElementById('prev-dock-iframe') as HTMLIFrameElement | null)?.contentWindow?.history.forward(); } catch (_) {}
  };
  const browserReload = () => {
    const wv = webviewRefs.current[activeBrowserTab];
    if (wv && typeof wv.reload === 'function') wv.reload();
    patchTab(activeBrowserTab, { reloadKey: (activeTab?.reloadKey || 0) + 1 });
  };

  // Resize right dock width state & drag handler
  const [dockWidth, setDockWidth] = useState<number>(() => {
    try {
      const saved = localStorage.getItem('claude_dock_w');
      if (saved) {
        const val = parseInt(saved, 10);
        if (val >= 300 && val <= window.innerWidth * 0.85) return val;
      }
    } catch (_) {}
    return 480;
  });
  const [isDragging, setIsDragging] = useState(false);

  useEffect(() => {
    if (!isDragging) return;

    const onMove = (e: MouseEvent) => {
      const newW = window.innerWidth - e.clientX;
      if (newW >= 300 && newW <= window.innerWidth * 0.85) {
        setDockWidth(newW);
      }
    };

    const onUp = () => {
      setIsDragging(false);
      document.body.classList.remove('ws-resizing');
      const grip = document.getElementById('dock-grip');
      if (grip) grip.classList.remove('dragging');
      try {
        localStorage.setItem('claude_dock_w', String(dockWidth));
      } catch (_) {}
    };

    window.addEventListener('mousemove', onMove);
    window.addEventListener('mouseup', onUp);
    return () => {
      window.removeEventListener('mousemove', onMove);
      window.removeEventListener('mouseup', onUp);
    };
  }, [isDragging, dockWidth]);

  const handleGripMouseDown = (e: React.MouseEvent) => {
    if (e.button !== 0) return;
    e.preventDefault();
    setIsDragging(true);
    document.body.classList.add('ws-resizing');
    const grip = document.getElementById('dock-grip');
    if (grip) grip.classList.add('dragging');
  };

  const filePath = previewModal?.filePath || '';
  const ext = filePath.split('.').pop()?.toLowerCase() || 'html';
  const urlExt = (() => {
    if (!filePath.startsWith('http')) return ext;
    try { return new URL(filePath).pathname.split('.').pop()?.toLowerCase() || ''; } catch (_) { return ext; }
  })();
  const IMAGE_EXT = ['png', 'jpg', 'jpeg', 'webp', 'gif', 'svg', 'ico', 'bmp', 'avif'];
  // a web address without a file extension is most often a picture too (CDN links); anything with another extension is not
  const isImage = IMAGE_EXT.includes(urlExt) || filePath.startsWith('data:image/') ||
    (filePath.startsWith('http') && !/\.[a-z0-9]{2,5}$/i.test(urlExt ? '.' + urlExt : ''));
  const isHtml = ext === 'html' || ext === 'htm';

  const enc = encodeURIComponent(filePath);
  const isDirectUrl = filePath.startsWith('http://') || filePath.startsWith('https://') || filePath.startsWith('data:') || filePath.startsWith('blob:');
  const rawUrl = isDirectUrl
    ? filePath
    : previewModal?.source === 'ws'
      ? `/agent/ws/raw?path=${enc}${agentWsParam()}`
      : `/agent/raw?path=${enc}`;

  useEffect(() => {
    if (filePath && rawUrl) navigateTab(rawUrl);
  }, [filePath, rawUrl]);

  useEffect(() => {
    if (!filePath && !previewModal?.directContent) {
      setPreviewText(null);
      return;
    }

    if (previewModal?.directContent != null) {
      setPreviewText(previewModal.directContent);
      return;
    }

    if (!isImage && filePath && !isDirectUrl) {
      // a slower earlier answer must not replace the file that is shown now
      let current = true;
      setPreviewText(null);
      fetch(rawUrl)
        .then((r) => (r.ok ? r.text() : ''))
        .then((txt) => { if (current) setPreviewText(txt); })
        .catch(() => { if (current) setPreviewText(''); });
      return () => { current = false; };
    }
    setPreviewText(null);
  }, [filePath, previewModal?.directContent, rawUrl, isImage, isDirectUrl]);

  // Mode rule matching claude-dock.js syncTabVisibility():
  // In chat mode without active custom agent, hide Code & Terminal tabs.
  const showCodeAndTerminal = appMode === 'code' || appMode === 'agent';

  const handleTabClick = (tab: DockTab) => {
    if (!dockCollapsed && dockTab === tab) {
      setDockCollapsed(true);
    } else {
      setDockTab(tab);
      setDockCollapsed(false);
    }
  };

  const isNarrow = useMediaQuery('(max-width: 1180px)');

  return (
    <>
      {isNarrow && !dockCollapsed && (
        <div
          className="dock-backdrop"
          aria-hidden="true"
          onClick={() => setDockCollapsed(true)}
        />
      )}
      <div
        id="right-dock"
        className={`right-dock ${dockCollapsed ? 'dock-collapsed' : ''} ${dockExpanded ? 'dock-expanded' : ''}`}
        style={{
          width: isNarrow ? undefined : (dockExpanded ? undefined : `${dockWidth}px`),
          maxWidth: isNarrow ? undefined : '85vw',
          minWidth: isNarrow ? undefined : '300px',
        }}
      >
      <div
        className={`dock-grip ${isDragging ? 'dragging' : ''}`}
        id="dock-grip"
        title="Drag to resize pane"
        onMouseDown={handleGripMouseDown}
      />

      {/* Dock Header */}
      <div className="dock-header">
        <div className="dock-header-tools">
          <button
            className={`dock-tab-btn icon-only ${dockTab === 'plan' ? 'active' : ''}`}
            id="dock-btn-plan"
            aria-label="Plan"
            title="Plan — the agent's current plan and execution steps"
            onClick={() => handleTabClick('plan')}
          >
            <ListChecks size={16} aria-hidden="true" />
          </button>

          <button
            className={`dock-tab-btn icon-only ${dockTab === 'tasks' ? 'active' : ''}`}
            id="dock-btn-tasks"
            aria-label="Tasks"
            title="Tasks — background tasks, their status and controls to stop them"
            onClick={() => handleTabClick('tasks')}
          >
            <ListTodo size={16} aria-hidden="true" />
          </button>

          {showCodeAndTerminal && (
            <button
              className={`dock-tab-btn icon-only ${dockTab === 'subagents' ? 'active' : ''}`}
              id="dock-btn-subagents"
              aria-label="Subagents"
              title="Subagents — live view of the child agents working on this task"
              onClick={() => handleTabClick('subagents')}
            >
              <Users size={16} aria-hidden="true" />
              {hasRunningSubagents ? (
                <span className="dock-tab-dot" aria-hidden="true" />
              ) : subagentsCount > 0 ? (
                <span className="dock-tab-count">{subagentsCount}</span>
              ) : null}
            </button>
          )}

          {showCodeAndTerminal && (
            <button
              className={`dock-tab-btn icon-only ${dockTab === 'files' ? 'active' : ''}`}
              id="dock-btn-files"
              aria-label="Code"
              title="Code — workspace files and the code inspector"
              onClick={() => handleTabClick('files')}
            >
              <Code2 size={16} aria-hidden="true" />
            </button>
          )}

          {showCodeAndTerminal && (
            <button
              className={`dock-tab-btn icon-only ${dockTab === 'preview' ? 'active' : ''}`}
              id="dock-btn-preview"
              aria-label="Preview"
              title="Preview — rendered web pages and artifacts"
              onClick={() => handleTabClick('preview')}
            >
              <Globe size={16} aria-hidden="true" />
            </button>
          )}

          {showCodeAndTerminal && (
            <button
              className={`dock-tab-btn icon-only ${dockTab === 'terminal' ? 'active' : ''}`}
              id="dock-btn-terminal"
              aria-label="Terminal"
              title="Terminal — the companion shell on your device"
              onClick={() => handleTabClick('terminal')}
            >
              <Terminal size={16} aria-hidden="true" />
            </button>
          )}
        </div>

        <div style={{ display: 'flex', alignItems: 'center', gap: '4px' }}>
          <button
            className="dock-icon-btn"
            id="dock-btn-expand"
            title={dockExpanded ? 'Restore Width' : 'Toggle expand width'}
            onClick={() => setDockExpanded(!dockExpanded)}
          >
            ⤢
          </button>
          <button
            className="dock-icon-btn"
            id="dock-btn-close"
            title="Close panel"
            onClick={() => setDockCollapsed(true)}
          >
            ✕
          </button>
        </div>
      </div>

      {/* Dock View: Plan */}
      <div className={`dock-view-body ${dockTab === 'plan' ? 'active' : ''}`} id="dock-view-plan">
        <PlanView />
      </div>

      {/* Dock View: Background tasks (polls only while this tab is open) */}
      <div className={`dock-view-body ${dockTab === 'tasks' ? 'active' : ''}`} id="dock-view-tasks">
        {dockTab === 'tasks' && !dockCollapsed && <TasksView />}
      </div>

      {/* Dock View: Sub-Agents (Interactive Application View) */}
      <div className={`dock-view-body ${dockTab === 'subagents' ? 'active' : ''}`} id="dock-view-subagents" style={{ display: dockTab === 'subagents' ? 'flex' : 'none', flexDirection: 'column', height: '100%', overflow: 'hidden' }}>
        <SubagentsView />
      </div>

      {/* Dock View: Files */}
      <div className={`dock-view-body ${dockTab === 'files' ? 'active' : ''}`} id="dock-view-files">
        <div id="ws-panel" className="dock-ws-inner">
          {dockTab === 'files' && !dockCollapsed && <WorkspaceTree />}
        </div>
      </div>

      {/* Dock View: Preview (Interactive side tab matching claude-dock.js) */}
      <div className={`dock-view-body ${dockTab === 'preview' ? 'active' : ''}`} id="dock-view-preview" style={{ display: dockTab === 'preview' ? 'flex' : 'none', flexDirection: 'column', height: '100%', overflow: 'hidden' }}>
        <div className="pv-tabstrip">
          {browserTabs.map((tab) => (
            <div key={tab.id} className={`pv-tab${tab.id === activeBrowserTab ? ' active' : ''}`}
                 onClick={() => setActiveBrowserTab(tab.id)} title={tab.url || 'New tab'}>
              <span className="pv-tab-title">{tab.title || 'New tab'}</span>
              <span className="pv-tab-close" title="Close tab" onClick={(e) => { e.stopPropagation(); closeBrowserTab(tab.id); }}>×</span>
            </div>
          ))}
          <button type="button" className="pv-tab-add" title="New tab" onClick={addBrowserTab}>+</button>
        </div>
        <div className="pv-browser-bar">
          <button type="button" className="pv-nav-btn" title="Back" onClick={browserBack}>←</button>
          <button type="button" className="pv-nav-btn" title="Forward" onClick={browserForward}>→</button>
          <button type="button" className="pv-nav-btn" title="Reload" onClick={browserReload}>⟳</button>
          <input
            className="pv-address"
            aria-label="Address"
            spellCheck={false}
            placeholder="Search or enter a URL"
            value={browserInput}
            onChange={(e) => patchTab(activeBrowserTab, { input: e.target.value })}
            onKeyDown={(e) => { if (e.key === 'Enter') navigateTab(browserInput); }}
          />
          <a className="pv-nav-btn" title="Open in system browser" target="_blank" rel="noopener noreferrer"
             href={browserUrl || 'about:blank'}>↗</a>
        </div>
        {filePath || previewModal?.directContent || browserUrl ? (
          <>
            <div style={{ flex: 1, minHeight: 0, position: 'relative', display: 'flex', flexDirection: 'column', overflow: 'hidden' }}>
              {browserUrl && !isLocalPreviewUrl(browserUrl) ? (
                React.createElement('webview' as any, {
                  key: `webview-${activeTab.id}`,
                  ref: (el: any) => { webviewRefs.current[activeTab.id] = el; },
                  src: browserUrl,
                  allowpopups: 'false',
                  nodeintegration: 'false',
                  style: { width: '100%', height: '100%', border: 0, background: '#ffffff' },
                  title: 'In-dock browser',
                })
              ) : isImage ? (
                <div style={{ flex: 1, display: 'flex', alignItems: 'center', justifyContent: 'center', background: '#0a0a0c', padding: '16px', overflow: 'auto' }}>
                  <img
                    src={rawUrl}
                    alt={previewModal.title || filePath}
                    key={`${activeTab.id}-${activeTab.reloadKey}`}
                    style={{ maxWidth: '100%', maxHeight: '100%', objectFit: 'contain', borderRadius: '8px', boxShadow: '0 4px 20px rgba(0,0,0,0.5)' }}
                  />
                </div>
              ) : isHtml ? (
                <div
                  className="preview-iframe-wrapper"
                  id="prev-frame-wrap"
                  style={{ flex: 1, width: '100%', height: '100%' }}
                >
                  <iframe
                    id="prev-dock-iframe"
                    key={`${activeTab.id}-${activeTab.reloadKey}`}
                    className="preview-live-iframe"
                    sandbox="allow-scripts allow-forms allow-popups allow-modals"
                    src={previewModal.directContent ? undefined : (browserUrl || rawUrl)}
                    srcDoc={previewModal.directContent || undefined}
                    style={{
                      width: '100%',
                      height: '100%',
                      border: 'none',
                      background: '#ffffff',
                      pointerEvents: isDragging ? 'none' : 'auto',
                    }}
                  />
                </div>
              ) : (
                <pre
                  className="preview-code-view"
                  style={{ flex: 1, margin: 0, overflow: 'auto' }}
                >
                  {previewText || previewModal.directContent || ''}
                </pre>
              )}
            </div>
          </>
        ) : (
          <div className="pv-empty">
            <div className="pv-empty-mark" aria-hidden="true">
              <svg width="26" height="26" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round" strokeLinejoin="round">
                <circle cx="12" cy="12" r="9" /><path d="M3 12h18M12 3c2.6 2.7 3.9 5.7 3.9 9S14.6 18.3 12 21c-2.6-2.7-3.9-5.7-3.9-9S9.4 5.7 12 3Z" />
              </svg>
            </div>
            <h2>Web Preview</h2>
            <p>Enter a URL above, or preview a generated file to see it here.</p>
          </div>
        )}
      </div>

      {/* Dock View: Terminal */}
      <div className={`dock-view-body ${dockTab === 'terminal' ? 'active' : ''}`} id="dock-view-terminal">
        <TerminalPanel />
      </div>
    </div>
    </>
  );
};

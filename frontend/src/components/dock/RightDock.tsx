import React, { useState, useEffect } from 'react';
import { useAppStore, type DockTab } from '../../stores/useAppStore';
import { PlanView } from './PlanView';
import { TasksView } from './TasksView';
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
  } = useAppStore();

  // Preview tab states
  const [previewMode, setPreviewMode] = useState<'preview' | 'code'>('preview');
  const [isMobileView, setIsMobileView] = useState(false);
  const [previewText, setPreviewText] = useState<string | null>(null);
  const [reloadKey, setReloadKey] = useState(0);

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
      ? `/agent/ws/raw?path=${enc}`
      : `/agent/raw?path=${enc}`;

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
  }, [filePath, previewModal?.directContent, rawUrl, isImage, isDirectUrl, reloadKey]);

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

  return (
    <div
      id="right-dock"
      className={`right-dock ${dockCollapsed ? 'dock-collapsed' : ''} ${dockExpanded ? 'dock-expanded' : ''}`}
      style={{
        width: dockExpanded ? undefined : `${dockWidth}px`,
        maxWidth: '85vw',
        minWidth: '300px',
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
            className={`dock-tab-btn ${dockTab === 'plan' ? 'active' : ''}`}
            id="dock-btn-plan"
            title="Plan & Execution"
            onClick={() => handleTabClick('plan')}
          >
            📋 Plan
          </button>

          <button
            className={`dock-tab-btn ${dockTab === 'tasks' ? 'active' : ''}`}
            id="dock-btn-tasks"
            title="Background tasks: what is running, and stop"
            onClick={() => handleTabClick('tasks')}
          >
            🗂 Tasks
          </button>

          {showCodeAndTerminal && (
            <button
              className={`dock-tab-btn ${dockTab === 'files' ? 'active' : ''}`}
              id="dock-btn-files"
              title="Code & Workspace Files (Ctrl+Shift+F)"
              onClick={() => handleTabClick('files')}
            >
              &lt;/&gt; Code
            </button>
          )}

          <button
            className={`dock-tab-btn ${dockTab === 'preview' ? 'active' : ''}`}
            id="dock-btn-preview"
            title="Web Preview (HTML / Artifacts)"
            onClick={() => handleTabClick('preview')}
          >
            🌐 Preview
          </button>

          {showCodeAndTerminal && (
            <button
              className={`dock-tab-btn ${dockTab === 'terminal' ? 'active' : ''}`}
              id="dock-btn-terminal"
              title="Companion Shell Terminal"
              onClick={() => handleTabClick('terminal')}
            >
              &gt;_ Terminal
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

      {/* Dock View: Files */}
      <div className={`dock-view-body ${dockTab === 'files' ? 'active' : ''}`} id="dock-view-files">
        <div id="ws-panel" className="dock-ws-inner">
          {dockTab === 'files' && !dockCollapsed && <WorkspaceTree />}
        </div>
      </div>

      {/* Dock View: Preview (Interactive side tab matching claude-dock.js) */}
      <div className={`dock-view-body ${dockTab === 'preview' ? 'active' : ''}`} id="dock-view-preview" style={{ display: dockTab === 'preview' ? 'flex' : 'none', flexDirection: 'column', height: '100%', overflow: 'hidden' }}>
        {filePath || previewModal?.directContent ? (
          <>
            <div className="preview-tab-controls">
              <div style={{ display: 'flex', alignItems: 'center', gap: '8px', minWidth: 0 }}>
                <b style={{ overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap', maxWidth: '140px' }} title={previewModal.title || filePath}>
                  {previewModal.title || filePath}
                </b>
                <span className="chip mono" style={{ fontSize: '10px', padding: '1px 6px', background: 'var(--panel)', border: '1px solid var(--border)', borderRadius: '4px' }}>
                  .{ext}
                </span>
              </div>
              <div style={{ display: 'flex', alignItems: 'center', gap: '6px' }}>
                {(isHtml || !isImage) && (
                  <div className="preview-toggle-btns">
                    <button
                      type="button"
                      className={`preview-toggle-btn ${previewMode === 'preview' ? 'active' : ''}`}
                      id="btn-prev-tab-view"
                      onClick={() => setPreviewMode('preview')}
                    >
                      Preview
                    </button>
                    <button
                      type="button"
                      className={`preview-toggle-btn ${previewMode === 'code' ? 'active' : ''}`}
                      id="btn-prev-tab-code"
                      onClick={() => setPreviewMode('code')}
                    >
                      Code
                    </button>
                  </div>
                )}
                {isHtml && (
                  <button
                    type="button"
                    className="dock-icon-btn"
                    id="btn-prev-mobile"
                    title="Toggle Mobile / Desktop view"
                    onClick={() => setIsMobileView(!isMobileView)}
                    style={{ background: 'none', border: 'none', cursor: 'pointer', padding: '2px 4px' }}
                  >
                    📱
                  </button>
                )}
                <button
                  type="button"
                  className="dock-icon-btn"
                  id="btn-prev-reload"
                  title="Reload preview"
                  onClick={() => setReloadKey((k) => k + 1)}
                  style={{ background: 'none', border: 'none', cursor: 'pointer', padding: '2px 4px' }}
                >
                  ⟳
                </button>
                {rawUrl && (
                  <a
                    href={rawUrl}
                    target="_blank"
                    rel="noopener noreferrer"
                    className="dock-icon-btn"
                    title="Open in new tab"
                    style={{ textDecoration: 'none', background: 'none', border: 'none', cursor: 'pointer', padding: '2px 4px' }}
                  >
                    ↗
                  </a>
                )}
              </div>
            </div>

            <div style={{ flex: 1, minHeight: 0, position: 'relative', display: 'flex', flexDirection: 'column', overflow: 'hidden' }}>
              {isImage ? (
                <div style={{ flex: 1, display: 'flex', alignItems: 'center', justifyContent: 'center', background: '#0a0a0c', padding: '16px', overflow: 'auto' }}>
                  <img
                    src={rawUrl}
                    alt={previewModal.title || filePath}
                    key={reloadKey}
                    style={{ maxWidth: '100%', maxHeight: '100%', objectFit: 'contain', borderRadius: '8px', boxShadow: '0 4px 20px rgba(0,0,0,0.5)' }}
                  />
                </div>
              ) : isHtml ? (
                <>
                  <div
                    className={`preview-iframe-wrapper ${isMobileView ? 'mobile-frame' : ''}`}
                    id="prev-frame-wrap"
                    style={{ display: previewMode === 'preview' ? 'flex' : 'none', flex: 1, width: '100%', height: '100%' }}
                  >
                    <iframe
                      id="prev-dock-iframe"
                      key={reloadKey}
                      className="preview-live-iframe"
                      sandbox="allow-scripts allow-forms allow-popups allow-modals"
                      src={previewModal.directContent ? undefined : rawUrl}
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
                  <pre
                    className="preview-code-view"
                    id="prev-code-wrap"
                    style={{ display: previewMode === 'code' ? 'block' : 'none', flex: 1, margin: 0, overflow: 'auto' }}
                  >
                    {previewText || previewModal.directContent || ''}
                  </pre>
                </>
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
          <div style={{ padding: '32px 16px', textAlign: 'center', color: 'var(--dim)', fontSize: '12.5px' }}>
            <div style={{ fontSize: '24px', marginBottom: '8px' }}>🌐</div>
            <b>Web Preview</b>
            <p style={{ marginTop: '6px', fontSize: '11.5px' }}>
              Click 👁️ Preview on any file, image, or generated HTML snippet to view it here.
            </p>
          </div>
        )}
      </div>

      {/* Dock View: Terminal */}
      <div className={`dock-view-body ${dockTab === 'terminal' ? 'active' : ''}`} id="dock-view-terminal">
        <TerminalPanel />
      </div>
    </div>
  );
};

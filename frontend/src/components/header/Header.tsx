import React from 'react';
import { useAppStore } from '../../stores/useAppStore';

export const Header: React.FC = () => {
  const { 
    toggleSidebar, 
    toggleDock, 
    activeModal,
    setActiveModal,
    hasPerm,
  } = useAppStore();

  const handleToolClick = (modal: 'customize' | 'report' | 'audit' | 'db' | 'monitor') => {
    setActiveModal(activeModal === modal ? 'none' : modal);
  };

  return (
    <header className="select-none">
      <button
        id="btn-sidebar-toggle"
        className="btn ghost icon-btn icon-only"
        title="Toggle Sidebar (Ctrl+B)"
        aria-label="Toggle Sidebar"
        onClick={toggleSidebar}
      >
        <svg className="ui-icon" width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
          <rect width="18" height="18" x="3" y="3" rx="2" />
          <path d="M9 3v18" />
        </svg>
      </button>

      <div className="spacer" />

      {/* Dev & System Tools Cluster (Customize, Report, Audit, DB, Monitor) */}
      <div className="header-tool-cluster" id="header-dev-cluster">
        <button
          id="btn-customize"
          className={`btn ghost icon-btn icon-only ${activeModal === 'customize' ? 'active' : ''}`}
          title="Customize: browse and install skills, connectors and plugins"
          aria-label="Customize"
          onClick={() => handleToolClick('customize')}
        >
          <svg className="ui-icon" width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
            <rect x="3" y="3" width="7" height="7" rx="1" />
            <rect x="14" y="3" width="7" height="7" rx="1" />
            <rect x="3" y="14" width="7" height="7" rx="1" />
            <path d="M17.5 14v7M14 17.5h7" />
          </svg>
        </button>

        {hasPerm('usage.report.view') && (
        <button
          id="btn-report"
          className={`btn ghost icon-btn icon-only ${activeModal === 'report' ? 'active' : ''}`}
          data-perm="usage.report.view"
          title="Token usage report: prompts vs completions, per-model and per-day totals"
          aria-label="Report"
          onClick={() => handleToolClick('report')}
        >
          <svg className="ui-icon" width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
            <path d="M3 3v18h18" />
            <path d="M18 17V9" />
            <path d="M13 17V5" />
            <path d="M8 17v-3" />
          </svg>
        </button>
        )}

        {hasPerm('audit.view') && (
        <button
          id="btn-audit"
          className={`btn ghost icon-btn icon-only ${activeModal === 'audit' ? 'active' : ''}`}
          data-perm="audit.view"
          title="Audit log: who did what — model loads, provider changes, user/role edits, permission denials"
          aria-label="Audit"
          onClick={() => handleToolClick('audit')}
        >
          <svg className="ui-icon" width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
            <path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z" />
            <path d="M14 2v6h6" />
            <path d="m9 15 2 2 4-4" />
          </svg>
        </button>
        )}

        {hasPerm('database.manage') && (
        <button
          id="btn-db"
          className={`btn ghost icon-btn icon-only ${activeModal === 'db' ? 'active' : ''}`}
          data-perm="database.manage"
          title="Database Explorer & SQL Query Console: inspect tables, schema, and execute SQL"
          aria-label="Database"
          onClick={() => handleToolClick('db')}
        >
          <svg className="ui-icon" width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
            <ellipse cx="12" cy="5" rx="9" ry="3" />
            <path d="M3 5v14c0 1.66 4.03 3 9 3s9-1.34 9-3V5" />
            <path d="M3 12c0 1.66 4.03 3 9 3s9-1.34 9-3" />
          </svg>
        </button>
        )}

        {hasPerm('monitor.view') && (
        <button
          id="btn-monitor"
          className={`btn ghost icon-btn icon-only ${activeModal === 'monitor' ? 'active' : ''}`}
          data-perm="monitor.view"
          title="Real-time request monitor: live generations, prompt/gen speed, endpoint calls"
          aria-label="Monitor"
          onClick={() => handleToolClick('monitor')}
        >
          <svg className="ui-icon" width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
            <path d="M22 12h-4l-3 9L9 3l-3 9H2" />
          </svg>
        </button>
        )}
      </div>

      <div className="header-divider" />

      {/* Action Tools Cluster: Toggle Right Dock Panel */}
      <div className="header-actions-cluster">
        <button
          id="btn-dock-toggle"
          className="btn ghost icon-btn icon-only"
          title="Toggle Right Panel (Ctrl+Shift+B)"
          aria-label="Toggle Right Panel"
          onClick={() => toggleDock()}
        >
          <svg className="ui-icon" width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
            <rect width="18" height="18" x="3" y="3" rx="2" />
            <path d="M15 3v18" />
          </svg>
        </button>
      </div>
    </header>
  );
};

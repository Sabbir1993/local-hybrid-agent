import React, { useEffect, useRef, useState } from 'react';
import { useAppStore } from '../../stores/useAppStore';
import { openAgentMemory } from './AgentMemory';

export const SettingsModal: React.FC = () => {
  const { activeModal, setActiveModal, settingsTab } = useAppStore();
  const isOpen = activeModal === 'settings';
  const frameRef = useRef<HTMLIFrameElement>(null);
  const [reloadKey, setReloadKey] = useState(0);

  useEffect(() => {
    if (!isOpen) return;
    if (window.location.pathname !== '/settings' && !window.location.hash.includes('settings') && !window.location.hash.startsWith('#sec-')) {
      try {
        const curTab = settingsTab || (typeof localStorage !== 'undefined' ? localStorage.getItem('a770_active_settings_tab') : '') || 'sec-theme';
        const target = curTab.startsWith('sec-') ? curTab : `sec-${curTab}`;
        window.history.pushState({ modal: 'settings' }, '', `/settings#${target}`);
      } catch (_) {}
    }
  }, [isOpen, settingsTab]);

  // Sync settingsTab to the iframe if changed externally (e.g. browser back/forward)
  useEffect(() => {
    if (!isOpen || !frameRef.current?.contentWindow || !settingsTab) return;
    try {
      frameRef.current.contentWindow.postMessage({ type: 'navigate-tab', tab: settingsTab }, '*');
    } catch (_) {}
  }, [settingsTab, isOpen]);

  // the Settings page (an iframe) asks for the memory editor, and syncs tab routing
  useEffect(() => {
    if (!isOpen) return;
    const onMsg = (e: MessageEvent) => {
      if (e.data && typeof e.data === 'object') {
        if (e.data.type === 'open-agent-memory' && useAppStore.getState().hasPerm('memory.use')) {
          openAgentMemory();
        }
        if (e.data.type === 'settings-tab-change' && e.data.tab) {
          useAppStore.getState().setSettingsTab(e.data.tab);
          try {
            window.history.replaceState(null, '', `/settings#${e.data.tab}`);
          } catch (_) {}
        }
        if (e.data.type === 'profile-updated') {
          void useAppStore.getState().checkAuth();
        }
      }
    };
    window.addEventListener('message', onMsg);
    return () => window.removeEventListener('message', onMsg);
  }, [isOpen]);

  if (!isOpen) return null;

  let initialTab = '';
  if (typeof window !== 'undefined') {
    const h = window.location.hash.replace(/^#/, '');
    if (h && (h.startsWith('sec-') || h.includes('/'))) {
      initialTab = h;
    }
  }
  const activeTabRaw = initialTab || settingsTab || (typeof localStorage !== 'undefined' ? localStorage.getItem('a770_active_settings_tab') : '') || 'sec-theme';
  const targetTab = activeTabRaw.startsWith('sec-') ? activeTabRaw : `sec-${activeTabRaw}`;
  // frame=1 makes Vite proxy this to the backend's standalone settings.html,
  // avoiding a recursive load of the React app inside the modal.
  const iframeUrl = `/settings?frame=1#${targetTab}`;

  const handleBackToChat = () => {
    if (window.location.pathname === '/settings' || window.location.hash.includes('settings') || window.location.hash.startsWith('#sec-')) {
      try {
        window.history.pushState({}, '', '/');
      } catch (_) {}
    }
    setActiveModal('none');
  };

  return (
    <div
      id="settings-page"
      className="fixed inset-0 z-50 flex flex-col w-screen h-screen bg-bg select-none"
      style={{ width: '100vw', height: '100vh', margin: 0, padding: 0 }}
    >
      {/* Top Header Bar with Back to Chat */}
      <div
        className="flex items-center border-b border-border bg-panel2 select-none flex-shrink-0 w-full"
        style={{ height: '48px', minHeight: '48px', maxHeight: '48px' }}
      >
        {/* Left section aligned with sidebar on desktop, responsive on mobile */}
        <div
          className="flex items-center px-2 sm:px-4 border-r border-border flex-shrink-0 w-auto sm:w-[240px] sm:min-w-[240px] sm:max-w-[240px] h-full"
        >
          <button
            className="btn ghost flex items-center justify-center gap-1.5 text-xs font-semibold px-2.5 sm:px-3 py-1.5 rounded-lg border border-border hover:bg-panel text-text transition-colors whitespace-nowrap w-full"
            onClick={handleBackToChat}
            title="Return to active chat"
          >
            <span>←</span>
            <span className="hidden sm:inline">Back to Chat</span>
            <span className="sm:hidden">Back</span>
          </button>
        </div>

        {/* Menu title after left sidebar divider */}
        <div className="flex items-center px-3 sm:px-5 flex-1 min-w-0 justify-between gap-2 sm:gap-4">
          <span className="font-bold text-xs sm:text-xs tracking-wider flex items-center gap-1.5 sm:gap-2 text-text truncate">
            ⚙️ Settings &amp; Configuration
          </span>
          <button
            type="button"
            className="btn ghost icon-btn icon-only flex-shrink-0"
            title="Refresh settings"
            aria-label="Refresh settings"
            onClick={() => setReloadKey((k) => k + 1)}
          >
            ⟳
          </button>
        </div>
      </div>

      {/* Fullscreen Dedicated Embedded View */}
      <div className="flex-1 w-full h-full bg-bg overflow-hidden">
        <iframe
          id="settings-frame"
          key={reloadKey}
          ref={frameRef}
          src={iframeUrl}
          title="Settings"
          className="w-full h-full border-none bg-bg"
          style={{ width: '100%', height: '100%', border: 'none', background: 'var(--bg)' }}
        />
      </div>
    </div>
  );
};

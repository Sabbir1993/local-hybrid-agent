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
    if (window.location.pathname !== '/settings' && !window.location.hash.includes('settings')) {
      try {
        window.history.pushState({ modal: 'settings' }, '', '/settings');
      } catch (_) {}
    }
  }, [isOpen]);

  // the Settings page (an iframe) asks for the memory editor, which is part of this app
  useEffect(() => {
    if (!isOpen) return;
    const onMsg = (e: MessageEvent) => {
      // the store's hasPerm already lets a super admin through
      if (e.origin === window.location.origin && e.data && e.data.type === 'open-agent-memory'
          && useAppStore.getState().hasPerm('memory.use')) openAgentMemory();
    };
    window.addEventListener('message', onMsg);
    return () => window.removeEventListener('message', onMsg);
  }, [isOpen]);

  if (!isOpen) return null;

  const targetTab = settingsTab ? (settingsTab.startsWith('sec-') ? settingsTab : `sec-${settingsTab}`) : 'sec-theme';
  // frame=1 makes Vite proxy this to the backend's standalone settings.html,
  // avoiding a recursive load of the React app inside the modal.
  const iframeUrl = `/settings?frame=1#${targetTab}`;

  const handleBackToChat = () => {
    if (window.location.pathname === '/settings' || window.location.hash.includes('settings')) {
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

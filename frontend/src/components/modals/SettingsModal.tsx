import React, { useEffect } from 'react';
import { useAppStore } from '../../stores/useAppStore';
import { openAgentMemory } from './AgentMemory';

export const SettingsModal: React.FC = () => {
  const { activeModal, setActiveModal, settingsTab } = useAppStore();
  const isOpen = activeModal === 'settings';

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
      if (e.origin === window.location.origin && e.data && e.data.type === 'open-agent-memory') openAgentMemory();
    };
    window.addEventListener('message', onMsg);
    return () => window.removeEventListener('message', onMsg);
  }, [isOpen]);

  if (!isOpen) return null;

  const targetTab = settingsTab ? (settingsTab.startsWith('sec-') ? settingsTab : `sec-${settingsTab}`) : 'sec-theme';
  const iframeUrl = `/settings#${targetTab}`;

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
        className="flex items-center border-b border-border bg-panel2 select-none flex-shrink-0"
        style={{ height: '48px', minHeight: '48px', maxHeight: '48px' }}
      >
        {/* Left section aligned with sidebar (240px) */}
        <div
          className="flex items-center px-4 border-r border-border flex-shrink-0"
          style={{ width: '240px', minWidth: '240px', maxWidth: '240px', height: '100%' }}
        >
          <button
            className="btn ghost flex items-center justify-center gap-1.5 text-xs font-semibold px-3 py-1.5 rounded-lg border border-border hover:bg-panel text-text transition-colors whitespace-nowrap w-full"
            onClick={handleBackToChat}
            title="Return to active chat"
          >
            ← Back to Chat
          </button>
        </div>

        {/* Menu title after left sidebar divider */}
        <div className="flex items-center px-5 flex-1 min-w-0">
          <span className="font-bold text-xs tracking-wider flex items-center gap-2 text-text whitespace-nowrap flex-shrink-0">
            ⚙️ Settings &amp; Configuration
          </span>
        </div>
      </div>

      {/* Fullscreen Dedicated Embedded View */}
      <div className="flex-1 w-full h-full bg-bg overflow-hidden">
        <iframe
          id="settings-frame"
          src={iframeUrl}
          title="Settings"
          className="w-full h-full border-none bg-bg"
          style={{ width: '100%', height: '100%', border: 'none', background: 'var(--bg)' }}
        />
      </div>
    </div>
  );
};

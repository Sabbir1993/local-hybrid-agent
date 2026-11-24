import React from 'react';
import { useAppStore } from '../../stores/useAppStore';
import { type UserProfile } from '../../services/api';

interface UserMenuProps {
  user: UserProfile | null;
  onClose: () => void;
}

/** Account actions that are not duplicated in Settings (Profile / Theme / Password live there). */
export const UserMenu: React.FC<UserMenuProps> = ({ onClose }) => {
  const { openSettingsModal, setActiveModal, logout } = useAppStore();

  const handleAction = (e: React.MouseEvent, action: () => void) => {
    e.stopPropagation();
    action();
    onClose();
  };

  return (
    <div
      className="user-menu-dropdown open shadow-2xl"
      style={{
        position: 'absolute', bottom: '100%', left: '0', right: '0', marginBottom: '8px',
        zIndex: 9999, background: 'var(--panel2)', border: '1px solid var(--border)',
        borderRadius: '10px', overflow: 'hidden',
      }}
      onClick={(e) => e.stopPropagation()}
    >
      <button type="button" className="user-menu-item" title="OpenAI compatibility and external agent docs"
              onClick={(e) => handleAction(e, () => setActiveModal('docs'))}>
        <svg className="um-ic" width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
          <path d="M4 19.5v-15A2.5 2.5 0 0 1 6.5 2H20v20H6.5a2.5 2.5 0 0 1-2.5-2.5Z" />
          <path d="M6 6h10" /><path d="M6 10h10" />
        </svg>
        <span>Docs &amp; Guide</span>
      </button>

      <button type="button" className="user-menu-item" title="System Settings and Preferences"
              onClick={(e) => handleAction(e, () => openSettingsModal('sec-theme'))}>
        <svg className="um-ic" width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
          <path d="M12.22 2h-.44a2 2 0 0 0-2 2v.18a2 2 0 0 1-1 1.73l-.43.25a2 2 0 0 1-2 0l-.15-.08a2 2 0 0 0-2.73.73l-.22.38a2 2 0 0 0 .73 2.73l.15.1a2 2 0 0 1 1 1.72v.51a2 2 0 0 1-1 1.74l-.15.09a2 2 0 0 0-.73 2.73l.22.38a2 2 0 0 0 2.73.73l.15-.08a2 2 0 0 1 2 0l.43.25a2 2 0 0 1 1 1.73V20a2 2 0 0 0 2 2h.44a2 2 0 0 0 2-2v-.18a2 2 0 0 1 1-1.73l.43-.25a2 2 0 0 1 2 0l.15.08a2 2 0 0 0 2.73-.73l.22-.39a2 2 0 0 0-.73-2.73l-.15-.08a2 2 0 0 1-1-1.74v-.5a2 2 0 0 1 1-1.74l.15-.09a2 2 0 0 0 .73-2.73l-.22-.38a2 2 0 0 0-2.73-.73l-.15.08a2 2 0 0 1-2 0l-.43-.25a2 2 0 0 1-1-1.73V4a2 2 0 0 0-2-2z" />
          <circle cx="12" cy="12" r="3" />
        </svg>
        <span>Settings</span>
      </button>

      <div className="user-menu-sep" />

      <button type="button" className="user-menu-item danger text-red"
              onClick={(e) => { e.stopPropagation(); onClose(); logout(); }}>
        <svg className="um-ic" width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true" style={{ color: '#f85149' }}>
          <path d="M9 21H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h4" />
          <polyline points="16 17 21 12 16 7" />
          <line x1="21" x2="9" y1="12" y2="12" />
        </svg>
        <span style={{ color: '#f85149' }}>Log out</span>
      </button>
    </div>
  );
};

import React, { useState } from 'react';
import { useAppStore } from '../../stores/useAppStore';
import { type UserProfile, api } from '../../services/api';
import { openAgentMemory } from '../modals/AgentMemory';

interface UserMenuProps {
  user: UserProfile | null;
  onClose: () => void;
}

const THEMES = ['slate', 'claude', 'tokyonight', 'light'];

export const UserMenu: React.FC<UserMenuProps> = ({ user, onClose }) => {
  const { theme, setTheme, openSettingsModal, setActiveModal, logout } = useAppStore();
  const [showPwdForm, setShowPwdForm] = useState(false);
  const [currentPwd, setCurrentPwd] = useState('');
  const [newPwd, setNewPwd] = useState('');
  const [confirmPwd, setConfirmPwd] = useState('');
  const [pwdMsg, setPwdMsg] = useState<{ text: string; isError?: boolean } | null>(null);
  const [isSavingPwd, setIsSavingPwd] = useState(false);

  const isSuper = !!user?.is_super_admin;
  const canManageUsers = isSuper || (user?.permissions && user.permissions.includes('users.manage'));

  const cycleNextTheme = (e: React.MouseEvent) => {
    e.stopPropagation();
    const curIdx = THEMES.indexOf(theme);
    const nextIdx = (curIdx + 1) % THEMES.length;
    setTheme(THEMES[nextIdx]);
  };

  const handleAction = (e: React.MouseEvent, action: () => void) => {
    e.stopPropagation();
    action();
    onClose();
  };

  const handleSavePassword = async (e: React.MouseEvent) => {
    e.stopPropagation();
    if (!currentPwd || !newPwd || !confirmPwd) {
      setPwdMsg({ text: 'All password fields are required.', isError: true });
      return;
    }
    if (newPwd !== confirmPwd) {
      setPwdMsg({ text: 'New passwords do not match.', isError: true });
      return;
    }

    setIsSavingPwd(true);
    setPwdMsg(null);

    const res = await api.auth.changePassword(currentPwd, newPwd);
    setIsSavingPwd(false);

    if (res.ok) {
      setPwdMsg({ text: 'Password updated successfully!', isError: false });
      setTimeout(() => {
        setShowPwdForm(false);
        setPwdMsg(null);
        setCurrentPwd('');
        setNewPwd('');
        setConfirmPwd('');
      }, 1200);
    } else {
      setPwdMsg({ text: res.error || 'Failed to update password', isError: true });
    }
  };

  return (
    <div
      className="user-menu-dropdown open shadow-2xl"
      style={{
        position: 'absolute',
        bottom: '100%',
        left: '0',
        right: '0',
        marginBottom: '8px',
        zIndex: 9999,
        background: 'var(--panel2)',
        border: '1px solid var(--border)',
        borderRadius: '10px',
        overflow: 'hidden',
      }}
      onClick={(e) => e.stopPropagation()}
    >
      {/* Theme item */}
      <button
        type="button"
        className="user-menu-item"
        onClick={cycleNextTheme}
        title={`Current: ${theme}. Click to cycle themes.`}
      >
        <svg className="um-ic" width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
          <circle cx="13.5" cy="6.5" r=".5" fill="currentColor" />
          <circle cx="17.5" cy="10.5" r=".5" fill="currentColor" />
          <circle cx="8.5" cy="7.5" r=".5" fill="currentColor" />
          <circle cx="6.5" cy="12.5" r=".5" fill="currentColor" />
          <path d="M12 2C6.5 2 2 6.5 2 12s4.5 10 10 10c.926 0 1.648-.746 1.648-1.688 0-.437-.18-.835-.437-1.125-.29-.289-.438-.652-.438-1.125a1.64 1.64 0 0 1 1.668-1.668h1.996c3.051 0 5.563-2.512 5.563-5.563C22 6.5 17.5 2 12 2z" />
        </svg>
        <span>Theme ({theme})</span>
      </button>

      {/* Docs & Guide */}
      <button
        type="button"
        className="user-menu-item"
        onClick={(e) => handleAction(e, () => setActiveModal('docs'))}
        title="Connect external agents, view OpenAI API compatibility & examples"
      >
        <svg className="um-ic" width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
          <path d="M4 19.5v-15A2.5 2.5 0 0 1 6.5 2H20v20H6.5a2.5 2.5 0 0 1-2.5-2.5Z" />
          <path d="M6 6h10" />
          <path d="M6 10h10" />
        </svg>
        <span>Docs & Guide</span>
      </button>

      {/* Agent memory */}
      <button
        type="button"
        className="user-menu-item"
        onClick={(e) => handleAction(e, openAgentMemory)}
        title="What the agent remembers about you"
      >
        <svg className="um-ic" width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
          <path d="M12 5a3 3 0 1 0-5.997.125 4 4 0 0 0-2.526 5.77 4 4 0 0 0 .556 6.588A4 4 0 1 0 12 18Z" />
          <path d="M12 5a3 3 0 1 1 5.997.125 4 4 0 0 1 2.526 5.77 4 4 0 0 1-.556 6.588A4 4 0 1 1 12 18Z" />
          <path d="M12 5v13" />
        </svg>
        <span>My agent memory</span>
      </button>

      {/* Settings */}
      <button
        type="button"
        className="user-menu-item"
        onClick={(e) => handleAction(e, () => openSettingsModal('sec-theme'))}
        title="System Settings and Preferences"
      >
        <svg className="um-ic" width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
          <path d="M12.22 2h-.44a2 2 0 0 0-2 2v.18a2 2 0 0 1-1 1.73l-.43.25a2 2 0 0 1-2 0l-.15-.08a2 2 0 0 0-2.73.73l-.22.38a2 2 0 0 0 .73 2.73l.15.1a2 2 0 0 1 1 1.72v.51a2 2 0 0 1-1 1.74l-.15.09a2 2 0 0 0-.73 2.73l.22.38a2 2 0 0 0 2.73.73l.15-.08a2 2 0 0 1 2 0l.43.25a2 2 0 0 1 1 1.73V20a2 2 0 0 0 2 2h.44a2 2 0 0 0 2-2v-.18a2 2 0 0 1 1-1.73l.43-.25a2 2 0 0 1 2 0l.15.08a2 2 0 0 0 2.73-.73l.22-.39a2 2 0 0 0-.73-2.73l-.15-.08a2 2 0 0 1-1-1.74v-.5a2 2 0 0 1 1-1.74l.15-.09a2 2 0 0 0 .73-2.73l-.22-.38a2 2 0 0 0-2.73-.73l-.15.08a2 2 0 0 1-2 0l-.43-.25a2 2 0 0 1-1-1.73V4a2 2 0 0 0-2-2z" />
          <circle cx="12" cy="12" r="3" />
        </svg>
        <span>Settings</span>
      </button>

      {/* 2FA */}
      <button
        type="button"
        className="user-menu-item"
        onClick={(e) => handleAction(e, () => openSettingsModal('sec-mfa'))}
        title="Configure Two-Factor Authentication (TOTP)"
      >
        <svg className="um-ic" width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
          <rect width="18" height="11" x="3" y="11" rx="2" ry="2" />
          <path d="M7 11V7a5 5 0 0 1 10 0v4" />
        </svg>
        <span>Two-Factor Auth (2FA)</span>
      </button>

      {/* Users & Roles (if admin) */}
      {canManageUsers && (
        <>
          <button
            type="button"
            className="user-menu-item"
            onClick={(e) => handleAction(e, () => openSettingsModal('sec-users'))}
            title="Manage Users and RBAC Roles"
          >
            <svg className="um-ic" width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
              <path d="M16 21v-2a4 4 0 0 0-4-4H6a4 4 0 0 0-4 4v2" />
              <circle cx="9" cy="7" r="4" />
              <path d="M22 21v-2a4 4 0 0 0-3-3.87" />
              <path d="M16 3.13a4 4 0 0 1 0 7.75" />
            </svg>
            <span>Users & Roles</span>
          </button>

          <button
            type="button"
            className="user-menu-item"
            onClick={(e) => handleAction(e, () => openSettingsModal('sec-session'))}
            title="Manage Session Expiry & Security Policy"
          >
            <svg className="um-ic" width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
              <circle cx="12" cy="12" r="10" />
              <polyline points="12 6 12 12 16 14" />
            </svg>
            <span>Session Security</span>
          </button>
        </>
      )}

      <div className="user-menu-sep" />

      {/* Change Password */}
      <button
        type="button"
        className="user-menu-item"
        onClick={(e) => {
          e.stopPropagation();
          setShowPwdForm(!showPwdForm);
        }}
      >
        <svg className="um-ic" width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
          <path d="m21 2-2 2m-1.5 1.5L12 11l4 4 5.5-5.5a2.12 2.12 0 0 0 0-3L20 5" />
          <path d="m15.5 7.5 3 3" />
          <path d="m3 21 6-6" />
          <path d="m9 15 2 2" />
          <path d="m11 13 2 2" />
        </svg>
        <span>Change password</span>
      </button>

      {showPwdForm && (
        <div className="user-menu-pwd-form p-2.5 flex flex-col gap-2 bg-panel border-b border-border-subtle text-xs">
          <input
            type="password"
            placeholder="Current password"
            value={currentPwd}
            onChange={(e) => setCurrentPwd(e.target.value)}
            className="w-full bg-bg border border-border-subtle rounded px-2 py-1 text-text outline-none text-xs"
          />
          <input
            type="password"
            placeholder="New password"
            value={newPwd}
            onChange={(e) => setNewPwd(e.target.value)}
            className="w-full bg-bg border border-border-subtle rounded px-2 py-1 text-text outline-none text-xs"
          />
          <input
            type="password"
            placeholder="Confirm new password"
            value={confirmPwd}
            onChange={(e) => setConfirmPwd(e.target.value)}
            className="w-full bg-bg border border-border-subtle rounded px-2 py-1 text-text outline-none text-xs"
          />
          {pwdMsg && (
            <div className={`text-[11px] ${pwdMsg.isError ? 'text-red' : 'text-green'}`} style={{ color: pwdMsg.isError ? '#f85149' : '#3fb950' }}>
              {pwdMsg.text}
            </div>
          )}
          <div className="flex justify-end gap-2 pt-1">
            <button
              type="button"
              className="btn ghost text-xs px-2 py-1"
              style={{ width: 'auto', margin: 0, padding: '3px 8px', fontSize: '11px' }}
              onClick={(e) => {
                e.stopPropagation();
                setShowPwdForm(false);
              }}
            >
              Cancel
            </button>
            <button
              type="button"
              className="btn accent text-xs px-2 py-1"
              style={{ width: 'auto', margin: 0, padding: '3px 12px', fontSize: '11px' }}
              disabled={isSavingPwd}
              onClick={handleSavePassword}
            >
              {isSavingPwd ? 'Saving…' : 'Save'}
            </button>
          </div>
        </div>
      )}

      <div className="user-menu-sep" />

      {/* Log out */}
      <button
        type="button"
        className="user-menu-item danger text-red"
        onClick={(e) => {
          e.stopPropagation();
          onClose();
          logout();
        }}
      >
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

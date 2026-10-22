import React, { useEffect, useState } from 'react';
import { useAppStore } from '../../stores/useAppStore';
import { api } from '../../services/api';
import { markActive } from '../../services/session';

/** "You will be signed out in 1 minute": shown by the idle watch. */
export const IdleWarning: React.FC = () => {
  const idleWarning = useAppStore((s) => s.idleWarning);
  if (!idleWarning) return null;
  return (
    <div
      role="alert"
      style={{ position: 'fixed', right: 16, bottom: 16, zIndex: 10000, padding: '12px 14px', borderRadius: 8, background: 'var(--panel, #222)', color: 'var(--text, #eee)', border: '1px solid var(--border, #555)', boxShadow: '0 4px 16px rgba(0,0,0,.35)', fontSize: 13 }}
    >
      <div>You will be signed out in 1 minute due to inactivity.</div>
      <button type="button" className="btn accent" style={{ marginTop: 8 }} onClick={() => markActive(true)}>Stay signed in</button>
    </div>
  );
};

/** Blocks the app until the password is changed when the account is flagged must_change_password. */
export const ForcePasswordModal: React.FC = () => {
  const { currentUser, setAuthenticatedUser, logout } = useAppStore();
  const [current, setCurrent] = useState('');
  const [next, setNext] = useState('');
  const [confirm, setConfirm] = useState('');
  const [error, setError] = useState('');
  const [busy, setBusy] = useState(false);

  useEffect(() => { setError(''); }, [currentUser?.id]);
  if (!currentUser?.must_change_password) return null;

  const submit = async (e: React.FormEvent) => {
    e.preventDefault();
    if (!current || !next) return setError('Fill in every field.');
    if (next !== confirm) return setError('New passwords do not match.');
    setBusy(true);
    setError('');
    const res = await api.auth.changePassword(current, next);
    setBusy(false);
    if (!res.ok) return setError(res.error || 'Could not change the password.');
    // the server re-issued the session; reload the profile so the flag clears
    setCurrent('');
    setNext('');
    setConfirm('');
    const me = await api.auth.getMe();
    if (me) setAuthenticatedUser({ ...me, must_change_password: false });
    else setError('Password changed. Reload the page to continue.');
  };

  const field: React.CSSProperties = { width: '100%', boxSizing: 'border-box', padding: '10px 12px', borderRadius: 8, border: '1px solid var(--border)', background: 'var(--bg-input, rgba(0,0,0,.25))', color: 'inherit', fontSize: 14, marginTop: 6 };
  return (
    <div style={{ position: 'fixed', inset: 0, zIndex: 20000, background: 'rgba(0,0,0,.8)', backdropFilter: 'blur(6px)', display: 'grid', placeItems: 'center', padding: 16 }}>
      <form onSubmit={submit} style={{ width: 380, maxWidth: '100%', background: 'var(--panel)', border: '1px solid var(--border)', borderRadius: 12, padding: 24 }}>
        <h2 style={{ margin: '0 0 6px', fontSize: 20 }}>Set a new password</h2>
        <p style={{ margin: '0 0 14px', fontSize: 13, color: 'var(--dim)' }}>
          Your account requires a new password before you continue. Use 12 or more characters with letters and digits.
        </p>
        <input type="password" autoComplete="current-password" placeholder="Current password" value={current} onChange={(e) => setCurrent(e.target.value)} style={field} autoFocus />
        <input type="password" autoComplete="new-password" placeholder="New password" value={next} onChange={(e) => setNext(e.target.value)} style={field} />
        <input type="password" autoComplete="new-password" placeholder="Confirm new password" value={confirm} onChange={(e) => setConfirm(e.target.value)} style={field} />
        {error && <div role="alert" style={{ marginTop: 12, fontSize: 12.5, color: '#fca5a5' }}>{error}</div>}
        <button type="submit" className="btn accent" disabled={busy} style={{ width: '100%', marginTop: 16, padding: 11 }}>{busy ? 'Saving…' : 'Change password'}</button>
        <button type="button" onClick={() => logout()} style={{ width: '100%', marginTop: 8, background: 'transparent', border: 'none', color: 'var(--dim)', cursor: 'pointer', fontSize: 12.5 }}>Sign out</button>
      </form>
    </div>
  );
};

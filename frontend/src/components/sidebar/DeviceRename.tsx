import React, { useState } from 'react';
import { apiFetch, getClientDeviceId, getClientDeviceName } from '../../services/api';
import { useAppStore } from '../../stores/useAppStore';

/** Small "rename this device" button with an inline editor (replaces the legacy prompt() flow). */
export const DeviceRename: React.FC = () => {
  const fetchProjects = useAppStore((s) => s.fetchProjects);
  const [editing, setEditing] = useState(false);
  const [name, setName] = useState('');
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');

  const start = () => {
    setName(getClientDeviceName());
    setError('');
    setEditing(true);
  };

  const save = async () => {
    const next = name.trim();
    if (!next || busy) return;
    // the name travels in a request header, which only carries Latin-1: a longer alphabet would be dropped silently
    if (/[^\x00-\xFF]/.test(next)) {
      setError('Use Latin letters, digits and common symbols: the device name is sent in a request header.');
      return;
    }
    if (next === getClientDeviceName()) {
      setEditing(false);
      return;
    }
    setBusy(true);
    setError('');
    try {
      const r = await apiFetch('/control/device/rename', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ old_name: getClientDeviceName(), new_name: next, device_id: getClientDeviceId() }),
      });
      const d = await r.json().catch(() => ({ error: `server error ${r.status}` }));
      if (!r.ok || d.ok === false || d.error) {
        setError(d.error || `Rename failed (${r.status})`);
        return;
      }
      try { localStorage.setItem('antigravity_device_name', next); } catch (_) {}
      setEditing(false);
      await fetchProjects();
    } catch (e: any) {
      setError(e?.message || 'Rename failed');
    } finally {
      setBusy(false);
    }
  };

  if (!editing) {
    return (
      <button className="btn ghost" title="Rename this device" onClick={start} style={{ width: 'auto', margin: 0, padding: '2px 8px', fontSize: 11 }}>
        ✎ Rename device
      </button>
    );
  }
  return (
    <div style={{ display: 'flex', flexDirection: 'column', gap: 4 }}>
      <div style={{ display: 'flex', gap: 4 }}>
        <input
          type="text"
          autoFocus
          value={name}
          disabled={busy}
          maxLength={60}
          onChange={(e) => setName(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === 'Enter') save();
            else if (e.key === 'Escape') setEditing(false);
          }}
          style={{ flex: 1, padding: '4px 8px', fontSize: 11, background: 'var(--bg)', border: '1px solid var(--border)', borderRadius: 4, color: 'inherit' }}
        />
        <button className="btn ghost" disabled={busy || !name.trim()} onClick={save} style={{ width: 'auto', margin: 0, padding: '3px 8px', fontSize: 11 }}>
          Save
        </button>
        <button className="btn ghost" onClick={() => setEditing(false)} style={{ width: 'auto', margin: 0, padding: '3px 8px', fontSize: 11 }}>
          ✕
        </button>
      </div>
      {error && <div style={{ fontSize: 11, color: 'var(--red, #f87171)' }}>{error}</div>}
    </div>
  );
};

import React from 'react';
import { useAppStore } from '../../stores/useAppStore';

export const ThemedDialog: React.FC = () => {
  const { dialog, closeDialog } = useAppStore();

  if (!dialog) return null;

  return (
    <div
      style={{
        position: 'fixed',
        inset: 0,
        zIndex: 99999,
        display: 'flex',
        alignItems: 'center',
        justifyContent: 'center',
        background: 'rgba(0, 0, 0, 0.65)',
        backdropFilter: 'blur(4px)',
        padding: '20px',
      }}
      onClick={closeDialog}
    >
      <div
        style={{
          width: '100%',
          maxWidth: '440px',
          background: 'var(--panel, #181b20)',
          border: '1px solid var(--border, rgba(255, 255, 255, 0.12))',
          borderRadius: '12px',
          padding: '20px',
          boxShadow: '0 20px 50px rgba(0, 0, 0, 0.7)',
          color: 'var(--text, #f0f6fc)',
        }}
        onClick={(e) => e.stopPropagation()}
      >
        <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', marginBottom: '12px' }}>
          <h3 style={{ margin: 0, fontSize: '15px', fontWeight: 700, letterSpacing: '-0.2px' }}>
            {dialog.title || 'Notification'}
          </h3>
          <button
            type="button"
            onClick={closeDialog}
            style={{
              background: 'transparent',
              border: 'none',
              color: 'var(--dim, #8b949e)',
              cursor: 'pointer',
              fontSize: '15px',
              padding: '2px 6px',
            }}
          >
            ✕
          </button>
        </div>
        <p style={{ margin: '0 0 20px 0', fontSize: '13px', lineHeight: 1.6, color: 'var(--text-muted, #94a3b8)' }}>
          {dialog.message}
        </p>
        <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'flex-end', gap: '10px' }}>
          {dialog.cancelLabel !== null && (
            <button
              type="button"
              className="btn ghost"
              onClick={() => {
                if (dialog.onCancel) dialog.onCancel();
                closeDialog();
              }}
              style={{
                padding: '6px 14px',
                borderRadius: '6px',
                fontSize: '12.5px',
                cursor: 'pointer',
              }}
            >
              {dialog.cancelLabel || 'Close'}
            </button>
          )}
          {dialog.confirmLabel && (
            <button
              type="button"
              className="btn primary"
              onClick={() => {
                const cb = dialog.onConfirm;
                closeDialog();
                if (cb) cb();
              }}
              style={{
                padding: '6px 16px',
                borderRadius: '6px',
                fontSize: '12.5px',
                fontWeight: 600,
                cursor: 'pointer',
                background: 'var(--accent, #38bdf8)',
                color: '#000000',
                border: 'none',
              }}
            >
              {dialog.confirmLabel}
            </button>
          )}
        </div>
      </div>
    </div>
  );
};

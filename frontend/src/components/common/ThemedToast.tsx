import React from 'react';
import { useAppStore } from '../../stores/useAppStore';

export const ThemedToast: React.FC = () => {
  const { toast, hideToast } = useAppStore();

  if (!toast) return null;

  const getBorderColor = () => {
    switch (toast.type) {
      case 'error': return 'rgba(239, 68, 68, 0.6)';
      case 'success': return 'rgba(34, 197, 94, 0.6)';
      case 'warn': return 'rgba(245, 158, 11, 0.6)';
      default: return 'var(--border, rgba(255, 255, 255, 0.15))';
    }
  };

  const getIcon = () => {
    switch (toast.type) {
      case 'error': return '⚠️';
      case 'success': return '✅';
      case 'warn': return '⚡';
      default: return 'ℹ️';
    }
  };

  return (
    <div
      style={{
        position: 'fixed',
        bottom: '80px',
        left: '50%',
        transform: 'translateX(-50%)',
        zIndex: 99999,
        background: 'var(--panel2, #1c1c24)',
        border: `1px solid ${getBorderColor()}`,
        borderRadius: '10px',
        padding: '10px 18px',
        color: 'var(--text, #f3f4f6)',
        fontSize: '13px',
        fontWeight: 500,
        boxShadow: '0 12px 36px rgba(0,0,0,0.6)',
        display: 'flex',
        alignItems: 'center',
        gap: '10px',
        maxWidth: '90vw',
        backdropFilter: 'blur(8px)',
        transition: 'all 0.2s ease',
      }}
    >
      <span style={{ fontSize: '15px' }}>{getIcon()}</span>
      <span style={{ lineHeight: 1.4 }}>{toast.message}</span>
      <button
        type="button"
        onClick={hideToast}
        style={{
          background: 'transparent',
          border: 'none',
          color: 'var(--dim, #8b949e)',
          cursor: 'pointer',
          padding: '2px 4px',
          marginLeft: '8px',
          fontSize: '13px',
          lineHeight: 1,
        }}
        title="Dismiss"
      >
        ✕
      </button>
    </div>
  );
};

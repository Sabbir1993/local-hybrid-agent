import React, { useEffect, useState } from 'react';
import { useAppStore } from '../../stores/useAppStore';

function promptText(m: any): string {
  let t = m.displayContent || m.content || '';
  if (t.includes('--- IMAGE:')) t = t.replace(/--- IMAGE:[\s\S]*?--- END [^\n]+ ---/g, '');
  if (t.includes('--- FILE:')) t = t.replace(/--- FILE:[\s\S]*?--- END [^\n]+ ---/g, '');
  if (t.includes('[Attached Files]')) t = t.replace(/\[Attached Files\][\s\S]*?--- END [^\n]+ ---(\s*\[NOTE:.*\])?/g, '');
  t = t.replace(/\s+/g, ' ').trim();
  return t ? (t.length > 90 ? t.slice(0, 90) + '…' : t) : '(attachment)';
}

export const PromptDots: React.FC = () => {
  const { messages } = useAppStore();
  const [activeIdx, setActiveIdx] = useState<number>(-1);

  const userMessages = messages.filter((m) => m.role === 'user');

  useEffect(() => {
    const chatEl = document.getElementById('chat');
    if (!chatEl || userMessages.length < 2) return;

    const handleScroll = () => {
      const top = chatEl.getBoundingClientRect().top;
      const line = chatEl.clientHeight * 0.3;
      let found = 0;

      userMessages.forEach((m, idx) => {
        const msgEl = document.getElementById(`msg-${m.id}`);
        if (msgEl) {
          const rect = msgEl.getBoundingClientRect();
          if (rect.top - top <= line) {
            found = idx;
          }
        }
      });
      setActiveIdx(found);
    };

    chatEl.addEventListener('scroll', handleScroll, { passive: true });
    handleScroll();
    return () => chatEl.removeEventListener('scroll', handleScroll);
  }, [messages, userMessages.length]);

  if (userMessages.length < 2) return null;

  const handleDotClick = (m: any, idx: number) => {
    const chatEl = document.getElementById('chat');
    const msgEl = document.getElementById(`msg-${m.id}`);
    if (chatEl && msgEl) {
      const targetTop = chatEl.scrollTop + msgEl.getBoundingClientRect().top - chatEl.getBoundingClientRect().top - 8;
      chatEl.scrollTo({ top: Math.max(0, targetTop), behavior: 'smooth' });
      setActiveIdx(idx);
    }
  };

  return (
    <div
      id="prompt-dots"
      aria-label="Jump to a prompt"
      style={{
        position: 'absolute',
        left: '10px',
        top: '50%',
        transform: 'translateY(-50%)',
        display: 'flex',
        flexDirection: 'column',
        alignItems: 'center',
        justifyContent: 'center',
        gap: '6px',
        padding: '8px 4px',
        background: 'var(--panel, #1e1e24)',
        border: '1px solid var(--border-subtle, rgba(255, 255, 255, 0.08))',
        borderRadius: '20px',
        boxShadow: '0 4px 16px rgba(0, 0, 0, 0.35)',
        zIndex: 40,
        maxHeight: '60vh',
        backdropFilter: 'blur(8px)',
      }}
    >
      {userMessages.map((m, idx) => {
        const label = `Prompt ${idx + 1} of ${userMessages.length}: ${promptText(m)}`;
        const isActive = activeIdx === idx || (activeIdx === -1 && idx === userMessages.length - 1);
        return (
          <button
            key={String(m.id)}
            type="button"
            className={`pd-dot ${isActive ? 'on' : ''}`}
            title={label}
            aria-label={label}
            onClick={() => handleDotClick(m, idx)}
            style={{
              position: 'relative',
              width: '16px',
              height: '16px',
              padding: 0,
              border: 0,
              background: 'transparent',
              cursor: 'pointer',
              borderRadius: '50%',
              display: 'flex',
              alignItems: 'center',
              justifyContent: 'center',
            }}
          >
            <span
              style={{
                width: '8px',
                height: '8px',
                borderRadius: '50%',
                background: isActive ? 'var(--accent, #f97316)' : 'var(--dim, #8b949e)',
                opacity: isActive ? 1 : 0.45,
                transform: isActive ? 'scale(1.25)' : 'scale(1)',
                transition: 'all 0.15s ease',
              }}
            />
          </button>
        );
      })}
    </div>
  );
};

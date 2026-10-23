import React, { useEffect, useState } from 'react';
import '../../styles/loader.css';

const STEPS = [
  'Connecting to Local Agent…',
  'Checking your session…',
  'Loading your workspace…',
];

/** Fade out and remove the HTML boot loader from index.html (safe to call more than once). */
export function dismissBootLoader(): void {
  const boot = document.getElementById('boot-loader');
  if (!boot || boot.classList.contains('boot-out')) return;
  boot.classList.add('boot-out');
  window.setTimeout(() => boot.remove(), 300);
}

/** Full-screen start-up screen: logo inside a spinning ring, a short progress line that moves on every
 *  couple of seconds, and a calm hint when the server is slow to answer. */
export const AppLoader: React.FC = () => {
  const [step, setStep] = useState(0);
  const [slow, setSlow] = useState(false);

  // When the page already shows the HTML boot loader, keep that one on screen (a second copy would restart every animation
  // and flicker) and only update its text. Without it (dev, tests) this component draws the whole screen itself.
  const [hasBoot] = useState(() => !!document.getElementById('boot-loader'));
  useEffect(() => {
    if (!hasBoot) return;
    const t = document.getElementById('boot-text');
    const sub = document.getElementById('boot-sub');
    if (t) t.textContent = STEPS[step];
    if (sub) sub.textContent = slow ? 'Taking longer than usual — the server may still be starting.' : '';
  }, [hasBoot, step, slow]);

  useEffect(() => {
    const next = window.setInterval(() => setStep((s) => Math.min(s + 1, STEPS.length - 1)), 1800);
    const late = window.setTimeout(() => setSlow(true), 8000);
    return () => { window.clearInterval(next); window.clearTimeout(late); };
  }, []);

  if (hasBoot) return null;

  return (
    <div className="app-loader" role="status" aria-live="polite" aria-label="Loading the application">
      <div className="app-loader-glow" aria-hidden="true" />
      <div className="app-loader-card">
        <div className="app-loader-mark" aria-hidden="true">
          <span className="app-loader-ring" />
          <span className="app-loader-ring app-loader-ring-slow" />
          <img src="/static/logo/logo.jpg" alt="" width={56} height={56} />
        </div>
        <div className="app-loader-text">
          <span key={step} className="app-loader-step">{STEPS[step]}</span>
          <span className="app-loader-sub">
            {slow ? 'Taking longer than usual — the server may still be starting.' : 'Just a moment'}
          </span>
        </div>
        <div className="app-loader-bar" aria-hidden="true"><span /></div>
      </div>
    </div>
  );
};

/** Small inline spinner for panels that are loading (Tasks, lists). */
export const Spinner: React.FC<{ label?: string }> = ({ label = 'Loading…' }) => (
  <div className="app-spinner-row" role="status" aria-live="polite">
    <span className="app-spinner" aria-hidden="true" />
    <span>{label}</span>
  </div>
);

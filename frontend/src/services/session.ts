// Idle sign-out and "a running job is not idle" (port of static/js/session.js, PCI DSS 8.2.8).
// Real input keeps the server session alive; background polls send X-A770-Background so they do not.

let idleMs = 15 * 60 * 1000;
let lastActive = Date.now();
let lastTouch = Date.now();
let timer: number | null = null;
let listening = false;
let onLogout: (() => void) | null = null;
let onWarn: ((show: boolean) => void) | null = null;
let warned = false;
let holds = 0;
let holdTimer: number | null = null;

const SHARED_KEY = 'a770_last_active';
const EVENTS = ['keydown', 'pointerdown', 'wheel', 'touchstart'];

function setWarn(show: boolean) {
  if (warned === show) return;
  warned = show;
  onWarn?.(show);
}

export function noteServerTouch() {
  lastTouch = Date.now();
}

/** Any real input (or a streaming job) counts as activity; the server session is refreshed at most once a minute. */
export function markActive(force: boolean | Event = false) {
  const now = Date.now();
  if (now - lastActive > 5000) {
    try { localStorage.setItem(SHARED_KEY, String(now)); } catch (_) {}   // tabs share one session
  }
  lastActive = now;
  setWarn(false);
  if (force === true || now - lastTouch > 60 * 1000) {
    lastTouch = now;
    fetch('/auth/me', { credentials: 'same-origin' })
      .then((r) => { if (r.status === 401) onLogout?.(); })
      .catch(() => {});
  }
}

function checkIdle() {
  if (holds > 0) return;
  const shared = Number(localStorage.getItem(SHARED_KEY) || 0);
  if (shared > lastActive) {
    lastActive = shared;
    setWarn(false);
  }
  const idle = Date.now() - lastActive;
  if (idle >= idleMs) onLogout?.();
  else if (idle >= idleMs - 60 * 1000) setWarn(true);
}

const onVisible = () => { if (document.visibilityState === 'visible') checkIdle(); };

export function startSessionWatch(opts: { idleSeconds?: number; idleEnabled?: boolean; onLogout: () => void; onWarn: (show: boolean) => void }) {
  stopSessionWatch();
  onLogout = opts.onLogout;
  onWarn = opts.onWarn;
  idleMs = opts.idleSeconds ? opts.idleSeconds * 1000 : 15 * 60 * 1000;
  // an admin can turn idle sign-out off (Settings > Session security)
  if (opts.idleEnabled === false) return;
  lastActive = Date.now();
  EVENTS.forEach((ev) => document.addEventListener(ev, markActive as EventListener, { passive: true, capture: true }));
  document.addEventListener('visibilitychange', onVisible);
  listening = true;
  timer = window.setInterval(checkIdle, 10 * 1000);
}

export function stopSessionWatch() {
  if (timer) window.clearInterval(timer);
  timer = null;
  if (listening) {
    EVENTS.forEach((ev) => document.removeEventListener(ev, markActive as EventListener, true));
    document.removeEventListener('visibilitychange', onVisible);
    listening = false;
  }
  warned = false;
}

/** Call while a stream is open; returns a release function. A long agent step can go minutes without a byte. */
export function holdSession(): () => void {
  if (++holds === 1) {
    markActive(true);
    holdTimer = window.setInterval(() => markActive(true), 30 * 1000);
  }
  let released = false;
  return () => {
    if (released) return;
    released = true;
    if (--holds === 0) {
      if (holdTimer) window.clearInterval(holdTimer);
      holdTimer = null;
      markActive(false);
    }
  };
}

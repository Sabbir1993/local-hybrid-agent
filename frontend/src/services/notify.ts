// "Your task finished" and "Action required" alerts when the app is not in front (port of notifyTaskFinished in static/js/utils.js).
// Nothing leaves the device: only the desktop app bridge or the browser's own Notification API is used.

import { env } from '../config/env';

let flashTimer: number | null = null;
let savedBaseTitle: string | null = null;

export function requestNotificationPermission() {
  try {
    if (typeof Notification !== 'undefined' && Notification.permission === 'default') {
      Notification.requestPermission().catch(() => {});
    }
  } catch (_) {
    // not supported here
  }
}

export function stopFlashTitle() {
  if (flashTimer) {
    window.clearInterval(flashTimer);
    flashTimer = null;
  }
  if (savedBaseTitle !== null) {
    document.title = savedBaseTitle;
    savedBaseTitle = null;
  }
}

function flashTitle(text: string) {
  if (flashTimer) window.clearInterval(flashTimer);
  if (savedBaseTitle === null) {
    savedBaseTitle = document.title;
  }
  const baseTitle = savedBaseTitle;
  let on = false;
  flashTimer = window.setInterval(() => {
    on = !on;
    document.title = on ? text : baseTitle;
  }, 1000);
  const stop = () => {
    stopFlashTitle();
    window.removeEventListener('focus', stop);
    document.removeEventListener('visibilitychange', onVisible);
  };
  // coming back by switching tabs raises no focus event
  const onVisible = () => { if (document.visibilityState === 'visible') stop(); };
  window.addEventListener('focus', stop);
  document.addEventListener('visibilitychange', onVisible);
}

export function playNotificationChime() {
  try {
    const AC = window.AudioContext || (window as any).webkitAudioContext;
    if (!AC) return;
    const ctx = new AC();
    const now = ctx.currentTime;
    const osc = ctx.createOscillator();
    const gain = ctx.createGain();
    osc.type = 'sine';
    osc.frequency.setValueAtTime(587.33, now); // D5
    osc.frequency.setValueAtTime(880, now + 0.12); // A5
    gain.gain.setValueAtTime(0.08, now);
    gain.gain.exponentialRampToValueAtTime(0.0001, now + 0.35);
    osc.connect(gain);
    gain.connect(ctx.destination);
    osc.start(now);
    osc.stop(now + 0.35);
  } catch (_) {
    // audio autoplay or context restrictions should never throw
  }
}

async function appInBackground(): Promise<boolean> {
  const bridge = window.electronAPI;
  if (bridge?.isMinimized) {
    try {
      if (await bridge.isMinimized()) return true;
    } catch (_) {
      // fall back to the page state
    }
  }
  return document.hidden || !document.hasFocus();
}

export async function notifyTaskFinished(opts: { title: string; body: string }) {
  if (!(await appInBackground())) return;
  const bridge = window.electronAPI;
  try {
    // the app's own name and logo lead the alert; a browser still adds its own "from Chrome" line unless the app is
    // installed (Chrome menu > Cast, save and share > Install page as app), after which toasts show the app name only
    const title = `${env.projectName}: ${opts.title}`;
    if (bridge?.notify) await bridge.notify({ title, body: opts.body });
    else if (typeof Notification !== 'undefined' && Notification.permission === 'granted') {
      const n = new Notification(title, { body: opts.body, icon: '/static/logo/icon-192.png', badge: '/static/logo/icon-192.png', tag: 'task-finished' });
      n.onclick = () => { window.focus(); n.close(); };
    }
    if (bridge?.flashFrame) await bridge.flashFrame(true);
  } catch (_) {
    // a failed alert must never affect the run
  }
  flashTitle('Done: ' + opts.title);
}

export async function notifyPermissionRequested(opts?: { cmd?: string; kind?: string }) {
  playNotificationChime();
  if (!(await appInBackground())) return;
  const bridge = window.electronAPI;
  const cmdStr = (opts?.cmd || '').trim();
  const cmdSnippet = cmdStr.length > 50 ? cmdStr.slice(0, 47) + '...' : cmdStr;
  const body = cmdSnippet ? `The agent is waiting for approval to run: ${cmdSnippet}` : 'The agent is waiting for your permission to proceed.';
  const title = `${env.projectName}: Permission required`;

  try {
    if (bridge?.notify) {
      await bridge.notify({ title, body });
    } else if (typeof Notification !== 'undefined' && Notification.permission === 'granted') {
      const n = new Notification(title, {
        body,
        icon: '/static/logo/icon-192.png',
        badge: '/static/logo/icon-192.png',
        tag: 'permission-required',
        requireInteraction: true,
      });
      n.onclick = () => {
        window.focus();
        n.close();
      };
    }
    if (bridge?.flashFrame) await bridge.flashFrame(true);
  } catch (_) {
    // a failed alert must never affect the run
  }
  flashTitle('⚠️ Permission required');
}

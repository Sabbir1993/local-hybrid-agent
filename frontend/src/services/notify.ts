// "Your task finished" alerts when the app is not in front (port of notifyTaskFinished in static/js/utils.js).
// Nothing leaves the device: only the desktop app bridge or the browser's own Notification API is used.

import { env } from '../config/env';

let flashTimer: number | null = null;

export function requestNotificationPermission() {
  try {
    if (typeof Notification !== 'undefined' && Notification.permission === 'default') {
      Notification.requestPermission().catch(() => {});
    }
  } catch (_) {
    // not supported here
  }
}

function flashTitle(text: string) {
  if (flashTimer) window.clearInterval(flashTimer);
  const baseTitle = document.title;   // whatever the page is called right now
  let on = false;
  flashTimer = window.setInterval(() => {
    on = !on;
    document.title = on ? text : baseTitle;
  }, 1000);
  const stop = () => {
    if (flashTimer) window.clearInterval(flashTimer);
    flashTimer = null;
    document.title = baseTitle;
    window.removeEventListener('focus', stop);
    document.removeEventListener('visibilitychange', onVisible);
  };
  // coming back by switching tabs raises no focus event
  const onVisible = () => { if (document.visibilityState === 'visible') stop(); };
  window.addEventListener('focus', stop);
  document.addEventListener('visibilitychange', onVisible);
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

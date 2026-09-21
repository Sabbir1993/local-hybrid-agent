"use strict";
const $ = id => document.getElementById(id);
const LUID_NAMES = {
  '0x04ac733a': 'A770 #1 · display',
  '0x04ab067c': 'A770 #2 · headless',
};
const IGNORED_LUIDS = new Set(['0x000165f7', '0x0001665a']);

let messages = [];       // {role, content, reasoning, tps?, ntok?, secs?}
let generating = false;
let ctrl = null;
let curStatus = null;
function isNativeAppClient() {
  return !!(
    (typeof window !== 'undefined' && window.electronAPI && window.electronAPI.isNativeApp) ||
    (typeof navigator !== 'undefined' && (navigator.userAgent.includes("A770NativeApp") || navigator.userAgent.includes("Electron")))
  );
}
window.isNativeAppClient = isNativeAppClient;

let agentMode = (function() {
  try {
    if (!isNativeAppClient()) return false;
    return localStorage.getItem('app_mode') === 'agent';
  } catch (_) { return false; }
})();
let planMode = false;
let chatWebSearch = true;
try {
  const savedWeb = localStorage.getItem('chat_web_search');
  if (savedWeb !== null) chatWebSearch = savedWeb === '1';
} catch (e) {}

const bgProcessEnabled = true;
window.bgJobs = window.bgJobs || new Map();

function isSessionGenerating(sid) {
  return sid != null && window.bgJobs.has(String(sid));
}

function getActiveBgJobs() {
  return Array.from(window.bgJobs.values());
}

function abortSessionJob(sid) {
  const sKey = String(sid);
  if (window.bgJobs.has(sKey)) {
    const job = window.bgJobs.get(sKey);
    try { if (job.ctrl) job.ctrl.abort(); } catch (e) {}
    toast(`Session "${job.title || sKey}" stopped`);
  }
}

function updateBgIndicators() {
  const activeJobs = getActiveBgJobs();
  const count = activeJobs.length;

  // 1. Header chip in top navigation bar (clean & non-intrusive)
  const chip = $('chip-bg-indicator');
  const chipCount = $('chip-bg-count');
  if (chip) {
    if (count > 0) {
      chip.style.display = 'inline-flex';
      if (chipCount) chipCount.textContent = count;
      const curIdStr = curSession ? String(curSession.id) : null;
      const otherJobs = activeJobs.filter(j => String(j.id) !== curIdStr);
      if (otherJobs.length > 0) {
        chip.title = `${count} background task${count > 1 ? 's' : ''} running — click to view "${otherJobs[0].title || 'session'}"`;
        chip.style.cursor = 'pointer';
        chip.onclick = () => { if (window.openSessionById) window.openSessionById(otherJobs[0].id); };
      } else {
        chip.title = 'Current session is working in background';
        chip.style.cursor = 'default';
        chip.onclick = null;
      }
    } else {
      chip.style.display = 'none';
      chip.onclick = null;
    }
  }

  // 2. Update sidebar session items (pulsing indicator icon on the working session)
  document.querySelectorAll('#session-list .session-row').forEach(row => {
    const sid = row.getAttribute('data-sid');
    if (!sid) return;
    const isRunning = window.bgJobs.has(String(sid));
    let ind = row.querySelector('.bg-session-indicator');
    if (isRunning) {
      if (!ind) {
        ind = document.createElement('span');
        ind.className = 'bg-session-indicator';
        ind.title = 'Executing in background...';
        ind.innerHTML = '<span class="bg-pulse-dot"></span>⚡';
        const wrap = row.querySelector('.s-menu-wrap');
        if (wrap) row.insertBefore(ind, wrap);
        else row.appendChild(ind);
      }
    } else if (ind) {
      ind.remove();
    }
  });
}

function updateWebToggleUI() {
  const btn = $('btn-web-toggle');
  if (!btn) return;
  btn.classList.toggle('active', !!chatWebSearch);
  btn.title = chatWebSearch
    ? 'Web Search is ON (model searches web & fetches URLs) — Click to turn OFF'
    : 'Web Search is OFF (offline local knowledge only) — Click to turn ON';
}

/* ---------------- theme management ---------------- */
const THEME_KEY = 'a770_theme';
const THEMES = ['slate', 'claude', 'tokyonight', 'oled', 'nord', 'classic', 'light'];
const THEME_NAMES = {
  slate: 'Slate & Indigo',
  claude: 'Claude Warm',
  tokyonight: 'Tokyo Night',
  oled: 'OLED Black',
  nord: 'Nord Arctic',
  classic: 'Classic Dark',
  light: 'Pure Light ☀️',
};

function getSavedTheme() {
  return localStorage.getItem(THEME_KEY) || 'slate';
}

function applyTheme(t) {
  if (!THEMES.includes(t)) t = 'slate';
  document.documentElement.setAttribute('data-theme', t);
  document.querySelectorAll('.theme-opt').forEach(b => {
    b.classList.toggle('active', b.getAttribute('data-t') === t);
  });
}

function setTheme(t) {
  localStorage.setItem(THEME_KEY, t);
  applyTheme(t);
  toast(`🎨 Theme: ${THEME_NAMES[t] || t}`);
}

function cycleTheme() {
  const cur = getSavedTheme();
  const nextIdx = (THEMES.indexOf(cur) + 1) % THEMES.length;
  setTheme(THEMES[nextIdx]);
}

window.setTheme = setTheme;
window.cycleTheme = cycleTheme;
applyTheme(getSavedTheme());

if ($('btn-theme')) {
  $('btn-theme').onclick = cycleTheme;
}

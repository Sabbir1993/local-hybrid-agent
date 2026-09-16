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
let agentMode = false;
let planMode = false;
let chatWebSearch = true;
try {
  const savedWeb = localStorage.getItem('chat_web_search');
  if (savedWeb !== null) chatWebSearch = savedWeb === '1';
} catch (e) {}

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

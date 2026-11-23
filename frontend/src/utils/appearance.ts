import { apiFetch } from '../services/api';

interface Appearance {
  mode: string;
  contrast: string;
  font: string;
  font_size: number;
  theme: string;
  background: string;
  foreground: string;
  accent: string;
}

export function applyAppearance(p: Appearance) {
  const root = document.documentElement;
  root.setAttribute('data-mode', p.mode);
  root.setAttribute('data-font', p.font);
  root.classList.toggle('contrast-strong', p.contrast === 'strong');
  root.style.setProperty('--bg', p.background);
  root.style.setProperty('--text', p.foreground);
  root.style.setProperty('--accent', p.accent);
  root.style.fontSize = `${p.font_size}px`;
  const effective = p.mode === 'light'
    ? (p.theme.includes('light') ? p.theme : 'light')
    : p.mode === 'dark' && p.theme.includes('light') ? 'slate' : p.theme;
  root.setAttribute('data-theme', effective);
  try { localStorage.setItem('a770_theme', effective); } catch (_) {}
}

/** Load the caller's saved appearance once at startup so a refresh keeps it. */
export async function loadAppearance() {
  try {
    const r = await apiFetch('/control/appearance', { background: true });
    if (r.ok) applyAppearance(await r.json());
  } catch (_) { /* offline: theme-boot.js fallback stays in place */ }
}

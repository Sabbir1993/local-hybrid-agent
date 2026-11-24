import { apiFetch } from '../services/api';

interface Appearance {
  conversation_steps: 'default' | 'verbose' | string;
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
  root.setAttribute('data-conversation-steps', p.conversation_steps || 'default');
  try {
    localStorage.setItem('a770_conversation_steps', p.conversation_steps || 'default');
    localStorage.setItem('a770_appearance', JSON.stringify(p));
  } catch (_) {}
  root.classList.toggle('contrast-strong', p.contrast === 'strong');
  // Background / Foreground / Accent are CODE-PREVIEW colours only. The theme
  // itself comes from the selected preset (data-theme) and must not be overridden.
  root.style.setProperty('--code-bg', p.background);
  root.style.setProperty('--code-fg', p.foreground);
  root.style.setProperty('--code-accent', p.accent);
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
    if (r.ok) {
      applyAppearance(await r.json());
    } else {
      const cached = readLocalAppearance();
      if (cached) applyAppearance(cached); else applyLocalSteps();
    }
  } catch (_) {
    const cached = readLocalAppearance();
    if (cached) applyAppearance(cached); else applyLocalSteps();
  }
}

function readLocalAppearance(): Appearance | null {
  try {
    const raw = localStorage.getItem('a770_appearance');
    return raw ? JSON.parse(raw) as Appearance : null;
  } catch (_) {
    return null;
  }
}

/** Apply the locally cached conversation-steps choice when the account API is unavailable. */
export function applyLocalSteps() {
  try {
    const v = localStorage.getItem('a770_conversation_steps');
    if (v === 'default' || v === 'verbose') {
      document.documentElement.setAttribute('data-conversation-steps', v);
    }
  } catch (_) {}
}

/** Change Conversation Steps immediately (chat UI + local cache) and persist best-effort. */
export async function setConversationSteps(value: 'default' | 'verbose') {
  document.documentElement.setAttribute('data-conversation-steps', value);
  try { localStorage.setItem('a770_conversation_steps', value); } catch (_) {}
  window.dispatchEvent(new CustomEvent('a770-appearance-changed', { detail: { conversation_steps: value } }));
  try {
    await apiFetch('/control/appearance', {
      method: 'PUT', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ conversation_steps: value }),
    });
  } catch (_) { /* local choice still applies this session */ }
}

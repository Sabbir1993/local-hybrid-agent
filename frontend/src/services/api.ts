import { logger } from '../utils/logger';
import { noteServerTouch } from './session';

function readCookie(name: string): string | null {
  if (typeof document === 'undefined') return null;
  const m = document.cookie.match(new RegExp('(?:^|; )' + name + '=([^;]*)'));
  return m ? decodeURIComponent(m[1]) : null;
}

/* Device identity: the server keys the active project by (user, device). Same ids as the legacy UI,
   so a browser that used both keeps the same device + projects. */
export function getClientDeviceId(): string {
  const w = window as any;
  if (w.electronAPI && w.electronAPI.deviceId) return String(w.electronAPI.deviceId);
  let id = localStorage.getItem('antigravity_device_id');
  if (!id) {
    id = 'dev_' + Math.random().toString(36).substring(2, 10) + '_' + Date.now().toString(36);
    try { localStorage.setItem('antigravity_device_id', id); } catch (_) {}
  }
  return id;
}

export function getClientDeviceName(): string {
  let name = localStorage.getItem('antigravity_device_name');
  if (!name) {
    const ua = navigator.userAgent || '';
    if (ua.includes('Windows')) name = 'Windows PC';
    else if (ua.includes('Macintosh') || ua.includes('Mac OS')) name = 'Mac';
    else if (ua.includes('Android')) name = 'Android';
    else if (ua.includes('iPhone') || ua.includes('iPad')) name = 'iOS Device';
    else if (ua.includes('Linux')) name = 'Linux PC';
    else name = 'My Device';
    try { localStorage.setItem('antigravity_device_name', name); } catch (_) {}
  }
  return name;
}

export function getDeviceHeaders(): Record<string, string> {
  return { 'X-Device-Id': getClientDeviceId(), 'X-Device-Name': getClientDeviceName() };
}

// Raw fetch() callers (not going through apiFetch) get the same headers on same-origin requests.
if (typeof window !== 'undefined' && !(window as any).__deviceFetchPatched) {
  (window as any).__deviceFetchPatched = true;
  const nativeFetch = window.fetch.bind(window);
  window.fetch = (input: RequestInfo | URL, init?: RequestInit) => {
    const url = typeof input === 'string' ? input : input instanceof URL ? input.href : (input as Request).url || '';
    const sameOrigin = (url.startsWith('/') && !url.startsWith('//')) || url.startsWith(location.origin + '/');
    if (!sameOrigin) return nativeFetch(input, init);
    const headers = new Headers(init?.headers || (input instanceof Request ? input.headers : undefined));
    const dev = getDeviceHeaders();
    for (const k of Object.keys(dev)) {
      try { if (!headers.has(k)) headers.set(k, dev[k]); } catch (_) {}
    }
    return nativeFetch(input, { ...(init || {}), headers });
  };
}

export async function apiFetch(input: RequestInfo | URL, rawInit: RequestInit & { background?: boolean } = {}): Promise<Response> {
  const init = { ...rawInit };   // never change the caller's object (a reused one would lose its flags)
  const method = (init.method || 'GET').toUpperCase();
  const headers = new Headers(init.headers || {});

  // Include credentials (cookies) always
  init.credentials = 'include';

  // Attach CSRF token on mutating requests
  if (method !== 'GET' && method !== 'HEAD') {
    const csrf = readCookie('a770_csrf');
    if (csrf) {
      headers.set('X-CSRF-Token', csrf);
    }
  }

  // background polls must not slide the server-side idle expiry
  if (init.background) {
    headers.set('X-A770-Background', '1');
    delete init.background;
  } else {
    noteServerTouch();
  }

  init.headers = headers;

  const resp = await fetch(input, init);

  if (resp.status === 401 && !String(input).includes('/auth/login') && !String(input).includes('/auth/me')) {
    logger.warn('Auth', '401 Unauthorized from backend API');
    if (typeof window !== 'undefined' && (window as any).__onAuthRequired) {
      (window as any).__onAuthRequired();
    }
  }

  return resp;
}

export interface UserProfile {
  id: string;
  username: string;
  display_name?: string;
  organization?: string;
  is_super_admin?: boolean;
  must_change_password?: boolean;
  roles: string[];
  permissions: string[];
}

export interface AvailableModelItem {
  id: string;
  display: string;
  kind?: 'local' | 'cloud';
  provider?: string;
  provider_name?: string;
  currently_loaded?: boolean;
  ctx?: number;
  context_size?: number;
  reasoning_available?: boolean;
  reasoning_mode?: string;
}

export interface ServerStatusPayload {
  pid?: number | null;
  healthy?: boolean;
  model?: string;
  loading?: boolean;
  tensor_split?: string;
  measured_tg_tokens_per_sec?: number;
  vision_capable?: boolean;
  cloud_main?: {
    key: string;
    display: string;
    provider: string;
  } | null;
  context?: {
    n_ctx?: number;
  };
  gpus?: Array<{
    id: number;
    name: string;
    used_mb: number;
    total_mb: number;
  }>;
  ram?: {
    used_mb: number;
    total_mb: number;
  };
}

export interface SessionRecord {
  id: string | number;
  name?: string;
  title?: string;
  created_at?: string;
  updated_at?: string;
}

export const api = {
  auth: {
    /** null = not signed in (401/403); a server or network error is retried briefly before that is assumed */
    getMe: async (): Promise<UserProfile | null> => {
      try {
        let res: Response | null = null;
        for (let attempt = 0; attempt < 3; attempt++) {
          try {
            res = await apiFetch('/auth/me');
            if (res.status < 500) break;
          } catch (_) {
            res = null;
          }
          await new Promise((r) => setTimeout(r, 800 * (attempt + 1)));
        }
        if (!res || !res.ok) return null;
        const data = await res.json();
        const u = data.user || data;
        return {
          id: String(u.id || ''),
          username: u.username || '',
          display_name: u.display_name || u.username || '',
          organization: u.organization || 'SSLWIRELESS',
          is_super_admin: Boolean(u.is_super_admin),
          must_change_password: Boolean(u.must_change_password),
          roles: data.roles || u.roles || [],
          permissions: data.permissions || u.permissions || [],
        };
      } catch (err) {
        return null;
      }
    },

    login: async (username: string, password: string): Promise<{ ok: boolean; error?: string; user?: UserProfile; mfaRequired?: boolean; enrollRequired?: boolean; ticket?: string }> => {
      try {
        const res = await apiFetch('/auth/login', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ username, password }),
        });
        const data = await res.json().catch(() => ({}));
        if (!res.ok) {
          return { ok: false, error: data.detail || 'Sign in failed' };
        }
        if (data.mfa_required) {
          return { ok: true, mfaRequired: true, ticket: data.ticket, enrollRequired: Boolean(data.enroll_required) };
        }
        const u = data.user || {};
        const profile: UserProfile = {
          must_change_password: Boolean(u.must_change_password),
          id: String(u.id || ''),
          username: u.username || username,
          display_name: u.display_name || u.username || username,
          organization: u.organization || 'SSLWIRELESS',
          is_super_admin: Boolean(u.is_super_admin),
          roles: data.roles || [],
          permissions: data.permissions || [],
        };
        return { ok: true, user: profile };
      } catch (err: any) {
        return { ok: false, error: err.message || 'Network error' };
      }
    },

    changePassword: async (currentPassword: string, newPassword: string): Promise<{ ok: boolean; error?: string }> => {
      try {
        const res = await apiFetch('/auth/change_password', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ current_password: currentPassword, new_password: newPassword }),
        });
        const data = await res.json().catch(() => ({}));
        if (!res.ok) return { ok: false, error: data.detail || 'Failed to change password' };
        return { ok: true };
      } catch (err: any) {
        return { ok: false, error: err.message || 'Network error' };
      }
    },

    /** idle sign-out policy from /auth/me */
    getSessionPolicy: async (): Promise<{ idleSeconds?: number; idleEnabled?: boolean }> => {
      try {
        const res = await apiFetch('/auth/me', { background: true });
        if (!res.ok) return {};
        const d = await res.json();
        return { idleSeconds: d.idle_seconds, idleEnabled: d.idle_enabled };
      } catch (_) {
        return {};
      }
    },

    logout: async (): Promise<boolean> => {
      try {
        const res = await apiFetch('/auth/logout', { method: 'POST' });
        return res.ok;
      } catch (_) {
        return false;
      }
    },
  },

  control: {
    getStatus: async (): Promise<ServerStatusPayload | null> => {
      try {
        const res = await apiFetch('/control/status');
        if (!res.ok) return null;
        return await res.json();
      } catch (_) {
        return null;
      }
    },

    getAvailableModels: async (): Promise<AvailableModelItem[]> => {
      try {
        const res = await apiFetch('/control/available_models');
        if (!res.ok) return [];
        const data = await res.json();
        return data.models || [];
      } catch (err) {
        return [];
      }
    },

    loadModel: async (modelId: string): Promise<boolean> => {
      try {
        const res = await apiFetch('/control/load', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ profile: modelId }),
        });
        return res.ok;
      } catch (err) {
        return false;
      }
    },

    stopModel: async (): Promise<boolean> => {
      try {
        const res = await apiFetch('/control/stop', {
          method: 'POST',
        });
        return res.ok;
      } catch (err) {
        return false;
      }
    },

    switchModel: async (target: string): Promise<any> => {
      try {
        const res = await apiFetch('/control/switch', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ target }),
        });
        return res.ok ? await res.json() : null;
      } catch (err) {
        return null;
      }
    },
  },

  projects: {
    list: async (): Promise<any[]> => {
      try {
        const res = await apiFetch('/control/projects?device=all');
        if (!res.ok) return [];
        const data = await res.json();
        return data.projects || [];
      } catch (_) {
        return [];
      }
    },

    delete: async (id: string | number): Promise<boolean> => {
      try {
        const res = await apiFetch(`/control/projects/${id}`, { method: 'DELETE' });
        return res.ok;
      } catch (_) {
        return false;
      }
    },

    activate: async (id: string | number): Promise<boolean> => {
      try {
        const res = await apiFetch(`/control/projects/${id}/activate`, { method: 'POST' });
        return res.ok;
      } catch (_) {
        return false;
      }
    },
  },

  sessions: {
    list: async (projectId: string | number = 0, limit = 50, before?: string | number): Promise<{ sessions: SessionRecord[]; hasMore: boolean }> => {
      try {
        let url = `/control/projects/${projectId}/sessions?limit=${limit}`;
        if (before !== undefined && before !== null) {
          url += `&before=${encodeURIComponent(String(before))}`;
        }
        const res = await apiFetch(url);
        if (!res.ok) return { sessions: [], hasMore: false };
        const data = await res.json();
        return {
          sessions: data.sessions || [],
          hasMore: Boolean(data.has_more),
        };
      } catch (_) {
        return { sessions: [], hasMore: false };
      }
    },

    getMessages: async (sessionId: string | number): Promise<any[]> => {
      try {
        const res = await apiFetch(`/control/sessions/${sessionId}/messages`);
        if (!res.ok) return [];
        const data = await res.json();
        return data.messages || [];
      } catch (_) {
        return [];
      }
    },

    create: async (title = 'New Chat Session', projectId: string | number = 0, agentId?: string | number): Promise<any> => {
      try {
        const body: Record<string, any> = { title };
        if (agentId != null) body.agent_id = agentId;
        const res = await apiFetch(`/control/projects/${projectId}/sessions`, {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify(body),
        });
        if (!res.ok) return null;
        return await res.json();
      } catch (_) {
        return null;
      }
    },

    delete: async (sessionId: string | number): Promise<boolean> => {
      try {
        const res = await apiFetch(`/control/sessions/${sessionId}`, {
          method: 'DELETE',
        });
        return res.ok;
      } catch (_) {
        return false;
      }
    },

    persistMessage: async (sessionId: string | number, role: string, content: string, meta?: any): Promise<boolean> => {
      try {
        const res = await apiFetch(`/control/sessions/${sessionId}/messages`, {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ role, content, meta }),
        });
        return res.ok;
      } catch (_) {
        return false;
      }
    },
  },
};

/** "Windows PC-ABC12DEF3456": device name plus a short readable id (same label as the legacy UI). */
export function getDeviceDisplayLabel(): string {
  const short = getClientDeviceId().replace(/^dev_/, '').replace(/_/g, '').slice(0, 12).toUpperCase();
  return `${getClientDeviceName()}-${short}`;
}

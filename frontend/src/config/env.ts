/**
 * Typed environment configuration loaded from Vite .env files.
 * Provides fallback defaults and type-safe access throughout the React application.
 */

export interface AppEnvConfig {
  projectName: string;
  appDescription: string;
  version: string;
  apiBaseUrl: string;
  wsUrl: string;
  orgDomain: string;
  logLevel: 'debug' | 'info' | 'warn' | 'error' | 'none';
  logMaxBuffer: number;
  logRemoteSync: boolean;
  features: {
    voice: boolean;
    mediaLanes: boolean;
    mcp: boolean;
    claudeDock: boolean;
  };
  isDev: boolean;
  isProd: boolean;
}

const parseBool = (value: string | undefined, defaultValue: boolean): boolean => {
  if (value === undefined || value === '') return defaultValue;
  return value.toLowerCase() === 'true' || value === '1';
};

const parseLogLevel = (value: string | undefined): AppEnvConfig['logLevel'] => {
  const normalized = (value || '').toLowerCase().trim();
  if (normalized === 'debug' || normalized === 'info' || normalized === 'warn' || normalized === 'error' || normalized === 'none') {
    return normalized;
  }
  return import.meta.env.DEV ? 'debug' : 'info';
};

export const env: AppEnvConfig = {
  projectName: import.meta.env.VITE_PROJECT_NAME || 'Vulkan Arc',
  appDescription: import.meta.env.VITE_APP_DESCRIPTION || 'A770 Dual-Runtime Workstation & Multi-Agent Environment',
  version: import.meta.env.VITE_APP_VERSION || '1.0.0',
  apiBaseUrl: import.meta.env.VITE_API_BASE_URL || '',
  wsUrl: import.meta.env.VITE_WS_URL || '',
  orgDomain: (import.meta.env.VITE_ORG_DOMAIN || '').trim().replace(/^@/, '').toLowerCase(),
  logLevel: parseLogLevel(import.meta.env.VITE_LOG_LEVEL),
  logMaxBuffer: Number(import.meta.env.VITE_LOG_MAX_BUFFER) || 500,
  logRemoteSync: parseBool(import.meta.env.VITE_LOG_REMOTE_SYNC, false),
  features: {
    voice: parseBool(import.meta.env.VITE_ENABLE_VOICE, true),
    mediaLanes: parseBool(import.meta.env.VITE_ENABLE_MEDIA_LANES, true),
    mcp: parseBool(import.meta.env.VITE_ENABLE_MCP, true),
    claudeDock: parseBool(import.meta.env.VITE_ENABLE_CLAUDE_DOCK, true),
  },
  isDev: import.meta.env.DEV,
  isProd: import.meta.env.PROD,
};

/** "first.last" -> "first.last@<org domain>". A value that already has an @ is left alone; no domain configured = unchanged. */
export function withOrgDomain(localPart: string): string {
  const v = (localPart || '').trim();
  if (!v || v.includes('@') || !env.orgDomain) return v;
  return `${v}@${env.orgDomain}`;
}

if (typeof window !== 'undefined') {
  // read by the embedded Settings page (Users & Roles -> create user)
  (window as any).__ORG_DOMAIN__ = env.orgDomain;
}

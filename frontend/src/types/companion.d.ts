export interface ElectronBridgeAPI {
  isNativeApp: boolean;
  deviceId: string | null;
  getDeviceId: () => Promise<string | null>;
  browseFolder: (initialDir?: string) => Promise<string | null>;
  browseDir: (targetPath: string) => Promise<any>;
  mkdir: (basePath: string, name: string) => Promise<{ ok: boolean; path?: string; error?: string }>;
  isMinimized: () => Promise<boolean>;
  flashFrame: (flag: boolean) => Promise<void>;
  notify: (opts: { title: string; body: string }) => Promise<void>;
}

declare global {
  interface Window {
    electronAPI?: ElectronBridgeAPI;
  }
}

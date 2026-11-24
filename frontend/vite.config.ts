import { defineConfig, loadEnv } from 'vite';
import react from '@vitejs/plugin-react';
import path from 'path';

const BACKEND_URL = 'http://127.0.0.1:8000';
const BACKEND_WS = 'ws://127.0.0.1:8000';

let mode = 'production';

// Frontend-owned routes must be served by the SPA shell on a hard refresh.
// Without this, /settings is proxied to the backend, which 404s and redirects to "/",
// so the Settings view (and its nav bar) disappears after refresh.
const spaBypass = (req: any) => {
  const full = String(req.url || '');
  const url = full.split('?')[0];
  // The Settings modal loads the legacy settings.html inside an iframe via
  // /settings?frame=1 — that request must still be proxied to the backend.
  if (full.includes('frame=1')) return undefined;
  if (['/settings', '/login', '/report', '/audit', '/docs'].includes(url) ||
      (req.headers?.accept || '').includes('text/html')) {
    return '/index.html';
  }
};

const proxyConfig = {
  '/auth': { target: BACKEND_URL, changeOrigin: true },
  '/control': { target: BACKEND_URL, changeOrigin: true },
  '/projects': { target: BACKEND_URL, changeOrigin: true },
  '/capabilities': { target: BACKEND_URL, changeOrigin: true },
  '/cloud': { target: BACKEND_URL, changeOrigin: true },
  '/lanes': { target: BACKEND_URL, changeOrigin: true },
  '/media': { target: BACKEND_URL, changeOrigin: true },
  '/chat': { target: BACKEND_URL, changeOrigin: true },
  '/agent': { target: BACKEND_URL, changeOrigin: true },
  '/tasks': { target: BACKEND_URL, changeOrigin: true },
  '/git': { target: BACKEND_URL, changeOrigin: true },
  '/mcp': { target: BACKEND_URL, changeOrigin: true },
  '/plugins': { target: BACKEND_URL, changeOrigin: true },
  '/customize': { target: BACKEND_URL, changeOrigin: true },
  '/knowledge': { target: BACKEND_URL, changeOrigin: true },
  '/db': {
    target: BACKEND_URL,
    changeOrigin: true,
    bypass: (req: any) => {
      if (req.url === '/db' || req.url === '/db/' || req.headers?.accept?.includes('text/html')) {
        return '/index.html';
      }
    },
  },
  '/admin': { target: BACKEND_URL, changeOrigin: true },
  '/custom_agents': { target: BACKEND_URL, changeOrigin: true },
  '/custom-agents': { target: BACKEND_URL, changeOrigin: true },
  '/api-tokens': { target: BACKEND_URL, changeOrigin: true },
  '/sandbox': { target: BACKEND_URL, changeOrigin: true },
  '/companion': { target: BACKEND_URL, changeOrigin: true },
  '/v1': { target: BACKEND_URL, changeOrigin: true },
  '/static': { target: BACKEND_URL, changeOrigin: true },
  '/settings': { target: BACKEND_URL, changeOrigin: true, bypass: spaBypass },
  '/login': { target: BACKEND_URL, changeOrigin: true, bypass: spaBypass },
  '/docs': { target: BACKEND_URL, changeOrigin: true, bypass: spaBypass },
  '/ws': {
    target: BACKEND_WS,
    ws: true,
    configure: (proxy: any) => {
      proxy.on('error', () => {
        // Suppress benign connection errors
      });
      proxy.on('proxyReqWsError', () => {
        // Suppress WebSocket proxy errors (ECONNRESET/ECONNABORTED)
      });
      proxy.on('open', (proxySocket: any) => {
        if (proxySocket && typeof proxySocket.on === 'function') {
          proxySocket.on('error', () => {
            // Suppress underlying socket aborts on client reload
          });
        }
      });
    },
  },
};

export default defineConfig({
  plugins: [react(), {
    // the tab title before the JS runs: VITE_PROJECT_NAME from frontend/.env, or the same fallback env.ts uses
    name: 'app-title',
    configResolved: (c) => { mode = c.mode; },
    transformIndexHtml: (html) =>
      html.replaceAll('%VITE_PROJECT_NAME%', loadEnv(mode, import.meta.dirname, 'VITE_').VITE_PROJECT_NAME || 'Local Agent'),
  }],
  resolve: {
    alias: {
      '@': path.resolve(import.meta.dirname, './src'),
    },
  },
  server: {
    port: 5173,
    allowedHosts: true,
    proxy: proxyConfig,
  },
  preview: {
    port: 3000,
    host: '0.0.0.0',
    allowedHosts: true,
    proxy: proxyConfig,
  },
  build: {
    // the audio worklet must stay a real file: the page policy only allows scripts from the same origin (no data: URLs)
    assetsInlineLimit: (file) => (file.endsWith('voice-worklet.js') ? false : undefined),
    outDir: 'dist',
    emptyOutDir: true,
    sourcemap: true,
  },
});

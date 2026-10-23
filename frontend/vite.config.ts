import { defineConfig } from 'vite';
import react from '@vitejs/plugin-react';
import path from 'path';

const BACKEND_URL = 'http://127.0.0.1:8000';
const BACKEND_WS = 'ws://127.0.0.1:8000';

export default defineConfig({
  plugins: [react()],
  resolve: {
    alias: {
      '@': path.resolve(import.meta.dirname, './src'),
    },
  },
  server: {
    port: 5173,
    proxy: {
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
      '/settings': { target: BACKEND_URL, changeOrigin: true },
      '/login': { target: BACKEND_URL, changeOrigin: true },
      '/docs': { target: BACKEND_URL, changeOrigin: true },
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
    },
  },
  build: {
    // the audio worklet must stay a real file: the page policy only allows scripts from the same origin (no data: URLs)
    assetsInlineLimit: (file) => (file.endsWith('voice-worklet.js') ? false : undefined),
    outDir: 'dist',
    emptyOutDir: true,
    sourcemap: true,
  },
});

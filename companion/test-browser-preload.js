// Renderer bridge for the Agent Test Browser window.
const { contextBridge, ipcRenderer } = require("electron");

contextBridge.exposeInMainWorld("testBrowser", {
  onInit: (cb) => ipcRenderer.on("testbrowser:init", (_e, url) => cb(url)),
  onNavigate: (cb) => ipcRenderer.on("testbrowser:navigate", (_e, url) => cb(url)),
});

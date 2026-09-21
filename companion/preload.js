// companion/preload.js — Secure bridge from Electron to the web app
const { contextBridge, ipcRenderer } = require("electron");

contextBridge.exposeInMainWorld("electronAPI", {
  isNativeApp: true,
  browseFolder: (initialDir) => ipcRenderer.invoke("dialog:browseFolder", initialDir),
  browseDir: (targetPath) => ipcRenderer.invoke("fs:browse", targetPath),
  mkdir: (basePath, name) => ipcRenderer.invoke("fs:mkdir", { path: basePath, name }),
});

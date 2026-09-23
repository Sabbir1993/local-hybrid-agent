// companion/preload.js — Secure bridge from Electron to the web app
const { contextBridge, ipcRenderer } = require("electron");

let nativeDeviceId = null;
try {
  nativeDeviceId = ipcRenderer.sendSync("system:getDeviceIdSync");
} catch (_) {}

contextBridge.exposeInMainWorld("electronAPI", {
  isNativeApp: true,
  deviceId: nativeDeviceId,
  getDeviceId: () => ipcRenderer.invoke("system:getDeviceId"),
  browseFolder: (initialDir) => ipcRenderer.invoke("dialog:browseFolder", initialDir),
  browseDir: (targetPath) => ipcRenderer.invoke("fs:browse", targetPath),
  mkdir: (basePath, name) => ipcRenderer.invoke("fs:mkdir", { path: basePath, name }),
});

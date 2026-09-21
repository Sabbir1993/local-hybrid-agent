// companion/main.js — Electron main process: local companion for the
// Local Agent server. Shows the server's own web app in a native
// window (login included — "/" redirects to "/login" server-side when
// unauthenticated), and holds a WebSocket to core/companion_bridge.py to
// execute fs/shell RPCs from fsops.js / shellops.js on this machine.

const { app, BrowserWindow, Tray, Menu, nativeImage, session: electronSession, dialog, ipcMain } = require("electron");
const WebSocket = require("ws");
const os = require("os");
const path = require("path");
const { autoUpdater } = require("electron-updater");


const { SERVER_URL } = require("./config");
const fsops = require("./fsops");
const shellops = require("./shellops");

const APP_ICON_PATH = path.join(__dirname, "build", "icon.png");
const appIcon = nativeImage.createFromPath(APP_ICON_PATH);
const trayIconImage = appIcon.resize({ width: 32, height: 32 });

const SESSION_COOKIE_NAME = "a770_session";
const RECONNECT_BASE_MS = 2000;
const RECONNECT_MAX_MS = 30000;

process.on("uncaughtException", (e) => console.error("[uncaughtException]", e));
process.on("unhandledRejection", (e) => console.error("[unhandledRejection]", e));

let tray = null;
let mainWindow = null;
let ws = null;
let reconnectDelay = RECONNECT_BASE_MS;
let reconnectTimer = null;
let connected = false;
let isQuitting = false;
let lastToken = null;

function setTrayStatus(text, connectedNow) {
  connected = connectedNow;
  if (!tray) return;
  tray.setToolTip(`A770 Companion — ${text}`);
  tray.setContextMenu(
    Menu.buildFromTemplate([
      { label: connectedNow ? "🟢 Connected" : "🔴 Not connected", enabled: false },
      { label: "Show App", click: showMainWindow },
      { type: "separator" },
      {
        label: "Quit",
        click: () => {
          isQuitting = true;
          app.quit();
        },
      },
    ])
  );
}

async function getSessionCookie() {
  const ses = (mainWindow && mainWindow.webContents && mainWindow.webContents.session) || electronSession.defaultSession;
  let currentUrl = (mainWindow && mainWindow.webContents && mainWindow.webContents.getURL()) || SERVER_URL;
  if (!currentUrl || currentUrl === "about:blank") currentUrl = SERVER_URL;

  try {
    const cookies = await ses.cookies.get({ url: currentUrl, name: SESSION_COOKIE_NAME });
    if (cookies && cookies.length) return cookies[0].value;
  } catch (_) {}

  try {
    const cookies = await ses.cookies.get({ name: SESSION_COOKIE_NAME });
    if (cookies && cookies.length) return cookies[0].value;
  } catch (_) {}

  try {
    const cookies = await electronSession.defaultSession.cookies.get({ url: currentUrl, name: SESSION_COOKIE_NAME });
    if (cookies && cookies.length) return cookies[0].value;
  } catch (_) {}

  try {
    const cookies = await electronSession.defaultSession.cookies.get({ name: SESSION_COOKIE_NAME });
    if (cookies && cookies.length) return cookies[0].value;
  } catch (_) {}

  try {
    const all = await ses.cookies.get({ url: currentUrl });
    for (const c of all) {
      if (c.name === SESSION_COOKIE_NAME || c.name.includes("session")) return c.value;
    }
  } catch (_) {}

  return null;
}

function showMainWindow() {
  if (!mainWindow) return;
  mainWindow.show();
  mainWindow.focus();
}

function createMainWindow() {
  const win = new BrowserWindow({
    width: 1280,
    height: 860,
    title: "Local Agent",
    icon: appIcon,
    webPreferences: {
      contextIsolation: true,
      nodeIntegration: false,
      preload: path.resolve(__dirname, "preload.js"),
    },
  });

  try {
    const customUa = win.webContents.getUserAgent() + " A770NativeApp";
    win.webContents.setUserAgent(customUa);
  } catch (_) {}

  win.webContents.on("did-fail-load", (e, code, desc, url) => {
    console.error("[main window] did-fail-load", code, desc, url);
    if (url && url.startsWith("https://") && !url.includes("127.0.0.1") && !url.includes("localhost")) {
      console.log("[main window] Remote failed, trying local server http://127.0.0.1:8000/...");
      win.loadURL("http://127.0.0.1:8000/");
    }
  });

  const checkLoggedIn = async () => {
    const token = await getSessionCookie();
    if (token && (!ws || ws.readyState !== WebSocket.OPEN || token !== lastToken)) {
      lastToken = token;
      connectWebSocket(token);
    }
  };
  win.webContents.on("did-navigate", checkLoggedIn);
  win.webContents.on("did-navigate-in-page", checkLoggedIn);
  win.webContents.on("did-finish-load", checkLoggedIn);
  setInterval(checkLoggedIn, 3000);

  win.on("close", (e) => {
    if (!isQuitting) {
      e.preventDefault();
      win.hide();
    }
  });

  win.loadURL(SERVER_URL + "/");
  return win;
}

// Register IPC handlers for direct native folder browsing from the web app
ipcMain.handle("dialog:browseFolder", async (event, initialDir) => {
  try {
    return (await fsops.browseFolder({ initial_dir: initialDir })).path || "";
  } catch (e) {
    console.error("[ipcMain dialog:browseFolder] error:", e);
    return "";
  }
});

ipcMain.handle("fs:browse", async (event, targetPath) => {
  try {
    return fsops.browse({ path: targetPath });
  } catch (e) {
    console.error("[ipcMain fs:browse] error:", e);
    return { ok: false, error: String(e) };
  }
});

ipcMain.handle("fs:mkdir", async (event, args) => {
  try {
    return fsops.mkdir(args || {});
  } catch (e) {
    console.error("[ipcMain fs:mkdir] error:", e);
    return { ok: false, error: String(e) };
  }
});

function scheduleReconnect() {
  if (reconnectTimer) return;
  reconnectTimer = setTimeout(async () => {
    reconnectTimer = null;
    const token = await getSessionCookie();
    if (token) connectWebSocket(token);
  }, reconnectDelay);
  reconnectDelay = Math.min(reconnectDelay * 2, RECONNECT_MAX_MS);
}

const OPS = {
  "fs.browse_folder": fsops.browseFolder,
  "fs.browse": fsops.browse,
  "fs.mkdir": fsops.mkdir,
  "fs.read": fsops.read,
  "fs.write": fsops.write,
  "fs.edit": fsops.edit,
  "fs.list": fsops.list,
  "fs.grep": fsops.grep,
  "fs.tree": fsops.tree,
  "shell.run": shellops.run,
};

async function handleCall(frame) {
  const impl = OPS[frame.op];
  if (!impl) {
    return { type: "result", req_id: frame.req_id, ok: false, error: `unknown op: ${frame.op}` };
  }
  try {
    const data = await impl(frame.params || {});
    return { type: "result", req_id: frame.req_id, ok: true, data };
  } catch (e) {
    return { type: "result", req_id: frame.req_id, ok: false, error: String((e && e.message) || e) };
  }
}

function connectWebSocket(sessionToken) {
  if (ws) {
    try {
      ws.terminate();
    } catch {}
    ws = null;
  }
  let effectiveServer = SERVER_URL;
  if (mainWindow && mainWindow.webContents) {
    try {
      const pageUrl = mainWindow.webContents.getURL();
      if (pageUrl && pageUrl.startsWith("http")) {
        const u = new URL(pageUrl);
        effectiveServer = `${u.protocol}//${u.host}`;
      }
    } catch (_) {}
  }
  const wsBase = effectiveServer.replace(/^http/, "ws") + "/ws/companion";
  const sep = wsBase.includes("?") ? "&" : "?";
  const wsUrl = `${wsBase}${sep}token=${encodeURIComponent(sessionToken)}`;
  console.log("[connectWebSocket] connecting to", wsUrl);
  ws = new WebSocket(wsUrl, {
    headers: {
      Cookie: `${SESSION_COOKIE_NAME}=${sessionToken}`,
      Authorization: `Bearer ${sessionToken}`,
      "ngrok-skip-browser-warning": "true",
      "User-Agent": "A770Companion A770NativeApp"
    }
  });

  ws.on("unexpected-response", (req, res) => {
    console.error("[connectWebSocket] unexpected-response", res.statusCode);
    setTrayStatus("connection error — retrying…", false);
    scheduleReconnect();
  });

  ws.on("open", () => {
    console.log("[connectWebSocket] open");
    reconnectDelay = RECONNECT_BASE_MS;
    ws.send(JSON.stringify({ type: "hello", hostname: os.hostname() }));
    setTrayStatus(`connected to ${effectiveServer}`, true);
  });

  ws.on("message", async (raw) => {
    let frame;
    try {
      frame = JSON.parse(raw.toString());
    } catch {
      return;
    }
    if (frame.type === "call") {
      const result = await handleCall(frame);
      ws.send(JSON.stringify(result));
    }
  });

  ws.on("close", (code, reason) => {
    console.log("[connectWebSocket] closed", code, reason.toString());
    setTrayStatus("disconnected — retrying…", false);
    scheduleReconnect();
  });

  ws.on("error", (err) => {
    console.error("[connectWebSocket] error", err.message);
    setTrayStatus("connection error — retrying…", false);
  });
}

app.whenReady().then(() => {
  tray = new Tray(trayIconImage);
  tray.on("click", showMainWindow);
  setTrayStatus("starting…", false);

  autoUpdater.checkForUpdatesAndNotify().catch(() => {});

  mainWindow = createMainWindow();

  getSessionCookie()
    .then((token) => {
      console.log("[whenReady] getSessionCookie resolved:", token);
      if (token) {
        lastToken = token;
        connectWebSocket(token);
      }
    })
    .catch((e) => console.error("[whenReady] getSessionCookie failed:", e));
});

app.on("window-all-closed", (e) => {
  // Companion lives in the tray; closing the window must not quit it.
  if (!isQuitting) e.preventDefault?.();
});

app.on("before-quit", () => {
  isQuitting = true;
});

// companion/main.js — Electron main process: local companion for the
// Local Agent server. Shows the server's own web app in a native
// window (login included — "/" redirects to "/login" server-side when
// unauthenticated), and holds a WebSocket to core/companion_bridge.py to
// execute fs/shell RPCs from fsops.js / shellops.js on this machine.

const { app, BrowserWindow, Tray, Menu, nativeImage, session: electronSession, dialog, ipcMain, shell, safeStorage } = require("electron");
const crypto = require("crypto");
const WebSocket = require("ws");
const os = require("os");
const path = require("path");
const { autoUpdater } = require("electron-updater");


const { SERVER_URL, SERVER_URL_ERROR } = require("./config");
const fsops = require("./fsops");
const shellops = require("./shellops");
const policy = require("./policy");

const APP_ICON_PATH = path.join(__dirname, "build", "icon.png");
const appIcon = nativeImage.createFromPath(APP_ICON_PATH);
const trayIconImage = appIcon.resize({ width: 32, height: 32 });

const SESSION_COOKIE_NAME = "a770_session";
const CSRF_COOKIE_NAME = "a770_csrf";
const RECONNECT_BASE_MS = 2000;
const RECONNECT_MAX_MS = 30000;
const KEEPALIVE_MS = 25000;        // client ping; a link with no pong by the next tick is dead
const CLOSE_REPLACED = 4409;       // server: another companion signed in as this user took over
const CLOSE_UNAUTHORIZED = 4401;   // server: session cookie / device key invalid or revoked
const CLOSE_WRONG_DEVICE = 4403;   // server: device key used from a different machine

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
let keepaliveTimer = null;
let parkedToken = null;   // token we stopped reconnecting with (4401 / 4409) until it changes

function setTrayStatus(text, connectedNow) {
  connected = connectedNow;
  if (!tray) return;
  tray.setToolTip(`A770 Companion — ${text}`);
  tray.setContextMenu(
    Menu.buildFromTemplate([
      { label: connectedNow ? "🟢 Connected" : "🔴 Not connected", enabled: false },
      { label: "Show App", click: showMainWindow },
      { label: "Reconnect", click: reconnectNow, enabled: !connectedNow },
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

// ---- device pairing ----
// After the user signs in here, the companion trades the browser session for its own
// device key (POST /companion/pair). The socket then authenticates with that key: it
// is bound to this machine, survives the 15-minute session idle timeout, and can be
// revoked on its own from Settings. Stored encrypted with the OS keychain (safeStorage).
function deviceKeyPath() {
  return path.join(app.getPath("userData"), "device-key.bin");
}

function loadDeviceKey() {
  try {
    const fs = require("fs");
    if (!safeStorage.isEncryptionAvailable() || !fs.existsSync(deviceKeyPath())) return null;
    const key = safeStorage.decryptString(fs.readFileSync(deviceKeyPath()));
    return key.startsWith("a770_dev_") ? key : null;
  } catch (e) {
    console.error("[deviceKey] unreadable:", e.message);
    return null;
  }
}

function saveDeviceKey(key) {
  try {
    if (!safeStorage.isEncryptionAvailable()) return false;   // never store it in plaintext
    require("fs").writeFileSync(deviceKeyPath(), safeStorage.encryptString(key), { mode: 0o600 });
    return true;
  } catch (e) {
    console.error("[deviceKey] save failed:", e.message);
    return false;
  }
}

function clearDeviceKey() {
  try { require("fs").unlinkSync(deviceKeyPath()); } catch (_) {}
}

async function getServerCookie(name) {
  try {
    const cookies = await electronSession.defaultSession.cookies.get({ url: SERVER_URL, name });
    return cookies && cookies.length ? cookies[0].value : null;
  } catch (_) {
    return null;
  }
}

async function pairDevice(sessionToken) {
  const csrf = await getServerCookie(CSRF_COOKIE_NAME);
  if (!csrf) return null;
  try {
    const r = await fetch(SERVER_URL + "/companion/pair", {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        Cookie: `${SESSION_COOKIE_NAME}=${sessionToken}; ${CSRF_COOKIE_NAME}=${csrf}`,
        "X-CSRF-Token": csrf,
        "User-Agent": "A770Companion A770NativeApp",
      },
      body: JSON.stringify({ device_id: getMachineId(), name: os.hostname() }),
      redirect: "error",
    });
    if (!r.ok) {
      console.warn("[pairDevice] server refused pairing:", r.status);
      return null;
    }
    const d = await r.json();
    if (!d.device_key || !saveDeviceKey(d.device_key)) return null;
    console.log("[pairDevice] paired as device", d.id);
    return d.device_key;
  } catch (e) {
    console.error("[pairDevice] failed:", e.message);
    return null;
  }
}

// The session cookie is only ever read for the configured server origin -- never for
// whatever page the window happens to show (a followed link must not receive it).
async function getSessionCookie() {
  const sessions = [];
  if (mainWindow && mainWindow.webContents) sessions.push(mainWindow.webContents.session);
  sessions.push(electronSession.defaultSession);
  for (const ses of sessions) {
    try {
      const cookies = await ses.cookies.get({ url: SERVER_URL, name: SESSION_COOKIE_NAME });
      if (cookies && cookies.length) return cookies[0].value;
    } catch (_) {}
  }
  return null;
}

function isServerUrl(url) {
  try {
    return new URL(url).origin === SERVER_URL;
  } catch (_) {
    return false;
  }
}

function openExternally(url) {
  try {
    const u = new URL(url);
    if (u.protocol === "https:" || u.protocol === "http:") shell.openExternal(u.href);
  } catch (_) {}
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
    // No fallback to another origin (it used to try plaintext http://127.0.0.1:8000,
    // handing the session to whatever listens there).
    console.error("[main window] did-fail-load", code, desc, url);
  });

  // The window only ever shows the configured server; links elsewhere (chat
  // output, model-written HTML) open in the system browser instead.
  win.webContents.on("will-navigate", (e, url) => {
    if (!isServerUrl(url)) {
      e.preventDefault();
      openExternally(url);
    }
  });
  win.webContents.on("will-redirect", (e, url) => {
    if (!isServerUrl(url)) e.preventDefault();
  });
  win.webContents.setWindowOpenHandler(({ url }) => {
    if (isServerUrl(url)) return { action: "allow" };
    openExternally(url);
    return { action: "deny" };
  });

  // Only (re)connect when there is no live socket, or the user signed in again.
  // A socket that is still CONNECTING must be left alone: replacing it here used
  // to feed an endless terminate/reconnect loop that dropped in-flight tool calls.
  const checkLoggedIn = async () => {
    const token = await getSessionCookie();
    if (!token) return;
    const changed = token !== lastToken;
    if (!changed && token === parkedToken) return;
    const idle = !ws || ws.readyState === WebSocket.CLOSED;
    if (changed || (idle && !reconnectTimer)) {
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

// IPC is callable only from the server's own pages, never from a frame of another origin.
function fromServer(event) {
  const url = (event.senderFrame && event.senderFrame.url) || "";
  if (!isServerUrl(url)) {
    console.warn("[ipc] rejected call from", url);
    return false;
  }
  return true;
}

// Register IPC handlers for direct native folder browsing from the web app
ipcMain.handle("dialog:browseFolder", async (event, initialDir) => {
  if (!fromServer(event)) return "";
  try {
    const picked = (await fsops.browseFolder({ initial_dir: initialDir })).path || "";
    if (picked) policy.approveRoot(picked);   // the user chose it in a native dialog
    return picked;
  } catch (e) {
    console.error("[ipcMain dialog:browseFolder] error:", e);
    return "";
  }
});

ipcMain.handle("fs:browse", async (event, targetPath) => {
  if (!fromServer(event)) return { ok: false, error: "forbidden" };
  try {
    return fsops.browse({ path: targetPath });
  } catch (e) {
    console.error("[ipcMain fs:browse] error:", e);
    return { ok: false, error: String(e) };
  }
});

ipcMain.handle("fs:mkdir", async (event, args) => {
  if (!fromServer(event)) return { ok: false, error: "forbidden" };
  try {
    await policy.ensurePath((args || {}).path, "create a folder in");
    return fsops.mkdir(args || {});
  } catch (e) {
    console.error("[ipcMain fs:mkdir] error:", e);
    return { ok: false, error: String(e) };
  }
});

// One-way hash of the hardware id: identical to the server's normalize_device_id, so
// projects registered by older companions (which sent dev_win_<guid slice>) still match.
function hashDeviceId(raw) {
  return "dev_h_" + crypto.createHash("sha256").update("a770-device:" + raw).digest("hex").slice(0, 24);
}

let cachedMachineId = null;
function getMachineId() {
  if (!cachedMachineId) {
    const raw = rawMachineId();
    cachedMachineId = /^dev_(win|lnx|mac)_[0-9a-z]+$/.test(raw) ? hashDeviceId(raw) : raw;
  }
  return cachedMachineId;
}

function rawMachineId() {
  try {
    if (process.platform === "win32") {
      const { execSync } = require("child_process");
      const out = execSync("reg query HKLM\\SOFTWARE\\Microsoft\\Cryptography /v MachineGuid", {
        encoding: "utf8",
        timeout: 5000,
        windowsHide: true,
      });
      const match = out.match(/MachineGuid\s+REG_SZ\s+(\S+)/i);
      if (match && match[1]) {
        const clean = match[1].replace(/[^a-zA-Z0-9]/g, "").slice(0, 16).toLowerCase();
        return `dev_win_${clean}`;
      }
    } else if (process.platform === "linux") {
      const fs = require("fs");
      const idPath = fs.existsSync("/etc/machine-id") ? "/etc/machine-id" : "/var/lib/dbus/machine-id";
      if (fs.existsSync(idPath)) {
        const id = fs.readFileSync(idPath, "utf8").trim().replace(/[^a-zA-Z0-9]/g, "").slice(0, 16).toLowerCase();
        if (id) return `dev_lnx_${id}`;
      }
    } else if (process.platform === "darwin") {
      const { execSync } = require("child_process");
      const out = execSync("ioreg -rd1 -c IOPlatformExpertDevice", { encoding: "utf8", timeout: 5000 });
      const match = out.match(/"IOPlatformUUID"\s*=\s*"([^"]+)"/);
      if (match && match[1]) {
        const clean = match[1].replace(/[^a-zA-Z0-9]/g, "").slice(0, 16).toLowerCase();
        return `dev_mac_${clean}`;
      }
    }
  } catch (e) {
    console.error("[getMachineId] Failed to read OS machine ID:", e);
  }

  // Persistent fallback in user home folder (survives app reinstall)
  try {
    const fs = require("fs");
    const idPath = path.join(os.homedir(), ".antigravity_device_id");
    if (fs.existsSync(idPath)) {
      const saved = fs.readFileSync(idPath, "utf8").trim();
      if (saved) return saved;
    }
    const newId = "dev_" + Math.random().toString(36).substring(2, 10) + "_" + Date.now().toString(36);
    fs.writeFileSync(idPath, newId, "utf8");
    return newId;
  } catch (_) {
    return "dev_default";
  }
}

ipcMain.on("system:getDeviceIdSync", (event) => {
  event.returnValue = fromServer(event) ? getMachineId() : null;
});

ipcMain.handle("system:getDeviceId", (event) => {
  return fromServer(event) ? getMachineId() : null;
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

// Every server-initiated op passes the local policy (companion/policy.js):
// file access only inside user-approved folders, shell commands confirmed here.
const OPS = {
  "fs.browse_folder": async (p) => {
    const r = await fsops.browseFolder(p);
    if (r.path) policy.approveRoot(r.path);   // picked by the user in a native dialog
    return r;
  },
  "fs.browse": fsops.browse,                  // folder names only (workspace picker)
  "fs.mkdir": async (p) => { await policy.ensurePath(p.path, "create a folder in"); return fsops.mkdir(p); },
  "fs.read": async (p) => { await policy.ensurePath(p.path, "read"); return fsops.read(p); },
  "fs.write": async (p) => { await policy.ensurePath(p.path, "write"); return fsops.write(p); },
  "fs.read_b64": async (p) => { await policy.ensurePath(p.path, "read"); return fsops.readB64(p); },
  "fs.write_b64": async (p) => { await policy.ensurePath(p.path, "write"); return fsops.writeB64(p); },
  "fs.edit": async (p) => { await policy.ensurePath(p.path, "edit"); return fsops.edit(p); },
  "fs.list": async (p) => { await policy.ensurePath(p.root, "list files in"); return fsops.list(p); },
  "fs.grep": async (p) => { await policy.ensurePath(p.root, "search files in"); return fsops.grep(p); },
  "fs.tree": async (p) => { await policy.ensurePath(p.root, "browse"); return fsops.tree(p); },
  "shell.run": async (p) => {
    await policy.confirmShell(p.command, p.cwd, p.display);
    // run_python: the script was written before approval -- run it only if it is
    // exactly the code the user just approved
    if (p.display != null && p.command === 'python "_agent_run.py"') {
      const script = require("path").join(p.cwd || "", "_agent_run.py");
      const onDisk = require("fs").existsSync(script) ? require("fs").readFileSync(script, "utf-8") : null;
      if (onDisk !== p.display) throw new Error("script on disk does not match the approved code");
    }
    return shellops.run(p);
  },
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

async function reconnectNow() {
  parkedToken = null;
  reconnectDelay = RECONNECT_BASE_MS;
  const token = await getSessionCookie();
  if (token) {
    lastToken = token;
    connectWebSocket(token);
  }
}

// Retire the current socket without letting its close handler schedule another
// reconnect (that handler used to kill the *new* healthy socket a moment later).
function dropSocket() {
  clearInterval(keepaliveTimer);
  keepaliveTimer = null;
  const old = ws;
  ws = null;
  if (!old) return;
  old.removeAllListeners();
  old.on("error", () => {});
  try {
    old.terminate();
  } catch {}
}

let pairing = false;

function connectWebSocket(sessionToken, skipPair = false) {
  clearTimeout(reconnectTimer);
  reconnectTimer = null;
  parkedToken = null;
  dropSocket();
  const deviceKey = loadDeviceKey();
  if (!deviceKey && sessionToken && !skipPair) {
    if (pairing) return;
    pairing = true;
    pairDevice(sessionToken)
      .then((key) => connectWebSocket(sessionToken, !key))   // unpaired: old session auth
      .finally(() => { pairing = false; });
    return;
  }
  // Always the configured origin: deriving it from the window's current page would
  // send the session token (and file/shell access) to any site the window reached.
  const effectiveServer = SERVER_URL;
  // The session token travels only in headers: a ?token= query string ends up
  // in proxy / tunnel / access logs.
  const wsUrl = effectiveServer.replace(/^http/, "ws") + "/ws/companion";
  console.log("[connectWebSocket] connecting to", wsUrl);
  const authHeaders = deviceKey
    ? { Authorization: `Bearer ${deviceKey}` }
    : { Cookie: `${SESSION_COOKIE_NAME}=${sessionToken}`, Authorization: `Bearer ${sessionToken}` };
  const sock = new WebSocket(wsUrl, {
    headers: {
      ...authHeaders,
      "ngrok-skip-browser-warning": "true",
      "User-Agent": "A770Companion A770NativeApp"
    }
  });
  ws = sock;
  // Every handler ignores events from a socket that is no longer the current one.
  const current = () => sock === ws;

  sock.on("unexpected-response", (req, res) => {
    if (!current()) return;
    console.error("[connectWebSocket] unexpected-response", res.statusCode);
    setTrayStatus("connection error — retrying…", false);
    dropSocket();
    scheduleReconnect();
  });

  sock.on("open", () => {
    if (!current()) return;
    console.log("[connectWebSocket] open");
    reconnectDelay = RECONNECT_BASE_MS;
    sock.send(JSON.stringify({ type: "hello", hostname: os.hostname(), device_id: getMachineId() }));
    setTrayStatus(`connected to ${effectiveServer}`, true);
    let alive = true;
    sock.on("pong", () => { alive = true; });
    keepaliveTimer = setInterval(() => {
      if (!current()) return;
      if (!alive) {
        console.warn("[connectWebSocket] no pong — reconnecting");
        dropSocket();
        scheduleReconnect();
        return;
      }
      alive = false;
      try { sock.ping(); } catch {}
    }, KEEPALIVE_MS);
  });

  sock.on("message", async (raw) => {
    let frame;
    try {
      frame = JSON.parse(raw.toString());
    } catch {
      return;
    }
    if (frame.type === "call") {
      const result = await handleCall(frame);
      if (sock.readyState === WebSocket.OPEN) sock.send(JSON.stringify(result));
    }
  });

  sock.on("close", (code, reason) => {
    if (!current()) return;
    console.log("[connectWebSocket] closed", code, reason.toString());
    dropSocket();
    if (deviceKey && (code === CLOSE_UNAUTHORIZED || code === CLOSE_WRONG_DEVICE)) {
      // key revoked in Settings (or copied from another machine): forget it and
      // re-pair with the current sign-in, if there is one
      clearDeviceKey();
      setTrayStatus("device key revoked — re-pairing…", false);
      scheduleReconnect();
      return;
    }
    if (code === CLOSE_REPLACED || code === CLOSE_UNAUTHORIZED) {
      // don't fight another companion for the connection / retry a bad session;
      // resume when the user signs in again or clicks "Reconnect" in the tray
      parkedToken = sessionToken;
      setTrayStatus(code === CLOSE_REPLACED ? "in use by another companion" : "sign in required", false);
      return;
    }
    setTrayStatus("disconnected — retrying…", false);
    scheduleReconnect();
  });

  sock.on("error", (err) => {
    if (!current()) return;
    console.error("[connectWebSocket] error", err.message);
    setTrayStatus("connection error — retrying…", false);
  });
}

// One companion per machine: a second copy would keep taking the server
// connection away from the first (4409) and back again.
if (!app.requestSingleInstanceLock()) {
  app.quit();
} else {
  app.on("second-instance", showMainWindow);
}

app.whenReady().then(() => {
  if (!SERVER_URL) {
    dialog.showErrorBox("A770 Companion — not configured",
      `${SERVER_URL_ERROR}.

Set A770_SERVER_URL=https://your-server in a .env file next to the app.`);
    isQuitting = true;
    app.quit();
    return;
  }
  tray = new Tray(trayIconImage);
  tray.on("click", showMainWindow);
  setTrayStatus("starting…", false);

  autoUpdater.checkForUpdatesAndNotify().catch(() => {});

  mainWindow = createMainWindow();

  getSessionCookie()
    .then((token) => {
      console.log("[whenReady] session cookie", token ? "found" : "not found");
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

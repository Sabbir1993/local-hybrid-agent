// companion/main.js — Electron main process: local companion for the
// Local Agent server. Shows the server's own web app in a native
// window (login included — "/" redirects to "/login" server-side when
// unauthenticated), and holds a WebSocket to core/companion_bridge.py to
// execute fs/shell RPCs from fsops.js / shellops.js on this machine.

const { app, BrowserWindow, Tray, Menu, nativeImage, session: electronSession, dialog, ipcMain, shell, safeStorage, Notification: NativeNotification } = require("electron");
const fs = require("fs");
const crypto = require("crypto");
const WebSocket = require("ws");
const os = require("os");
const path = require("path");
const { autoUpdater } = require("electron-updater");


const { SERVER_URL, SERVER_URL_ERROR, FRONTEND_URL, START_URL } = require("./config");
const fsops = require("./fsops");
const shellops = require("./shellops");
const gitops = require("./gitops");
const policy = require("./policy");
const oauthops = require("./oauthops");
// loaded on first use: playwright-core is only needed once the agent opens a browser
let _browserops = null;
const browserops = () => (_browserops = _browserops || require("./browserops"));
let _excelops = null;
const excelops = () => (_excelops = _excelops || require("./excelops"));
const androidops = require("./androidops");
const iosops = require("./iosops");

// Icon: shipped as extraResources when installed, from build/ when run from source (see appicon.js)
const { loadAppIcon } = require("./appicon");
const appIcon = loadAppIcon({ nativeImage, fs }).image;
const trayIconImage = appIcon.resize({ width: 32, height: 32 });

const SESSION_COOKIE_NAME = "a770_session";
const CSRF_COOKIE_NAME = "a770_csrf";
const RECONNECT_BASE_MS = 2000;
const RECONNECT_MAX_MS = 30000;
const KEEPALIVE_MS = 25000;        // client ping; a link with no pong by the next tick is dead
const CLOSE_REPLACED = 4409;       // server: another companion signed in as this user took over
const CLOSE_UNAUTHORIZED = 4401;   // server: session cookie / device key invalid or revoked
const CLOSE_WRONG_DEVICE = 4403;   // server: device key used from a different machine

// A tray app has no console: mirror the log to a file (never contains keys or tokens) so pairing problems can be read.
for (const lvl of ["log", "warn", "error"]) {
  const orig = console[lvl].bind(console);
  console[lvl] = (...a) => {
    orig(...a);
    try {
      require("fs").appendFileSync(path.join(app.getPath("userData"), "companion.log"),
        `${new Date().toISOString()} ${lvl} ${a.map((x) => (x && x.stack) || String(x)).join(" ")}\n`);
    } catch (_) {}
  };
}

process.on("uncaughtException", (e) => console.error("[uncaughtException]", e));
process.on("unhandledRejection", (e) => console.error("[unhandledRejection]", e));

let tray = null;
let mainWindow = null;
let connected = false;
let isQuitting = false;
const seenTokens = new Set();   // session tokens already checked for a paired device
// One connection per paired account (device key): the same machine can serve several signed-in users at once.
const conns = new Map();
let pairing = false;

function setTrayStatus(text, connectedNow) {
  connected = connectedNow;
  if (!tray) return;
  tray.setToolTip(`SSL Local Agent — ${text}`);
  tray.setContextMenu(
    Menu.buildFromTemplate([
      { label: connectedNow ? "🟢 Connected" : "🔴 Not connected", enabled: false },
      { label: "Show App", click: showMainWindow },
      { label: "Reconnect", click: reconnectNow, enabled: !connectedNow },
      {
        label: "Also confirm commands on this device",
        type: "checkbox",
        checked: policy.localConfirmEnabled(),
        click: (item) => policy.setLocalConfirm(item.checked),
      },
      {
        label: "Forget always-allowed commands",
        click: () => {
          policy.forgetAlwaysAllowed();
          dialog.showMessageBox({ type: "info", title: "SSL Local Agent",
            message: "The agent will ask again before running any command." });
        },
      },
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
  return path.join(app.getPath("userData"), "device-keys.bin");
}

// Entries are {key, id}; the legacy single-key file is migrated on first read.
function loadDeviceKeys() {
  const fs = require("fs");
  try {
    if (!safeStorage.isEncryptionAvailable()) return [];
    let list = [];
    if (fs.existsSync(deviceKeyPath())) {
      list = JSON.parse(safeStorage.decryptString(fs.readFileSync(deviceKeyPath())));
    } else {
      const legacy = path.join(app.getPath("userData"), "device-key.bin");
      if (fs.existsSync(legacy)) {
        const key = safeStorage.decryptString(fs.readFileSync(legacy));
        list = [{ key, id: null }];
        saveDeviceKeys(list);
        try { fs.unlinkSync(legacy); } catch (_) {}
      }
    }
    return list.filter((e) => e && typeof e.key === "string" && e.key.startsWith("a770_dev_"));
  } catch (e) {
    console.error("[deviceKey] unreadable:", e.message);
    return [];
  }
}

function saveDeviceKeys(list) {
  try {
    if (!safeStorage.isEncryptionAvailable()) return false;   // never store them in plaintext
    require("fs").writeFileSync(deviceKeyPath(), safeStorage.encryptString(JSON.stringify(list)), { mode: 0o600 });
    return true;
  } catch (e) {
    console.error("[deviceKey] save failed:", e.message);
    return false;
  }
}

function addDeviceKey(key, id) {
  const list = loadDeviceKeys().filter((e) => e.id !== id);
  list.push({ key, id });
  return saveDeviceKeys(list);
}

function removeDeviceKey(key) {
  saveDeviceKeys(loadDeviceKeys().filter((e) => e.key !== key));
}

async function getServerCookie(name) {
  const sessions = [];
  if (mainWindow && mainWindow.webContents) sessions.push(mainWindow.webContents.session);
  sessions.push(electronSession.defaultSession);
  const targetUrls = [SERVER_URL];
  if (FRONTEND_URL && FRONTEND_URL !== SERVER_URL) {
    targetUrls.push(FRONTEND_URL);
  }
  for (const ses of sessions) {
    for (const u of targetUrls) {
      try {
        const cookies = await ses.cookies.get({ url: u, name });
        if (cookies && cookies.length) return cookies[0].value;
      } catch (_) {}
    }
  }
  return null;
}

async function pairDevice(sessionToken, csrf) {
  if (!csrf) {
    console.warn("[pairDevice] no CSRF cookie for", SERVER_URL, "- sign in again in this window");
    return null;
  }
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
    if (!d.device_key || !addDeviceKey(d.device_key, d.id)) return null;
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
  const targetUrls = [SERVER_URL];
  if (FRONTEND_URL && FRONTEND_URL !== SERVER_URL) {
    targetUrls.push(FRONTEND_URL);
  }
  for (const ses of sessions) {
    for (const u of targetUrls) {
      try {
        const cookies = await ses.cookies.get({ url: u, name: SESSION_COOKIE_NAME });
        if (cookies && cookies.length) return cookies[0].value;
      } catch (_) {}
    }
  }
  return null;
}

// Every distinct signed-in session the window holds: the page may be served from another host than SERVER_URL
// (e.g. localhost:5173 vs 127.0.0.1:8000), so each origin has its own cookie jar entry, and a different account.
async function getSignedInSessions() {
  const sessions = [];
  if (mainWindow && mainWindow.webContents) sessions.push(mainWindow.webContents.session);
  sessions.push(electronSession.defaultSession);
  const urls = [SERVER_URL];
  if (FRONTEND_URL && FRONTEND_URL !== SERVER_URL) urls.push(FRONTEND_URL);
  const out = new Map();
  for (const ses of sessions) {
    for (const u of urls) {
      try {
        const [tok] = await ses.cookies.get({ url: u, name: SESSION_COOKIE_NAME });
        if (!tok || out.has(tok.value)) continue;
        const [csrf] = await ses.cookies.get({ url: u, name: CSRF_COOKIE_NAME });
        out.set(tok.value, { token: tok.value, csrf: csrf ? csrf.value : null });
      } catch (_) {}
    }
  }
  return [...out.values()];
}

function isServerUrl(url) {
  try {
    const origin = new URL(url).origin;
    return origin === SERVER_URL || (FRONTEND_URL && origin === FRONTEND_URL);
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
  if (mainWindow.isMinimized()) mainWindow.restore();
  mainWindow.show();
  mainWindow.focus();
  try {
    if (typeof mainWindow.flashFrame === "function") mainWindow.flashFrame(false);
  } catch (_) {}
}

function showCompanionNotification(title, body) {
  try {
    if (NativeNotification && NativeNotification.isSupported()) {
      const notif = new NativeNotification({
        title: title || "Local Agent",
        body: body || "Task completed.",
        icon: appIcon,
      });
      notif.on("click", () => {
        showMainWindow();
      });
      notif.show();
    }
  } catch (e) {
    console.error("[showCompanionNotification] error:", e);
  }
  try {
    if (mainWindow && typeof mainWindow.flashFrame === "function") {
      mainWindow.flashFrame(true);
    }
  } catch (_) {}
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
      devTools: false,
    },
  });

  // DevTools are off: also swallow the shortcuts (Ctrl/Cmd+Shift+I/J/C, F12)
  win.webContents.on("before-input-event", (event, input) => {
    const k = String(input.key || "").toLowerCase();
    const mod = input.control || input.meta;
    if (k === "f12" || (mod && input.shift && ["i", "j", "c"].includes(k))) event.preventDefault();
  });

  win.on("focus", () => {
    try {
      if (typeof win.flashFrame === "function") win.flashFrame(false);
    } catch (_) {}
  });

  // Context menu for input fields (Cut/Copy/Paste/Undo/Redo/SelectAll)
  win.webContents.on("context-menu", (e, params) => {
    const items = [];
    if (params.isEditable) {
      items.push(
        { role: "undo" },
        { role: "redo" },
        { type: "separator" },
        { role: "cut" },
        { role: "copy" },
        { role: "paste" },
        { role: "selectAll" }
      );
    } else if (params.selectionText) {
      items.push(
        { role: "copy" },
        { role: "selectAll" }
      );
    }
    if (items.length > 0) {
      Menu.buildFromTemplate(items).popup({ window: win });
    }
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
    // A new sign-in may be another account: make sure this machine has a connection for it (the others stay up).
    for (const sess of await getSignedInSessions()) {
      if (seenTokens.has(sess.token)) continue;
      seenTokens.add(sess.token);
      ensureAccount(sess);
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

  console.log(`[companion] Loading companion window at: ${START_URL}`);
  win.loadURL(START_URL + "/");
  return win;
}

// IPC is callable only from the server's own pages, never from a frame of another origin.
function fromServer(event) {
  const url = (event.senderFrame && event.senderFrame.url) || "";
  if (!url || url === "about:blank") {
    const senderWin = BrowserWindow.fromWebContents(event.sender);
    const winUrl = senderWin && senderWin.webContents ? senderWin.webContents.getURL() : "";
    if (winUrl && isServerUrl(winUrl)) return true;
    return true;
  }
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

ipcMain.handle("window:isMinimized", (event) => {
  if (!fromServer(event)) return false;
  if (!mainWindow) return false;
  return mainWindow.isMinimized() || !mainWindow.isVisible() || !mainWindow.isFocused();
});

ipcMain.handle("window:flashFrame", (event, flag) => {
  if (!fromServer(event)) return false;
  try {
    if (mainWindow && typeof mainWindow.flashFrame === "function") {
      mainWindow.flashFrame(flag !== false);
    }
  } catch (_) {}
  return true;
});

ipcMain.handle("window:notify", (event, args) => {
  if (!fromServer(event)) return false;
  const opts = args || {};
  showCompanionNotification(opts.title, opts.body);
  return true;
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
  "fs.remove": async (p) => { await policy.ensurePath(p.path, "delete"); return fsops.remove(p); },
  "fs.verify": async (p) => { await policy.ensurePath(p.path, "read"); return fsops.verify(p); },
  "fs.diagnose": async (p) => { await policy.ensurePath(p.path, "read"); return fsops.diagnose(p); },
  "fs.list": async (p) => { await policy.ensurePath(p.root, "list files in"); return fsops.list(p); },
  "fs.grep": async (p) => { await policy.ensurePath(p.root, "search files in"); return fsops.grep(p); },
  "fs.read_many": async (p) => { await policy.ensurePath(p.root, "read source files in"); return fsops.readMany(p); },
  "fs.tree": async (p) => { await policy.ensurePath(p.root, "browse"); return fsops.tree(p); },
  "shell.run": async (p) => {
    await policy.confirmShell(p.command, p.cwd, p.display, p.approved_in_app === true);
    // run_python: the script was written before approval -- run it only if it is
    // exactly the code that was approved. Scripts are unique per call
    // (`_agent_run_<hex8>.py`; older servers send the fixed `_agent_run.py`),
    // so verify whichever file the command names.
    const scriptName = policy.runPythonScript(p.command);
    if (p.display != null && scriptName) {
      const script = require("path").join(p.cwd || "", scriptName);
      const onDisk = require("fs").existsSync(script) ? require("fs").readFileSync(script, "utf-8") : null;
      if (onDisk !== p.display) throw new Error("script on disk does not match the approved code");
    }
    return shellops.run(p);
  },
  "git.run": async (p) => {
    // git must run inside an approved folder, like every other fs-touching op.
    // The args are the server's; the companion's destructive-op guard in
    // gitops.js still refuses history-destroying forms on this machine.
    await policy.ensurePath(p.cwd, "run git in");
    return gitops.run(p);
  },
  // calculate a workbook with the Excel installed on this PC (excelops.js): macros off, temp copy, hard timeout
  "excel.recalc": async (p) => {
    let data_b64 = p.data_b64;
    if (!data_b64 && p.path) {                  // a workbook in an approved project folder
      await policy.ensurePath(p.path, "read");
      data_b64 = (await fsops.readB64({ path: p.path })).data;
    }
    await policy.confirmAction("calculate a workbook in Excel?",
      "The AI assistant wants to open a workbook in Excel on this PC to calculate its formulas. Excel runs hidden on a temporary copy with macros " +
      "and link updates disabled, and only the calculated numbers are sent back.", String(p.name || "workbook.xlsx"), "excel.recalc", false);
    return excelops().recalc({ data_b64, name: p.name });
  },
  // OAuth sign-in redirect for remote MCP servers (oauthops.js)
  "oauth.loopback": (p) => oauthops.loopback(p),
  // open a link in the user's own default browser (not the agent's test browser)
  "system.open_url": async (p) => {
    let u;
    try { u = new URL(String(p.url || "")); } catch (_) { throw new Error("invalid URL"); }
    if ((u.protocol !== "https:" && u.protocol !== "http:") || u.username || u.password) {
      throw new Error("only plain http(s) links can be opened");
    }
    await policy.confirmAction("open link?", "The AI agent wants to open this link in your browser:", u.href,
      `open\n${u.origin}`, p.approved_in_app === true);
    await shell.openExternal(u.href);
    return { opened: u.href };
  },
  // agent test browser (browserops.js)
  "browser.navigate": (p) => browserops().navigate(p),
  "browser.snapshot": (p) => browserops().snapshot(p),
  "browser.click": (p) => browserops().click(p),
  "browser.type": (p) => browserops().type(p),
  "browser.press": (p) => browserops().press(p),
  "browser.select": (p) => browserops().select(p),
  "browser.screenshot": (p) => browserops().screenshot(p),
  "browser.console": (p) => browserops().console(p),
  "browser.eval": (p) => browserops().evaluate(p),
  "browser.wait": (p) => browserops().waitFor(p),
  "browser.close": (p) => browserops().close(p),
  // Android emulator / phone over adb (androidops.js)
  "android.devices": androidops.devices,
  "android.boot_avd": androidops.bootAvd,
  "android.pair": androidops.pair,
  "android.connect": androidops.connect,
  "android.install": androidops.install,
  "android.launch": androidops.launch,
  "android.stop": androidops.stopApp,
  "android.screenshot": androidops.screenshot,
  "android.ui_dump": androidops.uiDump,
  "android.tap": androidops.tap,
  "android.swipe": androidops.swipe,
  "android.text": androidops.text,
  "android.key": androidops.key,
  "android.logcat": androidops.logcat,
  // iOS Simulator, macOS companions only (iosops.js)
  "ios.devices": iosops.devices,
  "ios.boot": iosops.boot,
  "ios.install": iosops.install,
  "ios.launch": iosops.launch,
  "ios.screenshot": iosops.screenshot,
  "ios.ui_dump": iosops.uiDump,
  "ios.tap": iosops.tap,
  "ios.text": iosops.text,
  "ios.logcat": iosops.logcat,
  // lets the server tell the user their companion predates these ops
  "companion.capabilities": async () => ({ ops: Object.keys(OPS), platform: process.platform,
                                            version: app.getVersion() }),
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

function refreshTray() {
  const all = [...conns.values()];
  const up = all.filter((c) => c.state === "connected").length;
  if (!all.length) return setTrayStatus("sign in to connect", false);
  const parked = all.find((c) => c.state === "parked");
  const text = up
    ? `connected (${up} account${up > 1 ? "s" : ""})`
    : parked ? parked.note : "connecting…";
  setTrayStatus(text, up > 0);
}

async function reconnectNow() {
  for (const c of conns.values()) {
    if (c.state === "connected") continue;
    c.delay = RECONNECT_BASE_MS;
    openConn(c);
  }
  seenTokens.clear();   // the 3 s window check picks the signed-in sessions up again
}

// Retire a connection's socket without letting its close handler schedule another reconnect.
function dropSocket(c) {
  clearInterval(c.keepalive);
  c.keepalive = null;
  clearTimeout(c.timer);
  c.timer = null;
  const old = c.ws;
  c.ws = null;
  if (!old) return;
  old.removeAllListeners();
  old.on("error", () => {});
  try { old.terminate(); } catch {}
}

function scheduleReconnect(c) {
  if (c.timer) return;
  c.timer = setTimeout(() => { c.timer = null; openConn(c); }, c.delay);
  c.delay = Math.min(c.delay * 2, RECONNECT_MAX_MS);
}

// Start (or reuse) the connection for one credential: {key} for a paired device, {token} for an unpaired session.
function addConn(auth) {
  const id = auth.key ? "k:" + auth.key.slice(-12) : "s:" + auth.token.slice(-12);
  let c = conns.get(id);
  if (c) return c;
  c = { id, key: auth.key || null, token: auth.token || null, ws: null, timer: null, keepalive: null,
        delay: RECONNECT_BASE_MS, state: "connecting", note: "" };
  conns.set(id, c);
  openConn(c);
  return c;
}

// Pairs a new device for a signed-in account that has none on this machine; accounts that are already paired keep
// their own connection untouched. Device rows are matched to stored keys by the id returned at pairing.
async function ensureAccount(sess) {
  const token = sess.token;
  if (pairing) { seenTokens.delete(token); return; }   // retried by the next window check
  pairing = true;
  try {
    let mine = null;   // this account's live device rows on this machine, or null when unknown
    try {
      const r = await fetch(SERVER_URL + "/companion/devices", {
        headers: { Cookie: `${SESSION_COOKIE_NAME}=${token}`, "User-Agent": "A770Companion A770NativeApp" },
        redirect: "error",
      });
      if (r.ok) {
        const host = os.hostname();
        mine = ((await r.json()).devices || []).filter((x) => x.name === host && !x.revoked_at);
      }
    } catch (_) {}
    const keys = loadDeviceKeys();
    for (const e of keys) addConn({ key: e.key });
    console.log("[ensureAccount] devices for this account on this machine:", mine === null ? "unknown" : mine.length);
    if (mine === null) return;   // server unreachable or session unusable: keep what is connected
    const covered = mine.some((x) => keys.some((e) => e.id === x.id))
      || (mine.length > 0 && keys.some((e) => e.id == null));   // legacy key without a recorded id
    if (covered) return;
    const key = await pairDevice(token, sess.csrf);
    addConn(key ? { key } : { token });   // pairing refused: fall back to this sign-in's own session
  } finally {
    pairing = false;
  }
}

function openConn(c) {
  dropSocket(c);
  c.state = "connecting";
  // Always the configured origin: deriving it from the window's current page would
  // send the credential (and file/shell access) to any site the window reached.
  // The credential travels only in headers: a ?token= query string ends up in proxy / tunnel / access logs.
  const wsUrl = SERVER_URL.replace(/^http/, "ws") + "/ws/companion";
  console.log("[connectWebSocket] connecting to", wsUrl, c.id);
  const authHeaders = c.key
    ? { Authorization: `Bearer ${c.key}` }
    : { Cookie: `${SESSION_COOKIE_NAME}=${c.token}`, Authorization: `Bearer ${c.token}` };
  const sock = new WebSocket(wsUrl, {
    headers: {
      ...authHeaders,
      "ngrok-skip-browser-warning": "true",
      "User-Agent": "A770Companion A770NativeApp"
    }
  });
  c.ws = sock;
  // Every handler ignores events from a socket that is no longer the current one.
  const current = () => sock === c.ws;
  const retry = () => { dropSocket(c); c.state = "connecting"; refreshTray(); scheduleReconnect(c); };

  sock.on("unexpected-response", (req, res) => {
    if (!current()) return;
    console.error("[connectWebSocket] unexpected-response", res.statusCode, c.id);
    retry();
  });

  sock.on("open", () => {
    if (!current()) return;
    console.log("[connectWebSocket] open", c.id);
    c.delay = RECONNECT_BASE_MS;
    c.state = "connected";
    sock.send(JSON.stringify({ type: "hello", hostname: os.hostname(), device_id: getMachineId() }));
    refreshTray();
    let alive = true;
    sock.on("pong", () => { alive = true; });
    c.keepalive = setInterval(() => {
      if (!current()) return;
      if (!alive) {
        console.warn("[connectWebSocket] no pong — reconnecting", c.id);
        retry();
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
    console.log("[connectWebSocket] closed", code, reason.toString(), c.id);
    dropSocket(c);
    if (c.key && (code === CLOSE_UNAUTHORIZED || code === CLOSE_WRONG_DEVICE)) {
      // this account's key was revoked in Settings (or copied from another machine): forget only that key; the
      // next check of the signed-in account pairs again
      removeDeviceKey(c.key);
      conns.delete(c.id);
      refreshTray();
      seenTokens.clear();
      return;
    }
    if (code === CLOSE_REPLACED || code === CLOSE_UNAUTHORIZED) {
      // don't fight another companion for the connection / retry a bad session;
      // resume on "Reconnect" in the tray
      c.state = "parked";
      c.note = code === CLOSE_REPLACED ? "in use by another companion" : "sign in required";
      refreshTray();
      return;
    }
    retry();
  });

  sock.on("error", (err) => {
    if (!current()) return;
    console.error("[connectWebSocket] error", err.message, c.id);
    c.state = "connecting";
    refreshTray();
  });
}

// One companion per machine: a second copy would keep taking the server
// connection away from the first (4409) and back again.
if (!app.requestSingleInstanceLock()) {
  app.quit();
} else {
  app.on("second-instance", showMainWindow);
}

function setupApplicationMenu() {
  const isMac = process.platform === "darwin";
  const template = [
    ...(isMac ? [{ role: "appMenu" }] : []),
    {
      label: "Edit",
      submenu: [
        { role: "undo" },
        { role: "redo" },
        { type: "separator" },
        { role: "cut" },
        { role: "copy" },
        { role: "paste" },
        { role: "pasteAndMatchStyle" },
        { role: "delete" },
        { role: "selectAll" }
      ]
    },
    {
      label: "View",
      submenu: [
        { role: "reload" },
        { role: "forceReload" },
        { type: "separator" },
        { role: "resetZoom" },
        { role: "zoomIn" },
        { role: "zoomOut" },
        { type: "separator" },
        { role: "togglefullscreen" }
      ]
    }
  ];
  Menu.setApplicationMenu(Menu.buildFromTemplate(template));
}

app.whenReady().then(() => {
  setupApplicationMenu();
  if (!SERVER_URL) {
    dialog.showErrorBox("SSL Local Agent — not configured",
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

  for (const e of loadDeviceKeys()) addConn({ key: e.key });
  getSignedInSessions()
    .then((list) => console.log("[whenReady] signed-in sessions:", list.length))
    .catch((e) => console.error("[whenReady] getSignedInSessions failed:", e));
});

app.on("window-all-closed", (e) => {
  // Companion lives in the tray; closing the window must not quit it.
  if (!isQuitting) e.preventDefault?.();
});

app.on("before-quit", () => {
  isQuitting = true;
});

// A visible test-browser window on the user's machine. The agent drives it through
// browser.window.open / navigate ops; the user can watch the page it just built.
const { BrowserWindow } = require("electron");
const path = require("path");

let win = null;

function open(url, approvedInApp) {
  // Origin approval follows the same device-side policy as the headless browser.
  const check = require("./browserops").checkUrl;
  const checked = check(url, approvedInApp === true);
  const target = checked ? checked.href : String(url);
  if (win && !win.isDestroyed()) {
    navigate(target);
    win.focus();
    return { opened: target };
  }
  win = new BrowserWindow({
    width: 1280,
    height: 860,
    title: "Agent Test Browser",
    webPreferences: {
      contextIsolation: true,
      nodeIntegration: false,
      webviewTag: true,
      devTools: false,
      preload: path.resolve(__dirname, "test-browser-preload.js"),
    },
  });
  win.loadFile(path.resolve(__dirname, "test-browser.html"));
  win.webContents.once("did-finish-load", () => {
    if (win && !win.isDestroyed()) win.webContents.send("testbrowser:init", target);
  });
  win.on("closed", () => { win = null; });
  return { opened: target };
}

function navigate(url) {
  if (!win || win.isDestroyed()) return { opened: false, note: "no test browser window open" };
  win.webContents.send("testbrowser:navigate", String(url || ""));
  return { opened: String(url || "") };
}

function close() {
  if (win && !win.isDestroyed()) win.close();
  win = null;
  return { closed: true };
}

module.exports = { open, navigate, close };

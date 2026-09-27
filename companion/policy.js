// companion/policy.js — local safety policy for server-initiated operations.
//
// The server decides *what* the agent wants to do, but this machine decides
// what it *allows*: a compromised server or a stolen session token must not
// be able to read/write arbitrary files or run arbitrary commands here.
//
//  - File ops are limited to folders the user approved. A folder the user
//    picks in the native "Select Local Workspace" dialog is approved
//    automatically; anything else triggers an Allow/Deny prompt.
//  - Shell commands need a local confirmation (with a "don't ask again this
//    session" option for that exact command) and must run inside an approved folder.
//  - Paths are compared after resolving symlinks/junctions, so a link inside an
//    approved folder can't reach outside it.

const { app, dialog, BrowserWindow } = require("electron");
const fs = require("fs");
const path = require("path");

let roots = null;               // approved folder roots (resolved, persisted)
const trustedCommands = new Set();   // exact cwd+command+code the user allowed this session
let promptChain = Promise.resolve();   // one dialog at a time

function storePath() {
  return path.join(app.getPath("userData"), "approved-roots.json");
}

function loadRoots() {
  if (roots) return roots;
  try {
    roots = JSON.parse(fs.readFileSync(storePath(), "utf8")).filter((r) => typeof r === "string");
  } catch {
    roots = [];
  }
  return roots;
}

function saveRoots() {
  try {
    fs.writeFileSync(storePath(), JSON.stringify(roots, null, 2), "utf8");
  } catch (e) {
    console.error("[policy] failed to persist approved roots:", e.message);
  }
}

// Real path of `p`, resolving symlinks/junctions. For a path that doesn't exist
// yet (a file about to be written), resolve its nearest existing ancestor.
function realish(p) {
  let cur = path.resolve(String(p || ""));
  const rest = [];
  for (;;) {
    try {
      return path.join(fs.realpathSync.native(cur), ...rest.reverse());
    } catch (_) {
      const parent = path.dirname(cur);
      if (parent === cur) return path.resolve(String(p || ""));
      rest.push(path.basename(cur));
      cur = parent;
    }
  }
}

function norm(p) {
  const r = realish(p);
  return process.platform === "win32" ? r.toLowerCase() : r;
}

function isInside(p, root) {
  const a = norm(p);
  const b = norm(root).replace(/[\\/]+$/, "");
  return a === b || a.startsWith(b + path.sep);
}

function isApproved(p) {
  return loadRoots().some((r) => isInside(p, r));
}

function approveRoot(dir) {
  if (!dir) return;
  const r = path.resolve(dir);
  loadRoots();
  if (!roots.some((x) => norm(x) === norm(r))) {
    roots.push(r);
    saveRoots();
  }
}

function parentWindow() {
  return BrowserWindow.getFocusedWindow() || BrowserWindow.getAllWindows()[0] || null;
}

function prompt(opts) {
  // serialize dialogs so concurrent requests don't stack windows
  const run = () => {
    const win = parentWindow();
    return win ? dialog.showMessageBox(win, opts) : dialog.showMessageBox(opts);
  };
  const p = promptChain.then(run, run);
  promptChain = p.catch(() => {});
  return p;
}

// Ensure `target` (file or folder) is inside an approved root, asking the
// user once for its folder if it isn't. Throws when denied.
async function ensurePath(target, action) {
  if (!target) throw new Error("path required");
  if (isApproved(target)) return;
  let dir = path.resolve(target);
  try {
    if (!fs.existsSync(dir) || !fs.statSync(dir).isDirectory()) dir = path.dirname(dir);
  } catch {
    dir = path.dirname(dir);
  }
  const { response } = await prompt({
    type: "warning",
    buttons: ["Deny", "Allow this folder"],
    defaultId: 0,
    cancelId: 0,
    title: "A770 Companion — file access request",
    message: `The server wants to ${action}:\n${path.resolve(target)}`,
    detail: `Allow the AI agent to access files in this folder?\n\n${dir}\n\n` +
      "Only allow folders you intend to use as a workspace.",
  });
  if (response !== 1) throw new Error(`denied by local user: ${action} ${target}`);
  approveRoot(dir);
}

const MAX_SHOWN_CODE = 3000;

// "Always allow on this device": exact command line + folder, persisted here on the
// device only (the server can't add to it). Cleared from the tray menu.
let alwaysCommands = null;

function alwaysPath() {
  return path.join(app.getPath("userData"), "trusted-commands.json");
}

function loadAlways() {
  if (alwaysCommands) return alwaysCommands;
  try {
    const list = JSON.parse(fs.readFileSync(alwaysPath(), "utf8"));
    alwaysCommands = new Set(list.filter((k) => typeof k === "string"));
  } catch {
    alwaysCommands = new Set();
  }
  return alwaysCommands;
}

function saveAlways() {
  try {
    fs.writeFileSync(alwaysPath(), JSON.stringify([...loadAlways()], null, 2), "utf8");
  } catch (e) {
    console.error("[policy] failed to persist trusted commands:", e.message);
  }
}

function alwaysAllowedCount() {
  return loadAlways().size;
}

function forgetAlwaysAllowed() {
  alwaysCommands = new Set();
  trustedCommands.clear();
  saveAlways();
}

// "Also confirm on this device": off by default. The user already approves each command
// in the web app's permission card, so a second local dialog is skipped unless turned on
// from the tray menu (then every command is confirmed here too).
function settingsPath() {
  return path.join(app.getPath("userData"), "policy-settings.json");
}

let settings = null;
function loadSettings() {
  if (settings) return settings;
  try {
    settings = JSON.parse(fs.readFileSync(settingsPath(), "utf8")) || {};
  } catch {
    settings = {};
  }
  return settings;
}

function localConfirmEnabled() {
  return loadSettings().confirmLocally === true;
}

function setLocalConfirm(on) {
  loadSettings().confirmLocally = !!on;
  try {
    fs.writeFileSync(settingsPath(), JSON.stringify(settings, null, 2), "utf8");
  } catch (e) {
    console.error("[policy] failed to persist settings:", e.message);
  }
}

// `display` is what actually runs when `command` is only a wrapper (run_python sends
// `python "_agent_run.py"` plus the script body): the user approves the code, not the shim.
// `approvedInApp`: the server says the user approved it in the web app's card (or an allow
// rule / ask_first=off covered it) - no second dialog unless local confirmation is on.
// The folder check above still applies either way.
async function confirmShell(command, cwd, display, approvedInApp) {
  if (!cwd || !isApproved(cwd)) {
    await ensurePath(cwd || process.cwd(), "run a command in");
  }
  if (approvedInApp && !localConfirmEnabled()) return;
  const key = `${norm(cwd || "")}\n${command}\n${display || ""}`;
  if (trustedCommands.has(key)) return;
  if (!display && loadAlways().has(key)) return;
  let shown = display ? `${command}\n\n--- code ---\n${display}` : command;
  if (shown.length > MAX_SHOWN_CODE) {
    shown = shown.slice(0, MAX_SHOWN_CODE) + `\n… (${shown.length - MAX_SHOWN_CODE} more chars)`;
  }
  // code changes every time, so it can't be allowed forever; a command line can
  const buttons = display ? ["Deny", "Run"] : ["Deny", "Run", "Always allow on this device"];
  const { response, checkboxChecked } = await prompt({
    type: "question",
    buttons,
    defaultId: 0,
    cancelId: 0,
    title: "A770 Companion — run command?",
    message: display ? "The AI agent wants to run this code on your computer:"
                     : "The AI agent wants to run this command on your computer:",
    detail: `${shown}\n\nin: ${cwd}` +
      (display ? "" : "\n\n\"Always allow\" remembers this exact command in this folder " +
                      "(clear it from the tray menu)."),
    checkboxLabel: "Don't ask again for this exact command until the companion restarts",
    checkboxChecked: false,
  });
  if (response === 2 && !display) {
    loadAlways().add(key);
    saveAlways();
    return;
  }
  if (response !== 1) throw new Error("denied by local user");
  if (checkboxChecked) trustedCommands.add(key);
}

// Agent browser (browserops.js): a site other than a local dev host needs a one-time
// Allow per origin until the companion restarts.
const trustedOrigins = new Set();

async function confirmOrigin(origin) {
  if (trustedOrigins.has(origin)) return;
  const { response } = await prompt({
    type: "question",
    buttons: ["Deny", "Allow this site"],
    defaultId: 0,
    cancelId: 0,
    title: "A770 Companion — open a website?",
    message: "The AI agent wants to open this site in its test browser:",
    detail: `${origin}\n\nThe agent's browser is a fresh profile (none of your logins or cookies), ` +
      "but anything the page shows is sent to the AI. Allow only sites you are testing.",
  });
  if (response !== 1) throw new Error(`denied by local user: open ${origin}`);
  trustedOrigins.add(origin);
}

// Page JavaScript and device actions (install an app, boot an emulator, pair a phone)
// are confirmed each time, with the same "don't ask again" option as shell commands.
async function confirmAction(title, message, detail, trustKey) {
  if (trustKey && trustedCommands.has(trustKey)) return;
  const { response, checkboxChecked } = await prompt({
    type: "question",
    buttons: ["Deny", "Allow"],
    defaultId: 0,
    cancelId: 0,
    title: `A770 Companion — ${title}`,
    message,
    detail: detail.length > MAX_SHOWN_CODE ? detail.slice(0, MAX_SHOWN_CODE) + "\n…" : detail,
    checkboxLabel: trustKey ? "Don't ask again for this until the companion restarts" : undefined,
    checkboxChecked: false,
  });
  if (response !== 1) throw new Error("denied by local user");
  if (trustKey && checkboxChecked) trustedCommands.add(trustKey);
}

async function confirmScript(code, url) {
  await confirmAction("run page script?", "The AI agent wants to run this JavaScript in its test browser:",
    `${code}\n\non: ${url}`, `js\n${url}\n${code}`);
}

module.exports = { ensurePath, confirmShell, approveRoot, isApproved, confirmOrigin, confirmAction, confirmScript,
                   alwaysAllowedCount, forgetAlwaysAllowed, localConfirmEnabled, setLocalConfirm };

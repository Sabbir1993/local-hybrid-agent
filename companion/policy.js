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
//    session" option) and must run inside an approved folder.

const { app, dialog, BrowserWindow } = require("electron");
const fs = require("fs");
const path = require("path");

let roots = null;               // approved folder roots (resolved, persisted)
let shellTrustedThisSession = false;
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

function norm(p) {
  const r = path.resolve(String(p || ""));
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

async function confirmShell(command, cwd) {
  if (!cwd || !isApproved(cwd)) {
    await ensurePath(cwd || process.cwd(), "run a command in");
  }
  if (shellTrustedThisSession) return;
  const { response, checkboxChecked } = await prompt({
    type: "question",
    buttons: ["Deny", "Run"],
    defaultId: 0,
    cancelId: 0,
    title: "A770 Companion — run command?",
    message: "The AI agent wants to run this command on your computer:",
    detail: `${command}\n\nin: ${cwd}`,
    checkboxLabel: "Don't ask again until the companion restarts",
    checkboxChecked: false,
  });
  if (response !== 1) throw new Error("denied by local user");
  if (checkboxChecked) shellTrustedThisSession = true;
}

module.exports = { ensurePath, confirmShell, approveRoot, isApproved };

// companion/fsops.js — local filesystem operations invoked by the server over
// the WebSocket bridge (core/companion_bridge.py). Mirrors the shape of the
// server-local implementations in core/agent_tools.py / routes/projects.py
// so the two are drop-in equivalents from the server's point of view.

const fs = require("fs");
const path = require("path");
const os = require("os");
const { dialog, BrowserWindow } = require("electron");

const SKIP_DIR_NAMES = new Set([".git", "node_modules", "__pycache__", ".venv", "venv"]);

function globToRegExp(pattern) {
  // Minimal glob->regex: ** matches any depth, * matches within a segment, ? one char.
  let re = "";
  for (let i = 0; i < pattern.length; i++) {
    const c = pattern[i];
    if (c === "*" && pattern[i + 1] === "*") {
      re += ".*";
      i++;
      if (pattern[i + 1] === "/") i++;
    } else if (c === "*") {
      re += "[^/\\\\]*";
    } else if (c === "?") {
      re += "[^/\\\\]";
    } else if (".+^$()[]{}|\\".includes(c)) {
      re += "\\" + c;
    } else {
      re += c;
    }
  }
  return new RegExp("^" + re + "$", "i");
}

function walk(root, onFile) {
  let entries;
  try {
    entries = fs.readdirSync(root, { withFileTypes: true });
  } catch {
    return;
  }
  for (const ent of entries) {
    if (ent.name.startsWith(".") && ent.isDirectory()) continue;
    const full = path.join(root, ent.name);
    if (ent.isDirectory()) {
      if (SKIP_DIR_NAMES.has(ent.name)) continue;
      walk(full, onFile);
    } else if (ent.isFile()) {
      onFile(full);
    }
  }
}

function getDrives() {
  if (process.platform !== "win32") return ["/"];
  const drives = [];
  for (let c = 67; c <= 90; c++) {
    const d = String.fromCharCode(c) + ":\\";
    try {
      if (fs.existsSync(d)) drives.push(d);
    } catch {}
  }
  return drives.length ? drives : ["C:\\"];
}

async function browseFolder({ initial_dir }) {
  const defaultPath = initial_dir && fs.existsSync(initial_dir) ? initial_dir : os.homedir();
  const focusedWin = BrowserWindow.getFocusedWindow() || (BrowserWindow.getAllWindows && BrowserWindow.getAllWindows()[0]) || null;
  const opts = {
    title: "Select Local Workspace Directory",
    defaultPath,
    properties: ["openDirectory", "createDirectory"],
  };
  const res = focusedWin ? await dialog.showOpenDialog(focusedWin, opts) : await dialog.showOpenDialog(opts);
  return { path: res.canceled ? "" : res.filePaths[0] || "" };
}

function browse({ path: rawPath }) {
  let target = (rawPath || "").trim();
  if (!target || !fs.existsSync(target) || !fs.statSync(target).isDirectory()) {
    target = os.homedir();
  }
  target = path.resolve(target);
  let subdirs = [];
  try {
    subdirs = fs
      .readdirSync(target, { withFileTypes: true })
      .filter((e) => e.isDirectory() && !e.name.startsWith(".") && !e.name.startsWith("$"))
      .map((e) => e.name)
      .sort((a, b) => a.localeCompare(b))
      .slice(0, 250);
  } catch {}
  const parent = path.dirname(target) !== target ? path.dirname(target) : null;
  return { current: target, parent, drives: getDrives(), subdirs };
}

function mkdir({ path: base, name }) {
  const clean = (name || "").trim();
  if (!clean || /[<>:"/\\|?*]/.test(clean)) {
    throw new Error("Invalid folder name");
  }
  const target = path.join(base, clean);
  fs.mkdirSync(target, { recursive: true });
  return { path: path.resolve(target) };
}

function read({ path: p }) {
  if (!fs.existsSync(p) || !fs.statSync(p).isFile()) {
    return { content: null };
  }
  return { content: fs.readFileSync(p, "utf-8") };
}

function write({ path: p, content, append }) {
  fs.mkdirSync(path.dirname(p), { recursive: true });
  const existed = fs.existsSync(p);
  if (append && existed) {
    fs.appendFileSync(p, content, "utf-8");
  } else {
    fs.writeFileSync(p, content, "utf-8");
  }
  return { existed };
}

// Binary-safe variants for office documents (.pptx/.xlsx/.docx/.pdf); the
// server edits the bytes in memory and never stores them on its own disk.
const MAX_B64_BYTES = 10 * 1024 * 1024;

function readB64({ path: p }) {
  if (!fs.existsSync(p) || !fs.statSync(p).isFile()) {
    return { data: null };
  }
  const size = fs.statSync(p).size;
  if (size > MAX_B64_BYTES) throw new Error(`File too large for document edit (${size} bytes, max ${MAX_B64_BYTES})`);
  return { data: fs.readFileSync(p).toString("base64"), size };
}

function writeB64({ path: p, data }) {
  const buf = Buffer.from(data || "", "base64");
  if (buf.length > MAX_B64_BYTES) throw new Error(`Document too large (${buf.length} bytes, max ${MAX_B64_BYTES})`);
  fs.mkdirSync(path.dirname(p), { recursive: true });
  const existed = fs.existsSync(p);
  fs.writeFileSync(p, buf);
  return { existed, size: buf.length };
}

function edit({ path: p, old_string, new_string, replace_all }) {
  if (!fs.existsSync(p) || !fs.statSync(p).isFile()) {
    throw new Error(`File not found: ${p}`);
  }
  const text = fs.readFileSync(p, "utf-8");
  const count = text.split(old_string).length - 1;
  if (count === 0) throw new Error(`old_string not found in file: ${p}`);
  if (count > 1 && !replace_all) {
    throw new Error(`old_string appears ${count}x - add replace_all or more context`);
  }
  const updated = replace_all
    ? text.split(old_string).join(new_string)
    : text.replace(old_string, new_string);
  fs.writeFileSync(p, updated, "utf-8");
  return { count };
}

function list({ root, pattern }) {
  const rx = globToRegExp(pattern || "**/*");
  const files = [];
  walk(root, (full) => {
    if (files.length >= 200) return;
    const rel = path.relative(root, full).replace(/\\/g, "/");
    if (rx.test(rel)) files.push(rel);
  });
  return { files: files.slice(0, 200) };
}

function tree({ root, rel }) {
  const ignored = new Set([".git", "__pycache__", "node_modules", ".venv", "venv"]);
  const rootResolved = path.resolve(root);
  const base = rel ? path.resolve(rootResolved, rel) : rootResolved;
  const baseLower = base.toLowerCase();
  const rootLower = rootResolved.toLowerCase();
  if (baseLower !== rootLower && !baseLower.startsWith(rootLower + path.sep)) {
    return { nodes: [] };
  }
  let entries;
  try {
    entries = fs.readdirSync(base, { withFileTypes: true });
  } catch {
    return { nodes: [] };
  }
  entries = entries.filter((e) => !ignored.has(e.name));
  entries.sort((a, b) => {
    if (a.isDirectory() !== b.isDirectory()) return a.isDirectory() ? -1 : 1;
    return a.name.localeCompare(b.name, undefined, { sensitivity: "base" });
  });
  const nodes = entries.map((e) => {
    const full = path.join(base, e.name);
    const rel = path.relative(rootResolved, full).replace(/\\/g, "/");
    if (e.isDirectory()) {
      return { name: e.name, path: rel, dir: true, children: null };
    }
    let size = 0;
    try {
      size = fs.statSync(full).size;
    } catch {}
    return { name: e.name, path: rel, dir: false, size };
  });
  return { nodes };
}

function grep({ root, pattern }) {
  const rx = new RegExp(pattern, "i");
  const hits = [];
  walk(root, (full) => {
    if (hits.length >= 100) return;
    try {
      const stat = fs.statSync(full);
      if (stat.size > 2_000_000) return;
      const rel = path.relative(root, full).replace(/\\/g, "/");
      const lines = fs.readFileSync(full, "utf-8").split(/\r?\n/);
      for (let i = 0; i < lines.length && hits.length < 100; i++) {
        if (rx.test(lines[i])) hits.push(`${rel}:${i + 1}: ${lines[i].trim().slice(0, 200)}`);
      }
    } catch {}
  });
  return { hits };
}

module.exports = { browseFolder, browse, mkdir, read, write, readB64, writeB64, edit, list, grep, tree };

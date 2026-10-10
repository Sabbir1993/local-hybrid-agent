// companion/fsops.js — local filesystem operations invoked by the server over
// the WebSocket bridge (core/companion_bridge.py). Mirrors the shape of the
// server-local implementations in core/agent_tools.py / routes/projects.py
// so the two are drop-in equivalents from the server's point of view.

const fs = require("fs");
const path = require("path");
const os = require("os");
const childProcess = require("child_process");
const { dialog, BrowserWindow } = require("electron");
const { buildChildEnv } = require("./shellops");

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

// build output and vendored code are noise for a symbol index and can hold tens of thousands of files
const INDEX_SKIP_DIR_NAMES = new Set([...SKIP_DIR_NAMES, "dist", "build", "out", "target", "vendor", "site-packages", "coverage"]);

function walk(root, onFile, skipDirs = SKIP_DIR_NAMES) {
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
      if (skipDirs.has(ent.name)) continue;
      walk(full, onFile, skipDirs);
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

const MAX_READ_BYTES = 20 * 1024 * 1024;

function read({ path: p }) {
  if (!fs.existsSync(p) || !fs.statSync(p).isFile()) {
    return { content: null };
  }
  const size = fs.statSync(p).size;
  if (size > MAX_READ_BYTES) throw new Error(`File too large to read (${size} bytes, max ${MAX_READ_BYTES})`);
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

// Delete one file (used to undo a file the agent created). Never a directory.
function remove({ path: p }) {
  if (!fs.existsSync(p)) return { removed: false };
  if (!fs.statSync(p).isFile()) throw new Error("not a file: " + p);
  fs.unlinkSync(p);
  return { removed: true };
}

// Syntax check after the agent writes a file. Only JavaScript needs the device (the server
// checks Python/JSON/YAML/TOML/XML itself). `node --check` parses without running the file.
const CHECKABLE = new Set([".js", ".mjs", ".cjs"]);

function verify({ path: p }) {
  const ext = path.extname(p || "").toLowerCase();
  if (!CHECKABLE.has(ext) || !fs.existsSync(p)) return Promise.resolve({ checked: false });
  return new Promise((resolve) => {
    childProcess.execFile(process.execPath, ["--check", p], {
      timeout: 15000, windowsHide: true, env: { ...buildChildEnv(process.env), ELECTRON_RUN_AS_NODE: "1" },
    }, (err, _out, stderr) => {
      if (!err) return resolve({ checked: true, ok: true });
      if (err.code === "ENOENT" || err.killed) return resolve({ checked: false });
      const lines = String(stderr || err.message).split(/\r?\n/).filter(Boolean);
      const msg = lines.find((l) => /Error/.test(l)) || lines[0] || "syntax error";
      const at = lines.find((l) => /:\d+$/.test(l.trim()));
      resolve({ checked: true, ok: false, detail: (at ? path.basename(at.trim()) + " " : "") + msg.slice(0, 200) });
    });
  });
}

// Lint feedback after the agent edits a file: only the high-signal findings, so a clean edit stays silent and
// a real bug (undefined name, invalid comparison, parse error) reaches the model in the same step.
//   Python     ruff, isolated from project config, rule families E9 (syntax), F63/F7 (invalid comparisons,
//              misplaced statements) and F82 (undefined names)
//   JS/TS      the project's own eslint (node_modules/eslint, run by this app's node), errors only; it needs the
//              project's config, so a project without one is reported as not checked rather than guessed at
// A tool that is not installed, or fails for its own reasons, is `{ checked: false }`: never an error.
const RUFF_SELECT = "E9,F63,F7,F82";
const ESLINT_EXTS = new Set([".js", ".mjs", ".cjs", ".jsx", ".ts", ".tsx"]);
const MAX_ISSUES = 20;

function findEslint(fromDir, root) {
  const stop = root ? path.resolve(root) : path.resolve(fromDir);
  let dir = path.resolve(fromDir);
  for (;;) {
    const cand = path.join(dir, "node_modules", "eslint", "bin", "eslint.js");
    if (fs.existsSync(cand)) return { script: cand, cwd: dir };
    if (dir === stop || path.dirname(dir) === dir) return null;
    if (!dir.toLowerCase().startsWith(stop.toLowerCase())) return null;
    dir = path.dirname(dir);
  }
}

function runTool(file, args, opts) {
  return new Promise((resolve) => {
    childProcess.execFile(file, args, { timeout: 20000, windowsHide: true, maxBuffer: 4 * 1024 * 1024, ...opts },
      (err, stdout, stderr) => resolve({ err, stdout: String(stdout || ""), stderr: String(stderr || "") }));
  });
}

async function diagnose({ path: p, root }) {
  const ext = path.extname(p || "").toLowerCase();
  if (!p || !fs.existsSync(p)) return { checked: false };
  const env = buildChildEnv(process.env);
  if (ext === ".py" || ext === ".pyw") {
    const r = await runTool("ruff", ["check", "--isolated", "--no-fix", "--output-format=json", "--select", RUFF_SELECT, p],
      { cwd: path.dirname(p), env });
    if (r.err && (r.err.code === "ENOENT" || r.err.killed)) return { checked: false, reason: "ruff not installed" };
    let found;
    try { found = JSON.parse(r.stdout || "[]"); } catch { return { checked: false, reason: "ruff output unreadable" }; }
    if (!Array.isArray(found)) return { checked: false };
    const issues = found.slice(0, MAX_ISSUES).map((f) => ({
      line: (f.location && f.location.row) || 0, col: (f.location && f.location.column) || 0,
      code: f.code || "", message: String(f.message || "").slice(0, 160),
    }));
    return { checked: true, tool: "ruff", issues, total: found.length };
  }
  if (ESLINT_EXTS.has(ext)) {
    const es = findEslint(path.dirname(p), root);
    if (!es) return { checked: false, reason: "eslint not installed in the project" };
    const r = await runTool(process.execPath, [es.script, "-f", "json", "--no-warn-ignored", p],
      { cwd: es.cwd, env: { ...env, ELECTRON_RUN_AS_NODE: "1" } });
    if (r.err && r.err.killed) return { checked: false, reason: "eslint timed out" };
    let res;
    try { res = JSON.parse(r.stdout); } catch { return { checked: false, reason: "eslint has no usable config" }; }
    const msgs = ((Array.isArray(res) && res[0] && res[0].messages) || []).filter((m) => m.severity === 2);
    // TypeScript without a TS-aware config fails to parse: that is the config's gap, not a bug in the file
    if (msgs.some((m) => m.fatal) && (ext === ".ts" || ext === ".tsx")) return { checked: false, reason: "eslint cannot parse TypeScript here" };
    const issues = msgs.slice(0, MAX_ISSUES).map((m) => ({
      line: m.line || 0, col: m.column || 0, code: m.ruleId || (m.fatal ? "parse" : ""),
      message: String(m.message || "").slice(0, 160),
    }));
    return { checked: true, tool: "eslint", issues, total: msgs.length };
  }
  return { checked: false };
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

function clampInt(v, lo, hi, dflt) {
  const n = Math.trunc(Number(v));
  return Number.isFinite(n) ? Math.min(hi, Math.max(lo, n)) : dflt;
}

// Source files for the server's symbol index, incrementally. The server sends the [mtimeMs, size] stamps it
// already holds ({rel: [mtime, size]}); only files that are new or changed come back with their text, at most
// `budget_bytes` of text per call (`more: true` means call again). `all` is every matching file, so the server
// can drop the ones that were deleted. Nothing is written anywhere: the device stays the only copy.
function readMany({ root, exts, max_files, max_file_bytes, budget_bytes, known }) {
  const extSet = new Set((Array.isArray(exts) ? exts : []).map((e) => String(e).toLowerCase()));
  const maxFiles = clampInt(max_files, 1, 20000, 5000);
  const maxFileBytes = clampInt(max_file_bytes, 1024, 4 * 1024 * 1024, 512 * 1024);
  const budget = clampInt(budget_bytes, 64 * 1024, 8 * 1024 * 1024, 2 * 1024 * 1024);
  const have = known && typeof known === "object" ? known : {};
  const files = [];
  const all = [];
  let used = 0;
  let more = false;
  let truncated = false;
  let skippedLarge = 0;
  walk(root, (full) => {
    if (!extSet.has(path.extname(full).toLowerCase())) return;
    if (all.length >= maxFiles) { truncated = true; return; }
    let st;
    try { st = fs.statSync(full); } catch { return; }
    if (st.size > maxFileBytes) { skippedLarge++; return; }
    const rel = path.relative(root, full).replace(/\\/g, "/");
    all.push(rel);
    const stamp = [Math.floor(st.mtimeMs), st.size];
    const k = have[rel];
    if (Array.isArray(k) && k[0] === stamp[0] && k[1] === stamp[1]) return;
    if (files.length && used + st.size > budget) { more = true; return; }
    try {
      files.push({ rel, stamp, text: fs.readFileSync(full, "utf-8") });
      used += st.size;
    } catch { all.pop(); }
  }, INDEX_SKIP_DIR_NAMES);
  return { files, all, more, truncated, skipped_large: skippedLarge };
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

const MAX_GREP_PATTERN = 300;

function grep({ root, pattern }) {
  // the pattern is model-chosen and runs on this machine's event loop: keep it short
  // and refuse nested quantifiers like (a+)+ that backtrack catastrophically
  const pat = String(pattern || "");
  if (pat.length > MAX_GREP_PATTERN) throw new Error(`pattern too long (max ${MAX_GREP_PATTERN} chars)`);
  if (/\([^)]*[+*][^)]*\)[+*{]/.test(pat)) throw new Error("pattern has nested quantifiers");
  const rx = new RegExp(pat, "i");
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

module.exports = { browseFolder, browse, mkdir, read, write, readB64, writeB64, edit, remove, verify, list, grep, tree, readMany, diagnose };

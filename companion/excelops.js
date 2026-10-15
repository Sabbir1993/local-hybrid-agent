// excel.recalc: calculate a workbook with the Microsoft Excel installed on THIS PC and hand back the values of its formula cells.
//
// Why here and not on the server: Excel opens macros, external links and DDE. It runs in the user's own desktop session, on a
// temporary copy, with macros force-disabled, links never updated, read-only, hidden, under a hard timeout. The only code that runs
// is companion/excel_recalc.ps1 (shipped with the app); the workbook path is an ARGUMENT, nothing from the server is ever
// executed. The user's file is never opened, changed or saved.
const fs = require("fs");
const os = require("os");
const path = require("path");
const { spawn } = require("child_process");
const shellops = require("./shellops");

const MAX_BYTES = 10 * 1024 * 1024;        // same ceiling as fs.read_b64 / the server's DEVICE_MAX_BYTES
const MAX_CELLS = 100000;
const MAX_JSON_BYTES = 8 * 1024 * 1024;    // below the 16 MB websocket frame
const TIMEOUT_MS = 45000;
// PowerShell cannot read a script inside app.asar: the packaged app unpacks it (package.json asarUnpack)
const SCRIPT = path.join(__dirname, "excel_recalc.ps1").replace("app.asar" + path.sep, "app.asar.unpacked" + path.sep);

function refuse(why) { throw new Error(why); }

// What may be opened: a plain .xlsx zip with no VBA project. (.xlsm / .xlsb / .xls can carry macros or legacy formats.)
function check(buf, name) {
  const ext = path.extname(String(name || "book.xlsx")).toLowerCase();
  if (ext !== ".xlsx") refuse(`only .xlsx workbooks are calculated (not ${ext || "this file"}): macro-enabled and legacy formats are refused`);
  if (!buf || buf.length < 4 || buf[0] !== 0x50 || buf[1] !== 0x4b) refuse("not an .xlsx file (zip header missing)");
  if (buf.length > MAX_BYTES) refuse(`workbook is larger than ${MAX_BYTES / 1024 / 1024} MB`);
  if (buf.includes(Buffer.from("vbaProject.bin"))) refuse("the workbook contains macros (a VBA project): refused");
}

function endExcel(pidFile, run) {
  // Excel is an out-of-process COM server: killing PowerShell would not end it. The script wrote the pid of the Excel it started.
  try {
    const pid = parseInt(fs.readFileSync(pidFile, "ascii").trim(), 10);
    if (pid > 0) (run.spawn || spawn)("taskkill", ["/PID", String(pid), "/T", "/F"], { windowsHide: true, stdio: "ignore" }).on("error", () => {});
  } catch (_) { /* never started, or already gone */ }
}

// deps: injectable for tests {spawn, platform, timeoutMs, tmpdir}
async function recalc(params, deps = {}) {
  const platform = deps.platform || process.platform;
  if (platform !== "win32") refuse("excel not available on this platform (Windows only)");
  const buf = Buffer.from(String(params.data_b64 || ""), "base64");
  check(buf, params.name);
  const dir = fs.mkdtempSync(path.join(deps.tmpdir || os.tmpdir(), "a770-excel-"));
  const book = path.join(dir, "book.xlsx");
  const out = path.join(dir, "out.json");
  const pidFile = path.join(dir, "excel.pid");
  fs.writeFileSync(book, buf);
  const doSpawn = deps.spawn || spawn;
  try {
    const exit = await new Promise((resolve) => {
      let settled = false;
      const child = doSpawn("powershell.exe",
        ["-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-File", deps.script || SCRIPT,
         "-Path", book, "-Out", out, "-PidFile", pidFile, "-MaxCells", String(MAX_CELLS)],
        { windowsHide: true, stdio: ["ignore", "ignore", "pipe"], env: shellops.buildChildEnv(process.env, {}) });
      let err = "";
      if (child.stderr) child.stderr.on("data", (d) => { err = (err + d).slice(-2000); });
      const finish = (v) => { if (!settled) { settled = true; clearTimeout(timer); resolve(v); } };
      const timer = setTimeout(() => {
        shellops.killTree(child);
        endExcel(pidFile, { spawn: doSpawn });
        setTimeout(() => finish({ timedOut: true, err }), 1500);
      }, deps.timeoutMs || TIMEOUT_MS);
      child.on("error", (e) => finish({ code: 1, err: e.message }));
      child.on("close", (code) => finish({ code, err }));
    });
    if (exit.timedOut) refuse(`Excel did not finish within ${(deps.timeoutMs || TIMEOUT_MS) / 1000} s and was closed`);
    let raw;
    try { raw = fs.readFileSync(out); } catch (_) { refuse(`Excel produced no result${exit.err ? ": " + exit.err.trim().slice(0, 200) : ""}`); }
    if (raw.length > MAX_JSON_BYTES) refuse("the result is too large");
    const data = JSON.parse(raw.toString("utf8").replace(/^﻿/, ""));
    if (!data.ok) refuse(`Excel could not calculate this workbook: ${String(data.error || "unknown error").slice(0, 300)}`);
    return { cells: data.cells || {}, formulas: data.formulas || 0, errors: data.errors || 0, version: data.version || "" };
  } finally {
    try { fs.rmSync(dir, { recursive: true, force: true }); } catch (_) { /* best effort */ }
  }
}

module.exports = { recalc, check, MAX_BYTES, MAX_CELLS, TIMEOUT_MS };

// companion/iosops.js — iOS Simulator control for a companion running on macOS
// (Xcode's `xcrun simctl`). Same rules as androidops.js: fixed argv, no shell,
// install/boot confirmed locally. UI tree + taps need Meta's `idb` (optional);
// without it the agent still gets screenshots, logs and app install/launch.
// Physical iPhones need code signing and are not automated.

const { execFile } = require("child_process");
const fs = require("fs");
const os = require("os");
const path = require("path");
const policy = require("./policy");

const UDID_RX = /^[A-F0-9-]{36}$|^booted$/i;
const BUNDLE_RX = /^[A-Za-z0-9.\-]{3,200}$/;

function ensureMac() {
  if (process.platform !== "darwin") {
    throw new Error("iOS simulators need a Mac with Xcode - run the companion on macOS to test iOS apps");
  }
}

function run(file, args, { timeoutMs = 60000, binary = false } = {}) {
  return new Promise((resolve, reject) => {
    execFile(file, args, { timeout: timeoutMs, maxBuffer: 64 * 1024 * 1024, encoding: binary ? "buffer" : "utf8" },
      (err, stdout, stderr) => {
        if (err) return reject(new Error(err.code === "ENOENT" ? `${file} not found (install Xcode / idb)`
                                                               : String(stderr || err.message).trim().slice(0, 600)));
        resolve(stdout);
      });
  });
}

const simctl = (args, opts) => run("xcrun", ["simctl", ...args], opts);

function udid(p) {
  const u = String(p.udid || "booted");
  if (!UDID_RX.test(u)) throw new Error("invalid simulator udid");
  return u;
}

async function devices() {
  ensureMac();
  const data = JSON.parse(await simctl(["list", "devices", "available", "-j"]));
  const out = [];
  for (const [runtime, list] of Object.entries(data.devices || {})) {
    for (const d of list) out.push({ udid: d.udid, name: d.name, state: d.state, runtime: runtime.split(".").pop() });
  }
  let idb = false;
  try { await run("idb", ["--help"], { timeoutMs: 5000 }); idb = true; } catch (_) {}
  return { simulators: out, idb };
}

async function boot(p) {
  ensureMac();
  const u = udid(p);
  await policy.confirmAction("start simulator?", "The AI agent wants to boot this iOS simulator:", u, `ios-boot\n${u}`);
  await simctl(["boot", u]).catch((e) => { if (!/current state: Booted/i.test(e.message)) throw e; });
  await run("open", ["-a", "Simulator"]).catch(() => {});
  await simctl(["bootstatus", u, "-b"], { timeoutMs: 240000 });
  return { udid: u, booted: true };
}

async function install(p) {
  ensureMac();
  const u = udid(p);
  const app = path.resolve(String(p.app || ""));
  if (!/\.app\/?$/.test(app)) throw new Error("app must be a simulator .app bundle (xcodebuild -sdk iphonesimulator)");
  await policy.ensurePath(app, "install on a simulator");
  if (!fs.existsSync(app)) throw new Error(`not found: ${app}`);
  await policy.confirmAction("install app?", `The AI agent wants to install this app on simulator ${u}:`, app, `ios-install\n${u}\n${app}`);
  await simctl(["install", u, app], { timeoutMs: 240000 });
  return { udid: u, app };
}

async function launch(p) {
  ensureMac();
  const u = udid(p);
  const b = String(p.bundle_id || p.package || "");
  if (!BUNDLE_RX.test(b)) throw new Error("invalid bundle id");
  if (p.stop_first) await simctl(["terminate", u, b]).catch(() => {});
  const out = await simctl(["launch", u, b]);
  return { udid: u, bundle_id: b, output: out.trim().slice(-400) };
}

async function screenshot(p) {
  ensureMac();
  const u = udid(p);
  const tmp = path.join(os.tmpdir(), `a770_sim_${Date.now()}.png`);
  try {
    await simctl(["io", u, "screenshot", "--type=png", tmp], { timeoutMs: 20000 });
    const buf = fs.readFileSync(tmp);
    return { udid: u, png_b64: buf.toString("base64"), bytes: buf.length };
  } finally {
    fs.rm(tmp, { force: true }, () => {});
  }
}

let uiRefs = [];

async function uiDump(p) {
  ensureMac();
  const u = udid(p);
  const raw = await run("idb", ["ui", "describe-all", ...(u === "booted" ? [] : ["--udid", u])], { timeoutMs: 30000 });
  const nodes = JSON.parse(raw);
  uiRefs = [];
  const lines = [];
  for (const n of nodes.slice(0, 400)) {
    const f = n.frame || {};
    const label = n.AXLabel || n.AXValue || "";
    if (!label && !n.AXUniqueId) continue;
    uiRefs.push({ x: Math.round(f.x + f.width / 2), y: Math.round(f.y + f.height / 2) });
    lines.push(`[n${uiRefs.length}] ${n.type || n.role || "?"}${label ? ` "${String(label).slice(0, 80)}"` : ""}` +
      `${n.AXUniqueId ? ` id=${n.AXUniqueId}` : ""} @(${uiRefs[uiRefs.length - 1].x},${uiRefs[uiRefs.length - 1].y})`);
  }
  return { udid: u, elements: lines.length, tree: lines.join("\n") };
}

async function tap(p) {
  ensureMac();
  let pt;
  if (p.ref) {
    pt = uiRefs[Number(String(p.ref).replace(/^n/, "")) - 1];
    if (!pt) throw new Error(`unknown ref ${p.ref} - call mobile_ui again`);
  } else {
    pt = { x: Math.round(Number(p.x)), y: Math.round(Number(p.y)) };
    if (!Number.isFinite(pt.x) || !Number.isFinite(pt.y)) throw new Error("give ref or x,y");
  }
  await run("idb", ["ui", "tap", String(pt.x), String(pt.y)]);
  return { tapped: pt };
}

async function text(p) {
  ensureMac();
  const t = String(p.text || "");
  if (!t || t.length > 500) throw new Error("text required (max 500 chars)");
  if (/(?:\d[ -]?){13,19}/.test(t)) throw new Error("refused: that looks like a card number - type test card data yourself");
  if (p.ref || p.x != null) await tap(p);
  await run("idb", ["ui", "text", t]);
  return { typed_chars: t.length };
}

async function logcat(p) {
  ensureMac();
  const u = udid(p);
  const args = ["spawn", u, "log", "show", "--style", "compact", "--last", `${Math.min(Number(p.minutes) || 2, 30)}m`];
  if (p.bundle_id || p.package) {
    const b = String(p.bundle_id || p.package);
    if (!BUNDLE_RX.test(b)) throw new Error("invalid bundle id");
    args.push("--predicate", `subsystem == "${b}" OR process == "${b.split(".").pop()}"`);
  }
  const out = await simctl(args, { timeoutMs: 30000 });
  return { udid: u, log: out.slice(-20000) };
}

module.exports = { devices, boot, install, launch, screenshot, uiDump, tap, text, logcat };

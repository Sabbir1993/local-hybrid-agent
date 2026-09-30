// companion/androidops.js — Android emulator / phone control over adb on this
// machine, for testing mobile apps the agent builds (core/device_tools.py sends
// "android.*" ops).
//
// Every adb call is a fixed argv built here (execFile, no shell): the server can
// pick a device, package, coordinates or text, never an arbitrary adb/shell
// command -- free-form commands still go through shell.run and its approval.
// Installing an APK, booting an emulator and pairing/connecting a phone are
// confirmed on this machine. Typed text that looks like a card number is refused.

const { execFile, spawn } = require("child_process");
const fs = require("fs");
const os = require("os");
const path = require("path");
const policy = require("./policy");

const MAX_LOG_CHARS = 20000;
const SERIAL_RX = /^[\w.:\-]{1,64}$/;
const PKG_RX = /^[A-Za-z][\w]*(\.[A-Za-z][\w]*)+$/;
const ACT_RX = /^[\w.$]{1,200}$/;
const KEY_RX = /^[A-Z0-9_]{1,40}$/;
const HOSTPORT_RX = /^[\w.\-\[\]:]{3,80}:\d{2,5}$/;

let uiRefs = new Map();   // serial -> [{x, y, label}] from the last ui dump

function sdkRoots() {
  const out = [process.env.ANDROID_HOME, process.env.ANDROID_SDK_ROOT];
  if (process.platform === "win32") out.push(path.join(process.env.LOCALAPPDATA || "", "Android", "Sdk"));
  else if (process.platform === "darwin") out.push(path.join(os.homedir(), "Library", "Android", "sdk"));
  else out.push(path.join(os.homedir(), "Android", "Sdk"));
  return out.filter(Boolean);
}

function findTool(sub, exe) {
  const name = process.platform === "win32" ? `${exe}.exe` : exe;
  for (const root of sdkRoots()) {
    const p = path.join(root, sub, name);
    if (fs.existsSync(p)) return p;
  }
  return null;
}

function adbPath() {
  return findTool("platform-tools", "adb") || "adb";     // fall back to PATH
}

function run(file, args, { timeoutMs = 30000, binary = false } = {}) {
  return new Promise((resolve, reject) => {
    execFile(file, args, { timeout: timeoutMs, maxBuffer: 64 * 1024 * 1024, windowsHide: true,
                           encoding: binary ? "buffer" : "utf8" }, (err, stdout, stderr) => {
      if (err && !(stdout && stdout.length)) {
        const msg = err.code === "ENOENT" ? `${path.basename(file)} not found - install Android SDK platform-tools ` +
          "(Android Studio) or set ANDROID_HOME" : String(stderr || err.message).trim().slice(0, 600);
        return reject(new Error(msg));
      }
      resolve({ stdout, stderr: String(stderr || "") });
    });
  });
}

const adb = (args, opts) => run(adbPath(), args, opts);

async function listDevices() {
  const { stdout } = await adb(["devices", "-l"]);
  return stdout.split(/\r?\n/).slice(1).map((l) => l.trim()).filter(Boolean).map((l) => {
    const [serial, state, ...rest] = l.split(/\s+/);
    const kv = Object.fromEntries(rest.map((x) => x.split(":")).filter((x) => x.length === 2));
    return { serial, state, model: kv.model || "", device: kv.device || "", emulator: serial.startsWith("emulator-") };
  });
}

async function pickSerial(p) {
  if (p.serial) {
    if (!SERIAL_RX.test(String(p.serial))) throw new Error("invalid device serial");
    return String(p.serial);
  }
  const ready = (await listDevices()).filter((d) => d.state === "device");
  if (ready.length === 1) return ready[0].serial;
  if (!ready.length) throw new Error("no Android device or emulator is connected - boot an emulator or plug in a phone");
  throw new Error(`several devices connected, pass serial: ${ready.map((d) => d.serial).join(", ")}`);
}

// ---------------- ops ----------------

async function devices() {
  const emu = findTool("emulator", "emulator");
  let avds = [];
  if (emu) {
    try { avds = (await run(emu, ["-list-avds"])).stdout.split(/\r?\n/).map((s) => s.trim()).filter((s) => s && !s.startsWith("INFO")); } catch (_) {}
  }
  let list = [];
  let adbError = null;
  try { list = await listDevices(); } catch (e) { adbError = e.message; }
  return { devices: list, avds, adb: adbPath(), emulator: emu, adb_error: adbError, sdk_roots: sdkRoots() };
}

async function bootAvd(p) {
  const avd = String(p.avd || "");
  if (!/^[\w.\-]{1,100}$/.test(avd)) throw new Error("invalid AVD name");
  const emu = findTool("emulator", "emulator");
  if (!emu) throw new Error("Android emulator not found - install it with Android Studio's SDK Manager");
  await policy.confirmAction("start emulator?", "The AI agent wants to start this Android emulator:",
    `${avd}${p.cold ? " (cold boot)" : ""}`, `avd\n${avd}`, p.approved_in_app === true);
  const before = new Set((await listDevices().catch(() => [])).map((d) => d.serial));
  const args = ["-avd", avd, "-netdelay", "none", "-netspeed", "full"];
  if (p.cold) args.push("-no-snapshot-load");
  if (p.headless) args.push("-no-window");
  const child = spawn(emu, args, { detached: true, stdio: "ignore", windowsHide: false });
  child.unref();
  const deadline = Date.now() + Math.min(Number(p.timeout_s) || 180, 300) * 1000;
  let serial = null;
  while (Date.now() < deadline) {
    await new Promise((r) => setTimeout(r, 3000));
    const list = await listDevices().catch(() => []);
    if (!serial) {
      for (const d of list.filter((d) => d.emulator)) {
        const name = await adb(["-s", d.serial, "emu", "avd", "name"], { timeoutMs: 5000 }).then((r) => r.stdout.split(/\r?\n/)[0].trim()).catch(() => "");
        if (name === avd || (!before.has(d.serial) && !name)) { serial = d.serial; break; }
      }
    }
    if (serial) {
      const booted = await adb(["-s", serial, "shell", "getprop", "sys.boot_completed"], { timeoutMs: 5000 }).then((r) => r.stdout.trim()).catch(() => "");
      if (booted === "1") return { serial, avd, booted: true };
    }
  }
  return { serial, avd, booted: false, note: "emulator is still starting - call mobile_devices again shortly" };
}

async function pair(p) {
  const hp = String(p.host_port || "");
  if (!HOSTPORT_RX.test(hp)) throw new Error("host_port must look like 192.168.1.20:37123");
  if (!/^\d{6}$/.test(String(p.code || ""))) throw new Error("pairing code is the 6-digit code shown on the phone");
  await policy.confirmAction("pair phone?", "The AI agent wants to pair with this phone for wireless debugging:", hp, undefined, p.approved_in_app === true);
  const { stdout } = await adb(["pair", hp, String(p.code)], { timeoutMs: 30000 });
  return { output: stdout.trim().slice(0, 500) };
}

async function connect(p) {
  const hp = String(p.host_port || "");
  if (!HOSTPORT_RX.test(hp)) throw new Error("host_port must look like 192.168.1.20:5555");
  await policy.confirmAction("connect phone?", "The AI agent wants to connect to this device over Wi-Fi:", hp, `connect\n${hp}`, p.approved_in_app === true);
  const { stdout } = await adb(["connect", hp], { timeoutMs: 20000 });
  return { output: stdout.trim().slice(0, 500) };
}

async function install(p) {
  const serial = await pickSerial(p);
  const apk = path.resolve(String(p.apk || ""));
  if (!/\.(apk|aab)$/i.test(apk)) throw new Error("apk must be an .apk file");
  if (/\.aab$/i.test(apk)) throw new Error(".aab bundles can't be installed directly - build an APK (e.g. assembleDebug)");
  await policy.ensurePath(apk, "install on a device");
  if (!fs.existsSync(apk)) throw new Error(`file not found: ${apk}`);
  await policy.confirmAction("install app?", `The AI agent wants to install this app on ${serial}:`, apk, `install\n${serial}\n${apk}`, p.approved_in_app === true);
  const { stdout } = await adb(["-s", serial, "install", "-r", "-t", apk], { timeoutMs: 240000 });
  return { serial, apk, output: stdout.trim().slice(-800) };
}

async function launch(p) {
  const serial = await pickSerial(p);
  const pkg = String(p.package || "");
  if (!PKG_RX.test(pkg)) throw new Error("invalid package name");
  if (p.stop_first) await adb(["-s", serial, "shell", "am", "force-stop", pkg]).catch(() => {});
  let r;
  if (p.activity) {
    const act = String(p.activity);
    if (!ACT_RX.test(act)) throw new Error("invalid activity name");
    r = await adb(["-s", serial, "shell", "am", "start", "-W", "-n", `${pkg}/${act}`]);
  } else {
    r = await adb(["-s", serial, "shell", "monkey", "-p", pkg, "-c", "android.intent.category.LAUNCHER", "1"]);
  }
  return { serial, package: pkg, output: r.stdout.trim().slice(-600) };
}

async function stopApp(p) {
  const serial = await pickSerial(p);
  const pkg = String(p.package || "");
  if (!PKG_RX.test(pkg)) throw new Error("invalid package name");
  await adb(["-s", serial, "shell", "am", "force-stop", pkg]);
  return { serial, package: pkg, stopped: true };
}

async function screenshot(p) {
  const serial = await pickSerial(p);
  const { stdout } = await adb(["-s", serial, "exec-out", "screencap", "-p"], { binary: true, timeoutMs: 20000 });
  if (!stdout || stdout.length < 100) throw new Error("screencap returned no image");
  return { serial, png_b64: stdout.toString("base64"), bytes: stdout.length };
}

function decodeXml(s) {
  return String(s || "").replace(/&quot;/g, '"').replace(/&apos;/g, "'").replace(/&lt;/g, "<").replace(/&gt;/g, ">").replace(/&#10;/g, " ").replace(/&amp;/g, "&");
}

async function uiDump(p) {
  const serial = await pickSerial(p);
  const dev = "/sdcard/a770_ui.xml";
  await adb(["-s", serial, "shell", "uiautomator", "dump", dev], { timeoutMs: 30000 });
  const { stdout } = await adb(["-s", serial, "exec-out", "cat", dev], { timeoutMs: 15000 });
  const refs = [];
  const lines = [];
  const rx = /<node\b([^>]*?)\/?>/g;
  let m;
  while ((m = rx.exec(stdout)) && refs.length < 400) {
    const a = {};
    m[1].replace(/([\w-]+)="([^"]*)"/g, (_, k, v) => { a[k] = decodeXml(v); return ""; });
    const b = /\[(\d+),(\d+)\]\[(\d+),(\d+)\]/.exec(a.bounds || "");
    if (!b) continue;
    const [x1, y1, x2, y2] = b.slice(1).map(Number);
    if (x2 - x1 < 2 || y2 - y1 < 2) continue;
    const label = a.text || a["content-desc"] || "";
    const rid = (a["resource-id"] || "").replace(/^.*:id\//, "");
    const clickable = a.clickable === "true" || a["long-clickable"] === "true";
    const editable = /EditText/.test(a.class || "");
    if (!label && !rid && !clickable && !editable) continue;
    const ref = `n${refs.length + 1}`;
    refs.push({ x: Math.round((x1 + x2) / 2), y: Math.round((y1 + y2) / 2), label });
    const cls = (a.class || "").split(".").pop();
    const flags = [clickable && "clickable", editable && "editable", a.checked === "true" && "checked",
                   a.enabled === "false" && "disabled", a.focused === "true" && "focused",
                   a.password === "true" && "password"].filter(Boolean).join(" ");
    lines.push(`[${ref}] ${cls}${label ? ` "${label.slice(0, 80)}"` : ""}${rid ? ` id=${rid}` : ""} @(${refs[refs.length - 1].x},${refs[refs.length - 1].y})${flags ? " " + flags : ""}`);
  }
  uiRefs.set(serial, refs);
  const pkgM = /package="([^"]+)"/.exec(stdout);
  return { serial, foreground_package: pkgM ? pkgM[1] : "", elements: lines.length, tree: lines.join("\n") };
}

function point(serial, p) {
  if (p.ref) {
    const refs = uiRefs.get(serial) || [];
    const i = Number(String(p.ref).replace(/^\[?n/, "").replace(/\]$/, "")) - 1;
    if (!refs[i]) throw new Error(`unknown ref ${p.ref} - call mobile_ui again`);
    return refs[i];
  }
  const x = Math.round(Number(p.x)), y = Math.round(Number(p.y));
  if (!Number.isFinite(x) || !Number.isFinite(y) || x < 0 || y < 0 || x > 10000 || y > 10000) throw new Error("give ref or x,y");
  return { x, y };
}

async function tap(p) {
  const serial = await pickSerial(p);
  const pt = point(serial, p);
  if (p.long) await adb(["-s", serial, "shell", "input", "swipe", String(pt.x), String(pt.y), String(pt.x), String(pt.y), "800"]);
  else await adb(["-s", serial, "shell", "input", "tap", String(pt.x), String(pt.y)]);
  return { serial, tapped: pt };
}

async function swipe(p) {
  const serial = await pickSerial(p);
  const nums = ["x1", "y1", "x2", "y2"].map((k) => Math.round(Number(p[k])));
  if (nums.some((n) => !Number.isFinite(n) || n < 0 || n > 10000)) throw new Error("x1,y1,x2,y2 required");
  const ms = Math.min(Math.max(Math.round(Number(p.duration_ms) || 300), 50), 5000);
  await adb(["-s", serial, "shell", "input", "swipe", ...nums.map(String), String(ms)]);
  return { serial, swiped: nums };
}

function luhnLike(s) {
  const runs = String(s || "").match(/(?:\d[ -]?){13,19}/g) || [];
  return runs.some((r) => {
    const d = r.replace(/\D/g, "");
    if (d.length < 13 || d.length > 19) return false;
    let sum = 0;
    for (let i = 0; i < d.length; i++) {
      let n = +d[d.length - 1 - i];
      if (i % 2) { n *= 2; if (n > 9) n -= 9; }
      sum += n;
    }
    return sum % 10 === 0;
  });
}

async function text(p) {
  const serial = await pickSerial(p);
  const t = String(p.text || "");
  if (!t) throw new Error("text required");
  if (t.length > 500) throw new Error("text too long (500 chars max)");
  if (luhnLike(t)) throw new Error("refused: that looks like a payment card number - type test card data yourself");
  if (p.ref || p.x != null) await tap({ ...p, serial });
  // `input text` runs through the device shell: %s is a space, shell metacharacters are escaped
  const esc = t.replace(/([\\'"`$&|;<>()*~#!?\[\]{}^])/g, "\\$1").replace(/ /g, "%s");
  await adb(["-s", serial, "shell", "input", "text", esc]);
  if (p.submit) await adb(["-s", serial, "shell", "input", "keyevent", "KEYCODE_ENTER"]);
  return { serial, typed_chars: t.length };
}

async function key(p) {
  const serial = await pickSerial(p);
  let k = String(p.key || "").toUpperCase();
  if (!KEY_RX.test(k)) throw new Error("key must be like BACK, HOME, ENTER, APP_SWITCH or KEYCODE_xxx");
  if (!k.startsWith("KEYCODE_")) k = "KEYCODE_" + k;
  await adb(["-s", serial, "shell", "input", "keyevent", k]);
  return { serial, key: k };
}

async function logcat(p) {
  const serial = await pickSerial(p);
  const lines = Math.min(Math.max(Number(p.lines) || 200, 10), 2000);
  const args = ["-s", serial, "logcat", "-d", "-t", String(lines), "-v", "brief"];
  if (p.crash) args.push("-b", "crash");
  if (p.package) {
    if (!PKG_RX.test(String(p.package))) throw new Error("invalid package name");
    const pid = await adb(["-s", serial, "shell", "pidof", String(p.package)]).then((r) => r.stdout.trim().split(/\s+/)[0]).catch(() => "");
    if (pid && /^\d+$/.test(pid) && !p.crash) args.push(`--pid=${pid}`);
  }
  if (p.errors_only) args.push("*:E");
  const { stdout } = await adb(args, { timeoutMs: 20000 });
  return { serial, log: String(stdout).slice(-MAX_LOG_CHARS) };
}

module.exports = { devices, bootAvd, pair, connect, install, launch, stopApp, screenshot, uiDump, tap, swipe, text, key, logcat };

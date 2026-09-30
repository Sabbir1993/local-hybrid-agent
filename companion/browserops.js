// companion/browserops.js — a browser the AI agent can drive on this machine
// (core/browser_tools.py sends "browser.*" ops). It exists so the agent can open
// the app it just built, read the page, click through it and see console/network
// errors -- the "look at what you built" loop.
//
// Safety (this machine decides, not the server):
//  - Uses the Edge/Chrome already installed (playwright-core, no browser download),
//    always in a fresh throwaway context: never the user's profile, cookies or
//    saved passwords.
//  - Local dev hosts (localhost, 127.0.0.1, [::1], *.localhost, *.test) open without
//    asking; any other site needs a one-time Allow per origin (policy.confirmOrigin).
//    file:// only inside approved workspace folders.
//  - Never types into password / payment-card fields, and never types anything that
//    looks like a card number (PCI-DSS).
//  - page JS (browser.eval) is confirmed by the user every time.

const { chromium, devices } = require("playwright-core");
const policy = require("./policy");

const IDLE_CLOSE_MS = 15 * 60 * 1000;
const MAX_SNAPSHOT_CHARS = 30000;
const MAX_LOG = 200;
const MAX_TEXT = 4000;

let browser = null;
let browserHeadless = null;
const sessions = new Map();   // session id -> {context, page, console[], network[], device, timer}

const DEVICE_PRESETS = {
  "desktop": null,
  "iphone-14": "iPhone 14",
  "iphone-se": "iPhone SE",
  "pixel-7": "Pixel 7",
  "galaxy-s9": "Galaxy S9+",
  "ipad": "iPad (gen 7)",
};

async function launch(headless) {
  if (browser && browser.isConnected() && browserHeadless === headless) return browser;
  if (browser) { try { await browser.close(); } catch (_) {} }
  let lastErr = null;
  for (const channel of ["msedge", "chrome"]) {
    try {
      browser = await chromium.launch({ channel, headless });
      browserHeadless = headless;
      browser.on("disconnected", () => { browser = null; sessions.clear(); });
      return browser;
    } catch (e) { lastErr = e; }
  }
  throw new Error("no Microsoft Edge or Google Chrome found on this machine: " + (lastErr && lastErr.message));
}

function isLocalHost(host) {
  const h = String(host || "").replace(/^\[|\]$/g, "").toLowerCase();
  return h === "localhost" || h === "127.0.0.1" || h === "::1" || h.endsWith(".localhost") || h.endsWith(".test");
}

async function checkUrl(raw, approvedInApp) {
  let u;
  try { u = new URL(String(raw || "")); } catch (_) { throw new Error(`not a valid URL: ${raw}`); }
  if (u.protocol === "about:" && u.href === "about:blank") return u;
  if (u.protocol === "file:") {
    const p = decodeURIComponent(u.pathname.replace(/^\/([A-Za-z]:)/, "$1"));
    await policy.ensurePath(p, "open in the agent browser");
    return u;
  }
  if (u.protocol !== "http:" && u.protocol !== "https:") throw new Error(`blocked URL scheme: ${u.protocol}`);
  if (!isLocalHost(u.hostname)) await policy.confirmOrigin(u.origin, approvedInApp === true);
  return u;
}

function push(buf, item) {
  buf.push(item);
  if (buf.length > MAX_LOG) buf.splice(0, buf.length - MAX_LOG);
}

async function newContext(sess, deviceKey, headless) {
  const b = await launch(headless);
  const devName = DEVICE_PRESETS[deviceKey || "desktop"];
  const opts = devName && devices[devName] ? { ...devices[devName] } : { viewport: { width: 1280, height: 800 } };
  const context = await b.newContext({ ...opts, acceptDownloads: false, serviceWorkers: "block" });
  // top-level navigations to a new origin (link clicks, redirects) pass the same check
  await context.route("**/*", async (route) => {
    const req = route.request();
    if (req.isNavigationRequest() && req.frame() === req.frame().page().mainFrame()) {
      try { await checkUrl(req.url()); } catch (e) { return route.abort("blockedbyclient"); }
    }
    return route.continue();
  });
  const page = await context.newPage();
  page.on("console", (m) => {
    if (["error", "warning"].includes(m.type())) push(sess.console, { type: m.type(), text: m.text().slice(0, 500), at: Date.now() });
  });
  page.on("pageerror", (e) => push(sess.console, { type: "pageerror", text: String(e && e.message || e).slice(0, 500), at: Date.now() }));
  page.on("requestfailed", (r) => push(sess.network, { url: r.url().slice(0, 300), method: r.method(), failure: (r.failure() || {}).errorText || "failed" }));
  page.on("response", (r) => {
    if (r.status() >= 400) push(sess.network, { url: r.url().slice(0, 300), method: r.request().method(), status: r.status() });
  });
  sess.context = context;
  sess.page = page;
  sess.device = deviceKey || "desktop";
  sess.headless = headless;
}

function touch(sess, id) {
  clearTimeout(sess.timer);
  sess.timer = setTimeout(() => close({ session: id }), IDLE_CLOSE_MS);
}

async function getSession(p, create = true) {
  const id = String(p.session || "default");
  let sess = sessions.get(id);
  if (!sess && !create) throw new Error("no browser open - call browser_navigate first");
  if (!sess) {
    sess = { console: [], network: [], timer: null };
    await newContext(sess, p.device, p.headless !== false);
    sessions.set(id, sess);
  }
  touch(sess, id);
  return sess;
}

async function pageInfo(sess) {
  return { url: sess.page.url(), title: await sess.page.title().catch(() => ""), device: sess.device };
}

async function snapshotText(sess) {
  let snap = await sess.page.ariaSnapshot({ mode: "ai", timeout: 10000 });
  if (snap.length > MAX_SNAPSHOT_CHARS) snap = snap.slice(0, MAX_SNAPSHOT_CHARS) + `\n… (snapshot truncated, ${snap.length} chars)`;
  return snap;
}

function target(sess, p) {
  if (p.ref) return sess.page.locator(`aria-ref=${String(p.ref).replace(/^\[?ref=/, "").replace(/\]$/, "")}`);
  if (p.selector) return sess.page.locator(String(p.selector)).first();
  if (p.text) return sess.page.getByText(String(p.text), { exact: false }).first();
  throw new Error("give ref (from browser_snapshot), selector or text");
}

// ---------------- card / password guard ----------------

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

async function assertTypable(loc, text) {
  if (luhnLike(text)) throw new Error("refused: that looks like a payment card number - type test card data yourself");
  const info = await loc.evaluate((el) => ({
    type: (el.getAttribute("type") || "").toLowerCase(),
    ac: (el.getAttribute("autocomplete") || "").toLowerCase(),
    name: `${el.getAttribute("name") || ""} ${el.id || ""} ${el.getAttribute("aria-label") || ""}`.toLowerCase(),
  })).catch(() => ({ type: "", ac: "", name: "" }));
  if (info.type === "password" || info.ac.includes("password")) {
    throw new Error("refused: the agent never types into password fields - enter test credentials yourself");
  }
  if (info.ac.startsWith("cc-") || / cc-|\bcc(num|number|v|c)\b|card.?(num|no)|cvv|cvc|\bpan\b/.test(" " + info.name)) {
    throw new Error("refused: payment-card field - enter sandbox card data yourself");
  }
}

// ---------------- ops ----------------

async function navigate(p) {
  const u = await checkUrl(p.url, p.approved_in_app === true);
  const sess = await getSession(p);
  if ((p.device && p.device !== sess.device) || (p.headless !== undefined && (p.headless !== false) !== sess.headless)) {
    await sess.context.close().catch(() => {});
    await newContext(sess, p.device || sess.device, p.headless !== undefined ? p.headless !== false : sess.headless);
  }
  sess.console.length = 0;
  sess.network.length = 0;
  let status = null;
  try {
    const resp = await sess.page.goto(u.href, { waitUntil: "domcontentloaded", timeout: Math.min(Number(p.timeout_ms) || 30000, 90000) });
    status = resp ? resp.status() : null;
    await sess.page.waitForLoadState("networkidle", { timeout: 5000 }).catch(() => {});
    if (!sess.headless) await sess.page.bringToFront().catch(() => {});   // so the test window is actually seen
  } catch (e) {
    return { ...(await pageInfo(sess)), error: String(e.message || e).split("\n")[0] };
  }
  return { ...(await pageInfo(sess)), status, snapshot: await snapshotText(sess) };
}

async function snapshot(p) {
  const sess = await getSession(p, false);
  return { ...(await pageInfo(sess)), snapshot: await snapshotText(sess) };
}

async function click(p) {
  const sess = await getSession(p, false);
  await target(sess, p).click({ timeout: 10000, button: p.button === "right" ? "right" : "left",
                                clickCount: p.double ? 2 : 1 });
  await sess.page.waitForLoadState("domcontentloaded", { timeout: 5000 }).catch(() => {});
  await sess.page.waitForTimeout(300);
  return { ...(await pageInfo(sess)), snapshot: await snapshotText(sess) };
}

async function type(p) {
  const sess = await getSession(p, false);
  const loc = target(sess, p);
  const text = String(p.text_value != null ? p.text_value : p.value || "");
  await assertTypable(loc, text);
  if (p.clear !== false) await loc.fill(text, { timeout: 10000 });
  else await loc.pressSequentially(text, { timeout: 10000 });
  if (p.submit) await loc.press("Enter");
  await sess.page.waitForTimeout(300);
  return { ...(await pageInfo(sess)), snapshot: await snapshotText(sess) };
}

async function press(p) {
  const sess = await getSession(p, false);
  await sess.page.keyboard.press(String(p.key || "Enter"));
  await sess.page.waitForTimeout(300);
  return { ...(await pageInfo(sess)), snapshot: await snapshotText(sess) };
}

async function select(p) {
  const sess = await getSession(p, false);
  await target(sess, p).selectOption(Array.isArray(p.values) ? p.values.map(String) : String(p.values || p.value_option || ""));
  return { ...(await pageInfo(sess)), snapshot: await snapshotText(sess) };
}

async function screenshot(p) {
  const sess = await getSession(p, false);
  const buf = await sess.page.screenshot({ type: "png", fullPage: !!p.full_page, timeout: 15000,
                                           animations: "disabled", caret: "hide" });
  return { ...(await pageInfo(sess)), png_b64: buf.toString("base64"), bytes: buf.length };
}

async function consoleLog(p) {
  const sess = await getSession(p, false);
  return { ...(await pageInfo(sess)), console: sess.console.slice(-100), network: sess.network.slice(-100) };
}

async function evaluate(p) {
  const sess = await getSession(p, false);
  const code = String(p.expression || "");
  if (!code.trim()) throw new Error("expression required");
  await policy.confirmScript(code, sess.page.url(), p.approved_in_app === true);
  const out = await sess.page.evaluate(`(async () => (${code}))()`);
  let text;
  try { text = JSON.stringify(out, null, 1); } catch (_) { text = String(out); }
  return { ...(await pageInfo(sess)), result: String(text).slice(0, MAX_TEXT) };
}

async function waitFor(p) {
  const sess = await getSession(p, false);
  const ms = Math.min(Number(p.timeout_ms) || 10000, 60000);
  if (p.text) await sess.page.getByText(String(p.text)).first().waitFor({ timeout: ms });
  else await sess.page.waitForTimeout(Math.min(ms, 10000));
  return { ...(await pageInfo(sess)), snapshot: await snapshotText(sess) };
}

async function close(p) {
  const id = String((p && p.session) || "default");
  const sess = sessions.get(id);
  if (sess) {
    clearTimeout(sess.timer);
    sessions.delete(id);
    await sess.context.close().catch(() => {});
  }
  if (!sessions.size && browser) { const b = browser; browser = null; await b.close().catch(() => {}); }
  return { closed: !!sess };
}

module.exports = {
  navigate, snapshot, click, type, press, select, screenshot,
  console: consoleLog, evaluate, waitFor, close,
  DEVICE_PRESETS: Object.keys(DEVICE_PRESETS),
};

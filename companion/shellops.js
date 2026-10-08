// companion/shellops.js — runs a shell command on this machine. Mirrors
// core/shell_tools.py:tool_run_shell's local-execution branch; the server
// still owns all allow/deny policy and only sends a command here once it has
// already decided to run it.

const { exec, spawn } = require("child_process");
const net = require("net");

const MAX_OUTPUT_CHARS = 20000;

// What a command the AGENT wrote is allowed to see in the environment.
// Both spawn sites used to pass `env: { ...process.env }`, so every secret in
// the user's session environment (cloud keys, GITHUB_TOKEN, DB passwords) was
// readable by code the model wrote - and run_python is arbitrary Python.
// See TIER1_SANDBOX_SPIKE.md §6.
//
// Two policies, because both have real users:
//   deny  (default) an explicit deny-list of secret-shaped names; everything
//         else is dropped too, so a secret nobody thought to list does not pass
//   allow            an explicit allow-list - nothing is inherited by accident
// Either way CI=1 is injected (npm/pytest behaviour the caller relies on) and
// an `allow` list overrides both, so `git push` can be given GITHUB_TOKEN
// without re-enabling the rest.
const SECRET_NAME_RX = /(SECRET|TOKEN|PASSWORD|PASSWD|APIKEY|API_KEY|PRIVATE|CREDENTIAL|SESSION_KEY|ACCESS_KEY|_KEY$|^KEY$)/i;
// Vars a build cannot run without: dropping these breaks npm/pip/git/ssl.
const ALWAYS_KEEP = ["PATH", "HOME", "SYSTEMROOT", "HOMEDRIVE", "HOMEPATH", "TEMP", "TMP",
  "APPDATA", "LOCALAPPDATA", "PROGRAMDATA", "PROGRAMFILES", "PROGRAMFILES(X86)", "USERPROFILE",
  "COMSPEC", "PATHEXT", "SYSTEMDRIVE", "WINDIR", "NUMBER_OF_PROCESSORS", "PROCESSOR_ARCHITECTURE",
  "HTTP_PROXY", "HTTPS_PROXY", "NO_PROXY", "SSL_CERT_FILE", "SSL_CERT_DIR", "NODE_EXTRA_CA_CERTS",
  "LANG", "LC_ALL", "TZ", "PYTHONPATH", "VIRTUAL_ENV", "NODE_PATH", "CARGO_HOME", "RUSTUP_HOME"];

function buildChildEnv(sourceEnv, opts = {}) {
  const src = sourceEnv || opts.parentEnv || {};
  const mode = opts.mode === "allow" ? "allow" : "deny";   // unknown mode -> deny
  const allow = new Set((opts.allow || []).map((n) => String(n).toUpperCase()));
  const out = {};
  for (const [key, value] of Object.entries(src)) {
    const upper = key.toUpperCase();
    if (value === undefined) continue;
    if (allow.has(upper)) { out[key] = value; continue; }     // explicit escape hatch
    if (mode === "allow") continue;                           // ONLY what was named
    // deny mode: keep the build-critical names, never a secret-shaped one
    if (ALWAYS_KEEP.includes(upper) && !SECRET_NAME_RX.test(upper)) out[key] = value;
  }
  out.CI = "1";
  return out;
}

// The live policy: env var COMPANION_ENV_POLICY=allow switches the companion to
// allow-list mode; COMPANION_ENV_ALLOW=NAME,NAME re-admits individual vars.
function activeEnvPolicy() {
  const mode = process.env.COMPANION_ENV_POLICY === "allow" ? "allow" : "deny";
  const allow = (process.env.COMPANION_ENV_ALLOW || "").split(",").map((s) => s.trim()).filter(Boolean);
  return { mode, allow };
}

function portOpen(port) {
  return new Promise((resolve) => {
    const sock = net.connect({ port, host: "127.0.0.1" });
    sock.once("connect", () => { sock.destroy(); resolve(true); });
    sock.once("error", () => resolve(false));
    sock.setTimeout(800, () => { sock.destroy(); resolve(false); });
  });
}

function resolveShell(sh) {
  if (process.platform === "win32") {
    const s = String(sh || "").toLowerCase().trim();
    if (s === "cmd") return "cmd.exe";
    if (s === "pwsh") return "pwsh.exe";
    if (s === "bash") return "bash.exe";
    if (s === "powershell" || !s) return "powershell.exe";
    return s;
  }
  if (sh) {
    const s = String(sh).toLowerCase().trim();
    if (s === "bash") return "/bin/bash";
    if (s === "sh") return "/bin/sh";
    return s;
  }
  return undefined;
}

// Start a long-running command (a dev server) detached from this call and report once it is
// listening (`wait_for_port`) or after `wait` seconds. The process keeps running afterwards; the
// caller stops it with the returned pid (Windows: taskkill /PID <pid> /T /F).
function runBackground({ command, cwd, wait_for_port, wait, shell }) {
  return new Promise((resolve) => {
    let out = "";
    const resolvedShell = resolveShell(shell);
    const child = spawn(command, {
      cwd: cwd || undefined,
      shell: resolvedShell || true,
      detached: true,
      windowsHide: true,
      stdio: ["ignore", "pipe", "pipe"],
      env: buildChildEnv(process.env, activeEnvPolicy()),
    });
    const grab = (d) => { out = (out + d).slice(-4000); };
    child.stdout.on("data", grab);
    child.stderr.on("data", grab);
    let exited = null;
    child.on("exit", (code) => { exited = code == null ? 1 : code; });
    child.on("error", (e) => { out += "\n" + e.message; exited = 1; });
    child.unref();
    const limit = Math.min(Math.max(Number(wait) || 20, 1), 120) * 1000;
    const port = Number(wait_for_port) || 0;
    const started = Date.now();
    const tick = async () => {
      const up = port ? await portOpen(port) : false;
      if (exited !== null || up || Date.now() - started >= limit) {
        const status = exited !== null ? `exited with code ${exited}`
          : up ? `listening on port ${port}`
          : port ? `still running, port ${port} not open after ${Math.round(limit / 1000)}s` : "running";
        resolve({
          exit_code: exited !== null ? exited : 0,
          stdout: `background process pid ${child.pid}: ${status}` + (out ? `\n${out}` : ""),
          stderr: "",
        });
      } else setTimeout(tick, 500);
    };
    tick();
  });
}

function run({ command, cwd, timeout, background, wait_for_port, wait, shell }) {
  if (background) return runBackground({ command, cwd, wait_for_port, wait, shell });
  const resolvedShell = resolveShell(shell);
  return new Promise((resolve) => {
    const child = exec(
      command,
      {
        cwd: cwd || undefined,
        timeout: timeout ? timeout * 1000 : undefined,
        maxBuffer: 1024 * 1024 * 20,
        env: buildChildEnv(process.env, activeEnvPolicy()),
        shell: resolvedShell,
      },
      (error, stdout, stderr) => {
        resolve({
          exit_code: error ? (typeof error.code === "number" ? error.code : 1) : 0,
          stdout: (stdout || "").slice(-MAX_OUTPUT_CHARS),
          stderr: (stderr || "").slice(-4000) + (error && error.killed ? "\n(killed: timeout)" : ""),
        });
      }
    );
  });
}

module.exports = { run, buildChildEnv, resolveShell };

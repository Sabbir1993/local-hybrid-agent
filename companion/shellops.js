// companion/shellops.js — runs a shell command on this machine. Mirrors
// core/shell_tools.py:tool_run_shell's local-execution branch; the server
// still owns all allow/deny policy and only sends a command here once it has
// already decided to run it.

const { exec, spawn } = require("child_process");
const net = require("net");

const MAX_OUTPUT_CHARS = 20000;

function portOpen(port) {
  return new Promise((resolve) => {
    const sock = net.connect({ port, host: "127.0.0.1" });
    sock.once("connect", () => { sock.destroy(); resolve(true); });
    sock.once("error", () => resolve(false));
    sock.setTimeout(800, () => { sock.destroy(); resolve(false); });
  });
}

// Start a long-running command (a dev server) detached from this call and report once it is
// listening (`wait_for_port`) or after `wait` seconds. The process keeps running afterwards; the
// caller stops it with the returned pid (Windows: taskkill /PID <pid> /T /F).
function runBackground({ command, cwd, wait_for_port, wait }) {
  return new Promise((resolve) => {
    let out = "";
    const child = spawn(command, {
      cwd: cwd || undefined, shell: true, detached: true, windowsHide: true,
      stdio: ["ignore", "pipe", "pipe"], env: { ...process.env, CI: "1" },
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

function run({ command, cwd, timeout, background, wait_for_port, wait }) {
  if (background) return runBackground({ command, cwd, wait_for_port, wait });
  return new Promise((resolve) => {
    const child = exec(
      command,
      {
        cwd: cwd || undefined,
        timeout: timeout ? timeout * 1000 : undefined,
        maxBuffer: 1024 * 1024 * 20,
        env: { ...process.env, CI: "1" },
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

module.exports = { run };

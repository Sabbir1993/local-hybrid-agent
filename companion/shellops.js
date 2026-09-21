// companion/shellops.js — runs a shell command on this machine. Mirrors
// core/shell_tools.py:tool_run_shell's local-execution branch; the server
// still owns all allow/deny policy and only sends a command here once it has
// already decided to run it.

const { exec } = require("child_process");

const MAX_OUTPUT_CHARS = 20000;

function run({ command, cwd, timeout }) {
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

// companion/gitops.js - local git operations invoked by the server over the
// WebSocket bridge (core/companion_bridge). Mirrors shellops.js: the server
// owns allow/deny and only sends args here after deciding the command is
// allowed; this file does the execution on the user's machine.
//
// Destructive-operation guard (defense in depth, not the only gate - the
// server keeps its own allow-list too): a git command that can destroy history
// or reach outside the repo is refused here regardless of what the server
// asked, because "server compromised" is exactly the case this file exists for.

const { execFile } = require("child_process");
const { buildChildEnv } = require("./shellops");

const MAX_OUTPUT_CHARS = 20000;
// git args that can destroy history or rewrite the repo. Refused on this
// machine, period. reset --soft HEAD is fine (moves the index only); --hard
// and --mixed are not. clean -f / -d delete untracked/ignored files.
// Matched per-argument (exact arg or arg+prefix), because subcommands like
// `push` and `branch` are safe in their plain form and destructive only with a
// specific flag. The `rest` matchers scan a whole arg pair ("push --force").
const BLOCKED_ARG_RX = [
  /^--hard$/,
  /^reset(-mixed)?$/,
  /^clean$/,
  /^-f$/,
  /^filter-branch$/,
  /^--delete$/,
  /^--force$/,
  /^update-ref$/,
];
// multi-arg patterns that only make sense as a pair - the joined arg line.
const BLOCKED_LINE_RX = [
  /\bbranch\b.*\s(-d|--delete|-D)\b/,
  /\bpush\b.*\s(--force|--force-with-lease|-f)\b/,
];

function isBlocked(args) {
  if (!Array.isArray(args)) return true;
  if (BLOCKED_ARG_RX.some((rx) => args.some((a) => rx.test(String(a))))) return true;
  const joined = " " + args.join(" ");
  return BLOCKED_LINE_RX.some((rx) => rx.test(joined));
}

function run({ args, cwd }) {
  return new Promise((resolve) => {
    if (!Array.isArray(args) || args.length === 0 || typeof cwd !== "string" || !cwd) {
      resolve({ exit_code: 2, stdout: "", stderr: "bad git.run params: args[] and cwd required" });
      return;
    }
    if (isBlocked(args)) {
      resolve({ exit_code: 2, stdout: "", stderr: "refused by companion: destructive git op" });
      return;
    }
    execFile(
      "git",
      args,
      { cwd, encoding: "utf8", maxBuffer: 1024 * 1024 * 20,
        env: buildChildEnv(process.env, activeEnvPolicy()) },
      (error, stdout, stderr) => {
        const code = error ? (typeof error.code === "number" ? error.code : 1) : 0;
        resolve({
          exit_code: code,
          stdout: (stdout || "").slice(-MAX_OUTPUT_CHARS),
          stderr: (stderr || "").slice(-4000),
        });
      },
    );
  });
}

// Reuse the same env policy as shellops: deny by default, allow-list switchable.
function activeEnvPolicy() {
  return {
    mode: process.env.COMPANION_ENV_POLICY === "allow" ? "allow" : "deny",
    allow: (process.env.COMPANION_ENV_ALLOW || "").split(",").map((s) => s.trim()).filter(Boolean),
  };
}

module.exports = { run, isBlocked };

// tests/js/run_all.js - run every tests/js/test_*.js with node, aggregate exit codes.
//
// Each test file is a plain `node file.js` script that throws on assertion failure
// (nonzero exit) and prints OK on success. There was no runner: PROJECT_KNOWLEDGE.md
// said to run them by hand, so nothing aggregated results and CI could not gate on them.
//
// Usage: node tests/js/run_all.js [--filter <substring>]
// Exit: 0 iff every selected file exits 0.
const { execFile } = require("child_process");
const fs = require("fs");
const path = require("path");

const dir = __dirname;
const filter = process.argv.includes("--filter")
  ? process.argv[process.argv.indexOf("--filter") + 1]
  : null;

const files = fs.readdirSync(dir)
  .filter((f) => f.startsWith("test_") && f.endsWith(".js") && f !== "run_all.js")
  .filter((f) => !filter || f.includes(filter))
  .sort();

if (files.length === 0) {
  console.error("run_all: no test files selected");
  process.exit(2);
}

function runOne(file) {
  return new Promise((resolve) => {
    const t0 = Date.now();
    execFile(process.execPath, [path.join(dir, file)], { timeout: 120000 }, (err, stdout, stderr) => {
      resolve({ file, ok: !err, ms: Date.now() - t0, out: (stdout + stderr).trim().split("\n").slice(-3) });
    });
  });
}

(async () => {
  let failed = 0;
  for (const file of files) {
    // sequential: some tests bind ports / touch the same stub DOM
    const r = await runOne(file);
    console.log(`  [${r.ok ? "PASS" : "FAIL"}] ${file} (${r.ms}ms)`);
    if (!r.ok) {
      failed += 1;
      for (const line of r.out) console.log(`      ${line}`);
    }
  }
  console.log(`run_all: ${files.length - failed}/${files.length} passed`);
  process.exit(failed ? 1 : 0);
})();

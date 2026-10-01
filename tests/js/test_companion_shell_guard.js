// node tests/js/test_companion_shell_guard.js - policy.runPythonScript:
// the shell.run approval check must fire for exactly the run_python command
// shapes the server sends (fixed legacy name + unique per-call names), and for
// nothing else -- a looser match would skip the on-disk == approved-code check,
// a tighter one would silently drop it for new servers.
const assert = require('assert');
const path = require('path');

const Module = require('module');
const origLoad = Module._load;
Module._load = function (request, ...rest) {
  if (request === 'electron') return { dialog: {}, BrowserWindow: { getFocusedWindow: () => null, getAllWindows: () => [] }, app: { getPath: () => '.' } };
  return origLoad.call(this, request, ...rest);
};

const policy = require(path.join(__dirname, '..', '..', 'companion', 'policy.js'));

// both server generations are recognised...
assert.strictEqual(policy.runPythonScript('python "_agent_run.py"'), '_agent_run.py');
assert.strictEqual(policy.runPythonScript('python "_agent_run_1a2b3c4d.py"'), '_agent_run_1a2b3c4d.py');

// ...and everything else is not a run_python invocation
for (const bad of [
  'python "../evil.py"',
  'python "_agent_run.py" && rm -rf ~',
  'python _agent_run.py',
  'python "_agent_run_.py"',
  'python "_agent_run_123456789.py"',
  'python "_agent_run_1A2B3C4D.py"',
  'python "_agent_run_1a2b3c4d.py" --fast',
  'python3 "_agent_run_1a2b3c4d.py"',
  'dir',
  '',
  null,
  undefined,
]) {
  assert.strictEqual(policy.runPythonScript(bad), null, JSON.stringify(bad));
}

console.log('companion shell guard tests passed');

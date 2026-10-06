// tests/js/test_companion_gitops.js - the companion's destructive-git guard.
// gitops.js refuses history-destroying forms on this machine even if the
// server's own allow-list is bypassed (defense in depth - a compromised server
// is exactly the case the local guard exists for). Allowed forms must pass.
// Run: node tests/js/test_companion_gitops.js
const assert = require('assert');
const path = require('path');

const Module = require('module');
const origLoad = Module._load;
Module._load = function (request, ...rest) {
  if (request === 'electron') return { dialog: {}, BrowserWindow: { getFocusedWindow: () => null, getAllWindows: () => [] }, app: { getPath: () => '.' } };
  return origLoad.call(this, request, ...rest);
};

const { isBlocked, run } = require(path.join(__dirname, '..', '..', 'companion', 'gitops.js'));

// destructive forms refused
for (const args of [
  ['reset', '--hard', 'HEAD'],
  ['reset', '--mixed', 'HEAD'],
  ['clean', '-fd'],
  ['clean', '-f'],
  ['filter-branch', '--force'],
  ['branch', '-D', 'x'],
  ['branch', '-d', 'x'],
  ['push', '--force', 'origin', 'main'],
  ['push', 'origin', 'main', '--force-with-lease'],
  ['update-ref', '--stdin'],
]) {
  assert.ok(isBlocked(args), `expected blocked: ${args.join(' ')}`);
}

// safe forms pass
for (const args of [
  ['status', '--porcelain=v1', '-b'],
  ['branch'],
  ['rev-parse', '--abbrev-ref', 'HEAD'],
  ['diff', '--cached'],
  ['add', '--', 'a.py'],
  ['commit', '-m', 'fix'],
  ['restore', '--staged', '--', 'a.py'],
  ['push', 'origin', 'main'],
  ['push', '--set-upstream', 'origin', 'feature'],
  ['log', '--oneline', '-5'],
]) {
  assert.ok(!isBlocked(args), `expected allowed: ${args.join(' ')}`);
}

// non-array / empty input: treated as safe-to-refuse at the exec layer
assert.ok(isBlocked(null));
assert.ok(isBlocked('reset --hard'));   // not an array - guard must not bypass

// empty args are not a destructive op but the exec layer rejects them
run({ args: [], cwd: '/tmp' }).then((r) => {
  assert.strictEqual(r.exit_code, 2, 'empty args must be rejected at exec');
  console.log('companion gitops guard: OK');
});

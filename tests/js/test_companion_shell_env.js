// node tests/js/test_companion_shell_env.js - what a command the AGENT wrote can see.
//
// RED for TIER1_SANDBOX_SPIKE.md §6: both spawn sites passed
// `env: { ...process.env, CI: "1" }`, so every secret in the user's session
// environment was readable by code the model wrote - and run_python is
// arbitrary Python. This pins the policy that closes it without breaking the
// toolchains that legitimately need a real environment (npm/pip/git need HOME,
// PATH, proxies, cert bundles).
const assert = require('assert');
const path = require('path');

const shellops = require(path.join(__dirname, '..', '..', 'companion', 'shellops.js'));

// A representative parent environment: the boring vars a build needs, plus the
// secrets nobody wants a model-authored command to be able to print.
const PARENT = {
  PATH: 'C:\\Windows;C:\\Program Files\\nodejs',
  HOME: 'C:\\Users\\dev',
  SystemRoot: 'C:\\Windows',
  HTTPS_PROXY: 'http://proxy:8080',
  SSL_CERT_FILE: 'C:\\certs\\corp.pem',
  GITHUB_TOKEN: 'ghp_realtoken',
  AWS_SECRET_ACCESS_KEY: 'wJalr',
  OPENAI_API_KEY: 'sk-realkey',
  DB_PASSWORD: 'hunter2',
  MY_SESSION_ID: 'not-a-secret',
};

const clean = (env) => shellops.buildChildEnv(env, { parentEnv: env, mode: 'deny' });

// --- deny mode (default): secrets go, build-critical vars stay
{
  const out = clean(PARENT);
  for (const k of ['PATH', 'HOME', 'SystemRoot', 'HTTPS_PROXY', 'SSL_CERT_FILE']) {
    assert.strictEqual(out[k], PARENT[k], `${k} must survive - tooling breaks without it`);
  }
  for (const k of ['GITHUB_TOKEN', 'AWS_SECRET_ACCESS_KEY', 'OPENAI_API_KEY', 'DB_PASSWORD']) {
    assert.ok(!(k in out), `${k} must not reach a command the model wrote`);
  }
  assert.ok(!("MY_SESSION_ID" in out), 'deny mode is a NAME deny-list: unlisted vars do not pass');
  assert.strictEqual(out.CI, '1', 'CI=1 is still injected');
}

// --- escape hatch: the legitimate `git push` case, named explicitly
{
  const out = shellops.buildChildEnv(PARENT, {
    parentEnv: PARENT, mode: 'deny', allow: ['GITHUB_TOKEN'],
  });
  assert.strictEqual(out.GITHUB_TOKEN, 'ghp_realtoken', 'an explicitly allowed var passes');
  assert.ok(!("OPENAI_API_KEY" in out), 'the escape hatch is per-variable, not a blanket');
}

// --- allow mode: explicit allow-list only, nothing inherited by accident
{
  const out = shellops.buildChildEnv(PARENT, {
    parentEnv: PARENT, mode: 'allow', allow: ['PATH', 'HOME'],
  });
  assert.deepStrictEqual(Object.keys(out).sort(), ['CI', 'HOME', 'PATH']);
}

// --- unknown mode must fail closed, not silently pass secrets through
{
  const out = shellops.buildChildEnv(PARENT, { parentEnv: PARENT, mode: 'nonsense' });
  assert.ok(!("GITHUB_TOKEN" in out), 'an unknown mode must not behave like pass-through');
}

// --- Windows caseness: env var names are case-insensitive there
{
  const out = clean({ Path: 'C:\\Windows', Github_Token: 'ghp_x' });
  assert.ok(!("Github_Token" in out), 'name matching must be case-insensitive');
  assert.strictEqual(out.Path, 'C:\\Windows');
}

// --- empty/undefined input must not throw (the shell still has to run)
{
  assert.strictEqual(shellops.buildChildEnv(undefined, { parentEnv: {} }).CI, '1');
  assert.strictEqual(shellops.buildChildEnv({}, { mode: 'deny', parentEnv: {} }).CI, '1');
}

console.log('companion shell env tests passed');
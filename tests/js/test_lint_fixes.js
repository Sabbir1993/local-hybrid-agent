// tests/js/test_lint_fixes.js - regressions the JS lint gate caught (2026-10-06).
//
// ESLint no-undef over static/js (source-derived globals) found two real
// defects: input-guard.js called a never-defined `showToast`, and gpu-status.js
// read a never-assigned `APP_MODELS`. Both were guarded by `typeof`, so each
// was a silent dead path - a "saved" toast that never appeared and an executor
// ctx ceiling that always fell through. Pin the fixes here so a rename cannot
// silently re-divert them.
// Run: node tests/js/test_lint_fixes.js
const fs = require('fs');
const path = require('path');
const assert = require('assert');

const read = (name) => fs.readFileSync(
  path.join(__dirname, '../../static/js', name), 'utf8');

const inputGuard = read('input-guard.js');
assert(!/showToast\s*\(/.test(inputGuard),
  "input-guard.js must use the real toast(), not a never-defined showToast");
assert(/toast\(`\$\{isOutput \? 'Output' : 'Input'\} sanitizer rules saved`\)/.test(inputGuard),
  "saved-rules toast must still fire through toast()");

const gpuStatus = read('gpu-status.js');
assert(!/typeof APP_MODELS/.test(gpuStatus) && !/APP_MODELS\.(executor|models)/.test(gpuStatus),
  "gpu-status.js must not read the never-assigned APP_MODELS");

console.log('lint fixes: OK');

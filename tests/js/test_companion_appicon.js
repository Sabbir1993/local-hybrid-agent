// node tests/js/test_companion_appicon.js - the tray icon must never be blank, installed or from source
const assert = require('assert');
const path = require('path');
const { iconCandidates, loadAppIcon, FALLBACK_PNG_BASE64 } = require('../../companion/appicon');

// an installed app looks in resources/ first (build/ is not inside the asar)
const win = iconCandidates({ platform: 'win32', resourcesPath: 'C:\App\resources', dirname: 'C:\App\resources\app.asar' });
assert.strictEqual(win[0], path.join('C:\App\resources', 'icon.ico'));
assert.ok(win.some(p => p.endsWith(path.join('build', 'icon.png'))), 'source checkout still works');
assert.deepStrictEqual(iconCandidates({ platform: 'linux', resourcesPath: '/r', dirname: '/d' }).slice(0, 1), [path.join('/r', 'icon.png')]);

const img = (empty) => ({ isEmpty: () => empty, tag: empty ? 'empty' : 'real' });
const fsHas = (...have) => ({ existsSync: p => have.includes(p) });

// found in resources/
let r = loadAppIcon({ nativeImage: { createFromPath: () => img(false), createFromDataURL: () => img(false) },
  fs: fsHas(path.join('/res', 'icon.png')), platform: 'linux', resourcesPath: '/res', dirname: '/app', warn() {} });
assert.strictEqual(r.path, path.join('/res', 'icon.png'));

// a file that exists but decodes to nothing is skipped, the next candidate is used
const created = [];
r = loadAppIcon({ nativeImage: { createFromPath: p => { created.push(p); return img(created.length === 1); }, createFromDataURL: () => img(false) },
  fs: fsHas(path.join('/res', 'icon.png'), path.join('/app', 'build', 'icon.png')), platform: 'linux', resourcesPath: '/res', dirname: '/app', warn() {} });
assert.strictEqual(r.path, path.join('/app', 'build', 'icon.png'));

// nothing loads: the built-in image is used and a warning is logged, never an empty image
let warned = 0, dataUrl = '';
r = loadAppIcon({ nativeImage: { createFromPath: () => img(true), createFromDataURL: u => { dataUrl = u; return img(false); } },
  fs: fsHas(), platform: 'win32', resourcesPath: '/res', dirname: '/app', warn: () => { warned++; } });
assert.strictEqual(r.path, null);
assert.strictEqual(warned, 1);
assert.ok(dataUrl.startsWith('data:image/png;base64,') && dataUrl.endsWith(FALLBACK_PNG_BASE64));
assert.ok(Buffer.from(FALLBACK_PNG_BASE64, 'base64').slice(1, 4).toString() === 'PNG', 'the fallback is a real PNG');
console.log('companion app icon tests: OK');

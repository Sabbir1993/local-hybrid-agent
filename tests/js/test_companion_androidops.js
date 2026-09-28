// node tests/js/test_companion_androidops.js - companion/androidops.js with adb faked:
// argv shape (no shell), device picking, UI dump parsing + tap-by-ref, text escaping,
// the card-number refusal and install confirmation.
const assert = require('assert');
const path = require('path');
const cp = require('child_process');

const companion = path.join(__dirname, '..', '..', 'companion');
const calls = [];
const confirmed = [];
const policyPath = path.join(companion, 'policy.js');
require.cache[policyPath] = { id: policyPath, filename: policyPath, loaded: true, exports: {
  ensurePath: async () => {},
  confirmAction: async (title, msg, detail) => { confirmed.push(title); },
} };

const UI = `<?xml version='1.0' encoding='UTF-8' standalone='yes' ?><hierarchy rotation="0">
<node index="0" text="" resource-id="" class="android.widget.FrameLayout" package="com.example.app" clickable="false" bounds="[0,0][1080,2400]">
<node index="1" text="Email" resource-id="com.example.app:id/email" class="android.widget.EditText" package="com.example.app" clickable="true" focused="true" bounds="[40,300][1040,420]" />
<node index="2" text="Sign in" resource-id="com.example.app:id/login" class="android.widget.Button" package="com.example.app" clickable="true" bounds="[40,500][1040,620]" />
<node index="3" text="" content-desc="Tom &amp; Jerry" class="android.widget.ImageView" package="com.example.app" clickable="false" bounds="[0,0][0,0]" />
</node></hierarchy>`;

cp.execFile = (file, args, opts, cb) => {
  calls.push(args);
  const j = args.join(' ');
  let out = '';
  if (j === 'devices -l') out = 'List of devices attached\nemulator-5554          device product:sdk model:Pixel_7 device:emu64\n';
  else if (j.endsWith('exec-out cat /sdcard/a770_ui.xml')) out = UI;
  else if (j.includes('install')) out = 'Performing Streamed Install\nSuccess';
  setImmediate(() => cb(null, opts && opts.encoding === 'buffer' ? Buffer.from(out) : out, ''));
};
const a = require(path.join(companion, 'androidops.js'));

(async () => {
  // one device -> picked automatically
  const ui = await a.uiDump({});
  assert.strictEqual(ui.serial, 'emulator-5554');
  assert.strictEqual(ui.foreground_package, 'com.example.app');
  assert.match(ui.tree, /\[n1\] EditText "Email" id=email @\(540,360\) clickable editable focused/);
  assert.match(ui.tree, /\[n2\] Button "Sign in" id=login @\(540,560\)/);
  assert.ok(!/Tom/.test(ui.tree), 'zero-size nodes dropped');

  calls.length = 0;
  await a.tap({ ref: 'n2' });
  assert.deepStrictEqual(calls.pop(), ['-s', 'emulator-5554', 'shell', 'input', 'tap', '540', '560']);

  await a.text({ text: "it's a b&c" });
  assert.deepStrictEqual(calls.pop(), ['-s', 'emulator-5554', 'shell', 'input', 'text', "it\\'s%sa%sb\\&c"]);
  await assert.rejects(a.text({ text: '4111111111111111' }), /card number/);

  await assert.rejects(a.launch({ package: 'com.x; reboot' }), /invalid package/);
  await assert.rejects(a.key({ key: 'HOME; rm' }), /key must be/);
  await assert.rejects(a.tap({ serial: 'x; y' }), /invalid device serial/);

  confirmed.length = 0;
  await assert.rejects(a.install({ apk: 'app.txt' }), /\.apk/);
  const fs = require('fs'); const os = require('os');
  const apk = path.join(os.tmpdir(), 'a770-test.apk'); fs.writeFileSync(apk, 'x');
  const r = await a.install({ apk });
  assert.match(r.output, /Success/);
  assert.deepStrictEqual(confirmed, ['install app?']);
  fs.rmSync(apk);
  console.log('ok - companion androidops');
})().catch(e => { console.error(e); process.exit(1); });

// tests/js/test_task_notification.js - notifications when task finishes & user away or companion minimized
const assert = require('assert');
const fs = require('fs');
const path = require('path');
const vm = require('vm');

const utilsSrc = fs.readFileSync(path.join(__dirname, '..', '..', 'static', 'js', 'utils.js'), 'utf8');

function setupTestContext(opts = {}) {
  const notificationsMade = [];
  const nativeNotifsMade = [];
  const framesFlashed = [];
  let chimePlayed = false;

  const mockDoc = {
    hidden: opts.docHidden ?? false,
    hasFocus: () => (opts.docFocused ?? true),
    title: opts.docTitle || 'Local Agent',
    addEventListener: () => {},
    removeEventListener: () => {},
  };

  class MockNotification {
    static permission = opts.notifPermission || 'granted';
    static requestCalls = 0;
    static requestPermission() {
      MockNotification.requestCalls++;
      return Promise.resolve(MockNotification.permission);
    }
    constructor(title, options) {
      this.title = title;
      this.options = options;
      notificationsMade.push({ title, options });
    }
  }

  class MockAudioContext {
    constructor() {
      chimePlayed = true;
      this.currentTime = 0;
      this.destination = {};
    }
    createOscillator() {
      return {
        type: 'sine',
        frequency: { setValueAtTime() {} },
        connect() {},
        start() {},
        stop() {},
      };
    }
    createGain() {
      return {
        gain: { setValueAtTime() {}, exponentialRampToValueAtTime() {} },
        connect() {},
      };
    }
    close() { return Promise.resolve(); }
  }

  const mockElectron = opts.electronAPI ? {
    isMinimized: async () => opts.electronMinimized ?? false,
    flashFrame: (flag) => { framesFlashed.push(flag); },
    notify: (data) => { nativeNotifsMade.push(data); },
  } : undefined;

  const ctx = {
    document: mockDoc,
    window: {},
    console,
    Notification: MockNotification,
    AudioContext: MockAudioContext,
    setTimeout: (fn) => fn(),
    clearTimeout: () => {},
    setInterval: () => 123,
    clearInterval: () => {},
    hlLangFor: () => '',
    hlCode: (c) => c,
  };
  if (mockElectron) {
    ctx.window.electronAPI = mockElectron;
  }

  vm.createContext(ctx);
  vm.runInContext(utilsSrc, ctx);

  return {
    ctx,
    mockDoc,
    MockNotification,
    notificationsMade,
    nativeNotifsMade,
    framesFlashed,
    getChimePlayed: () => chimePlayed,
  };
}

(async () => {
  // Test 1: User active on tab and companion not minimized -> no notification
  {
    const env = setupTestContext({ docHidden: false, docFocused: true });
    const away = await env.ctx.isUserAwayOrCompanionMinimized();
    assert.strictEqual(away, false, 'User on tab must report not away');

    const notified = await env.ctx.notifyTaskFinished({ title: 'T1', body: 'B1' });
    assert.strictEqual(notified, false, 'Must not notify when user is active');
    assert.strictEqual(env.notificationsMade.length, 0);
  }

  // Test 2: User not on tab (document hidden) -> triggers notification
  {
    const env = setupTestContext({ docHidden: true, docFocused: false });
    const away = await env.ctx.isUserAwayOrCompanionMinimized();
    assert.strictEqual(away, true, 'document.hidden must report away');

    const notified = await env.ctx.notifyTaskFinished({ title: 'Task Done', body: 'All steps finished' });
    assert.strictEqual(notified, true, 'Must notify when document is hidden');
    assert.strictEqual(env.notificationsMade.length, 1);
    assert.strictEqual(env.notificationsMade[0].title, 'Task Done');
    assert.strictEqual(env.notificationsMade[0].options.body, 'All steps finished');
    assert.strictEqual(env.getChimePlayed(), true, 'Chime should play');
    assert.ok(env.mockDoc.title.includes('Task Done'), 'Tab title should update with task title');
  }

  // Test 3: User tab blurred / unfocused (!document.hasFocus()) -> triggers notification
  {
    const env = setupTestContext({ docHidden: false, docFocused: false });
    const away = await env.ctx.isUserAwayOrCompanionMinimized();
    assert.strictEqual(away, true, '!document.hasFocus() must report away');

    const notified = await env.ctx.notifyTaskFinished({ title: 'Task 2' });
    assert.strictEqual(notified, true);
    assert.strictEqual(env.notificationsMade.length, 1);
  }

  // Test 4: Companion app minimized -> triggers native notification and flashFrame
  {
    const env = setupTestContext({
      electronAPI: true,
      electronMinimized: true,
      docHidden: false,
      docFocused: true,
    });
    const away = await env.ctx.isUserAwayOrCompanionMinimized();
    assert.strictEqual(away, true, 'Companion minimized must report away');

    const notified = await env.ctx.notifyTaskFinished({ title: 'Companion Task', body: 'Finished in companion' });
    assert.strictEqual(notified, true);
    assert.strictEqual(env.nativeNotifsMade.length, 1);
    assert.strictEqual(env.nativeNotifsMade[0].title, 'Companion Task');
    assert.strictEqual(env.framesFlashed.length, 1);
    assert.strictEqual(env.framesFlashed[0], true);
  }

  // Test 5: Permission request
  {
    const env = setupTestContext({ notifPermission: 'default' });
    env.ctx.requestNotificationPermission();
    assert.strictEqual(env.MockNotification.requestCalls, 1, 'Should call requestPermission when default');
  }

  console.log('task notification tests: OK');
})().catch(err => {
  console.error(err);
  process.exit(1);
});

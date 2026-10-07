// Runs popup.js against a fake DOM and `chrome`. Covers the tab-ownership rule: only the tab that
// started the session may use the toggle (Stop); from any other tab it is disabled and sends nothing.
const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const ACTIVE = { active: true, repo: 'o/r', tabId: 7, error: null };
const INACTIVE = { active: false, repo: null, tabId: null, error: null };

async function boot({ session = INACTIVE, tabId = 7 } = {}) {
  const sent = [];
  const handlers = {};
  const el = (extra = {}) => ({
    textContent: '', value: '', disabled: false, hidden: false, readOnly: false,
    addEventListener(type, fn) { handlers[type] = fn; }, ...extra,
  });
  const toggle = el();
  const elements = { repo: el(), toggle, 'status-text': el(), error: el() };
  const storageListeners = [];
  const chrome = {
    runtime: {
      async sendMessage(message) {
        sent.push(message);
        if (message.type === 'badcat:status') return { ok: true, session };
        return { ok: true };
      },
    },
    storage: {
      local: { async get() { return {}; }, async set() {} },
      onChanged: { addListener: (fn) => storageListeners.push(fn) },
    },
    tabs: { async query() { return tabId === null ? [] : [{ id: tabId }]; } },
  };
  const context = {
    document: {
      getElementById: (id) => elements[id],
      body: { classList: { toggle() {} } },
    },
    chrome,
    self: { BadCatShared: require('../shared.js') },
  };
  vm.createContext(context);
  vm.runInContext(fs.readFileSync(path.join(__dirname, '..', 'popup.js'), 'utf8'), context);
  await new Promise((r) => setImmediate(r)); // let init() finish
  const changeSession = (next) => storageListeners.forEach((fn) => fn({ 'badcat.session': { newValue: next } }, 'session'));
  return { toggle, sent, click: () => handlers.click(), changeSession, elements };
}

test('on the tab that started the session the toggle works and stops it with that tab id', async () => {
  const t = await boot({ session: ACTIVE, tabId: 7 });
  assert.equal(t.toggle.disabled, false);
  await t.click();
  assert.equal(JSON.stringify(t.sent.filter((m) => m.type !== 'badcat:status')), JSON.stringify([{ type: 'badcat:stop', tabId: 7 }]));
  assert.equal(t.toggle.disabled, false);
});

test('from another tab the toggle is disabled and neither Start nor Stop is sent', async () => {
  const t = await boot({ session: ACTIVE, tabId: 8 });
  assert.equal(t.toggle.disabled, true);
  assert.equal(t.toggle.textContent, 'Nap time, Meow!');
  await t.click();
  assert.equal(t.sent.filter((m) => m.type !== 'badcat:status').length, 0);
});

test('a session change re-evaluates the control', async () => {
  const t = await boot({ session: INACTIVE, tabId: 8 });
  assert.equal(t.toggle.disabled, false); // nothing is running: Start is available here
  t.changeSession(ACTIVE);
  assert.equal(t.toggle.disabled, true);
  t.changeSession(INACTIVE);
  assert.equal(t.toggle.disabled, false);
});

test('without a known current tab an active session cannot be controlled', async () => {
  const t = await boot({ session: ACTIVE, tabId: null });
  assert.equal(t.toggle.disabled, true);
});

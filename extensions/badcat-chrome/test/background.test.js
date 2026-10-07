// Drives background.js against a fake `chrome`, as the service worker would run it.
const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const HEAD1 = 'a'.repeat(40);
const HEAD2 = 'b'.repeat(40);
const GOOD_URL = 'https://chatgpt.com/c/abc-123';

function boot({ tabUrl = GOOD_URL, handoff = async () => ({ ok: true }), seed = {} } = {}) {
  const store = { ...seed };
  const tabs = { 7: { id: 7, url: tabUrl } };
  const sent = [];
  const listeners = {};
  const natives = [];
  const hook = (name) => ({ addListener: (fn) => { listeners[name] = fn; } });
  const chrome = {
    runtime: {
      id: 'me',
      lastError: null,
      onMessage: hook('runtime.onMessage'),
      connectNative(name) {
        const port = {
          name, posted: [], disconnected: false,
          postMessage(m) { this.posted.push(m); },
          disconnect() { this.disconnected = true; },
          onMessage: { addListener(fn) { port.emit = fn; } },
          onDisconnect: { addListener(fn) { port.drop = fn; } },
        };
        natives.push(port);
        return port;
      },
    },
    storage: {
      session: {
        async get(key) { return key in store ? { [key]: store[key] } : {}; },
        async set(obj) { Object.assign(store, JSON.parse(JSON.stringify(obj))); },
      },
    },
    tabs: {
      onRemoved: hook('tabs.onRemoved'),
      onUpdated: hook('tabs.onUpdated'),
      async get(id) { if (!tabs[id]) throw new Error('No tab'); return tabs[id]; },
      async sendMessage(id, message) {
        if (message.type === 'badcat:ping') return { ok: true };
        sent.push({ id, message });
        return handoff(message);
      },
    },
  };
  const shared = require('../shared.js');
  const context = {
    chrome, console: { warn() {}, error() {}, log() {} }, setTimeout, URL,
    importScripts() {}, self: { BadCatShared: shared },
  };
  context.self.chrome = chrome;
  vm.createContext(context);
  vm.runInContext(fs.readFileSync(path.join(__dirname, '..', 'background.js'), 'utf8'), context);

  const popup = (message) => new Promise((resolve) => {
    const keep = listeners['runtime.onMessage'](message, { id: 'me' }, resolve);
    assert.equal(keep, true);
  });
  const session = () => store['badcat.session'];
  const settle = () => new Promise((r) => setImmediate(r));
  return { popup, session, store, tabs, sent, listeners, natives, settle };
}

const plus1 = (pr, head, extra = {}) => ({ repo: 'o/r', pr, head, status: '+1', ...extra });

async function started(opts) {
  const t = boot(opts);
  const res = await t.popup({ type: 'badcat:start', repo: 'o/r', tabId: 7 });
  assert.equal(res.ok, true);
  return t;
}

test('starts OFF; ON binds the current tab and tells the host which repo to watch', async () => {
  const t = boot();
  assert.equal((await t.popup({ type: 'badcat:status' })).session.active, false);
  await t.popup({ type: 'badcat:start', repo: 'o/r', tabId: 7 });
  assert.equal(JSON.stringify(t.session()), JSON.stringify({ active: true, repo: 'o/r', tabId: 7, error: null }));
  assert.equal(t.natives.length, 1);
  assert.equal(t.natives[0].name, 'com.genonfire.badcat_host');
  assert.equal(JSON.stringify(t.natives[0].posted), JSON.stringify([{ type: 'start', repo: 'o/r' }]));
});

test('start refuses a non-conversation tab, a missing tab and a bad repo without connecting', async () => {
  let t = boot({ tabUrl: 'https://chatgpt.com/' });
  assert.equal((await t.popup({ type: 'badcat:start', repo: 'o/r', tabId: 7 })).ok, false);
  t = boot();
  assert.equal((await t.popup({ type: 'badcat:start', repo: 'o/r', tabId: 99 })).ok, false);
  assert.equal((await t.popup({ type: 'badcat:start', repo: 'bad repo', tabId: 7 })).ok, false);
  assert.equal(t.natives.length, 0);
});

test('only extension pages may control the session', () => {
  const t = boot();
  const l = t.listeners['runtime.onMessage'];
  assert.equal(l({ type: 'badcat:stop' }, { id: 'other' }, () => {}), false);
  assert.equal(l({ type: 'badcat:stop' }, { id: 'me', tab: { id: 1 } }, () => {}), false);
});

test('a valid +1 sends exactly one handoff with only the PR number', async () => {
  const t = await started();
  t.natives[0].emit(plus1(123, HEAD1));
  await t.settle();
  assert.equal(JSON.stringify(t.sent), JSON.stringify([{ id: 7, message: { type: 'badcat:handoff', pr: 123 } }]));
});

test('a duplicate event for the same repo/PR/HEAD is not sent twice; a new HEAD is', async () => {
  const t = await started();
  t.natives[0].emit(plus1(123, HEAD1));
  t.natives[0].emit(plus1(123, HEAD1));
  await t.settle();
  assert.equal(t.sent.length, 1);
  t.natives[0].emit(plus1(123, HEAD2));
  await t.settle();
  assert.equal(t.sent.length, 2);
});

test('invalid, +2, error-less junk and other-repo payloads do nothing', async () => {
  const t = await started();
  for (const m of [plus1(1, HEAD1, { status: '+2' }), plus1(1, 'short'), plus1('1', HEAD1), plus1(1, HEAD1, { repo: 'x/y' }),
    null, 'text', [], {}, plus1(0, HEAD1)]) {
    t.natives[0].emit(m);
  }
  await t.settle();
  assert.equal(t.sent.length, 0);
  assert.equal(t.session().active, true);
});

test('a failed submit is not marked delivered, so the same event can still be handed off', async () => {
  let calls = 0;
  const t = await started({ handoff: async () => (++calls === 1 ? { ok: false, error: 'composer not found' } : { ok: true }) });
  const realSetTimeout = global.setTimeout;
  global.setTimeout = (fn) => realSetTimeout(fn, 0); // skip the retry backoff
  try {
    t.natives[0].emit(plus1(5, HEAD1));
    await new Promise((r) => realSetTimeout(r, 50));
  } finally {
    global.setTimeout = realSetTimeout;
  }
  assert.ok(calls >= 1);
});

test('OFF disconnects the host and stops sending', async () => {
  const t = await started();
  const port = t.natives[0];
  await t.popup({ type: 'badcat:stop', tabId: 7 });
  assert.equal(port.disconnected, true);
  assert.equal(t.session().active, false);
  port.emit(plus1(9, HEAD1));
  await t.settle();
  assert.equal(t.sent.length, 0);
});

test('only the tab that started the session can stop or start it', async () => {
  const t = await started();
  const port = t.natives[0];
  for (const tabId of [8, undefined, null, '7']) {
    assert.equal((await t.popup({ type: 'badcat:stop', tabId })).ok, false, String(tabId));
  }
  assert.equal((await t.popup({ type: 'badcat:start', repo: 'x/y', tabId: 8 })).ok, false);
  assert.equal(port.disconnected, false);
  assert.equal(JSON.stringify(t.session()), JSON.stringify({ active: true, repo: 'o/r', tabId: 7, error: null }));
  assert.equal(t.natives.length, 1);
  assert.equal((await t.popup({ type: 'badcat:stop', tabId: 7 })).ok, true);
  assert.equal(t.session().active, false);
});

test('closing the target tab stops the session and never picks another tab', async () => {
  const t = await started();
  await t.listeners['tabs.onRemoved'](7);
  assert.equal(t.session().active, false);
  assert.equal(t.natives[0].disconnected, true);
  t.natives[0].emit(plus1(9, HEAD1));
  await t.settle();
  assert.equal(t.sent.length, 0);
});

test('navigating the target tab away from a conversation stops the session', async () => {
  const t = await started();
  await t.listeners['tabs.onUpdated'](7, { url: 'https://example.com/' });
  assert.equal(t.session().active, false);
});

test('a tab that is no longer a conversation at delivery time fails safely', async () => {
  const t = await started();
  t.tabs[7].url = 'https://chatgpt.com/';
  t.natives[0].emit(plus1(9, HEAD1));
  await t.settle();
  assert.equal(t.sent.length, 0);
  assert.equal(t.session().active, false);
});

test('a host error or disconnect marks the session inactive with a reason', async () => {
  let t = await started();
  t.natives[0].emit({ error: 'cannot determine the authenticated gh user' });
  await t.settle();
  assert.equal(t.session().active, false);
  assert.match(t.session().error, /gh user/);

  t = await started();
  t.natives[0].drop();
  await t.settle();
  assert.equal(t.session().active, false);
});

test('a stale active session without a live connection is reset on wake-up', async () => {
  const t = boot({ seed: { 'badcat.session': { active: true, repo: 'o/r', tabId: 7, error: null } } });
  const res = await t.popup({ type: 'badcat:status' });
  assert.equal(res.session.active, false);
});

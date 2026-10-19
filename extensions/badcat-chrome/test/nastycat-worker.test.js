// nastycat.js against a fake chrome: arming, polling, delivery, idempotency, per-tab isolation.
const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const crypto = require('node:crypto').webcrypto;

const PR = (n, repo = 'o/r') => `https://github.com/${repo}/pull/${n}`;

function boot({ hold = null, tabs: tabUrls = { 7: 'https://claude.ai/code/session_A' } } = {}) {
  const store = { local: {}, session: {} };
  const listeners = {};
  const hook = (name) => ({ addListener: (fn) => { listeners[name] = fn; } });
  const tabs = {};
  for (const [id, url] of Object.entries(tabUrls)) tabs[id] = { id: Number(id), url, links: [], conversationId: null, editor: 'ok', delivered: [] };
  const hostPorts = [];
  const hostState = { merged: {}, down: false, requests: [] }; // key -> {merged, merged_at}
  const alarms = { active: false };
  const area = (name) => ({
    async get(key) { return key in store[name] ? { [key]: JSON.parse(JSON.stringify(store[name][key])) } : {}; },
    async set(obj) {
      if (name === 'local' && hold && hold.when(obj)) await hold.promise;
      Object.assign(store[name], JSON.parse(JSON.stringify(obj)));
    },
  });
  const chrome = {
    runtime: {
      id: 'me',
      onMessage: hook('onMessage'),
      connectNative(name) {
        assert.equal(name, 'com.genonfire.nastycat_host');
        const port = {
          onMessage: { addListener(fn) { port.emit = fn; } },
          onDisconnect: { addListener(fn) { port.drop = fn; } },
          postMessage(m) {
            hostState.requests.push(m);
            if (hostState.down) return;
            const prs = m.prs.filter((p) => hostState.merged[p.repo.toLowerCase() + '#' + p.pr]).map((p) => {
              const a = hostState.merged[p.repo.toLowerCase() + '#' + p.pr];
              return { repo: p.repo, pr: p.pr, state: 'open', merged: a.merged, merged_at: a.merged_at };
            });
            setImmediate(() => port.emit({ type: 'result', id: m.id, prs, failed: [] }));
          },
        };
        hostPorts.push(port);
        return port;
      },
    },
    storage: { session: area('session'), local: area('local') },
    alarms: { onAlarm: hook('onAlarm'), create() { alarms.active = true; }, async clear() { alarms.active = false; } },
    scripting: { async executeScript() {} },
    tabs: {
      onRemoved: hook('onRemoved'),
      onUpdated: hook('onUpdated'),
      async get(id) { if (!tabs[id]) throw new Error('No tab'); return tabs[id]; },
      async sendMessage(id, m) {
        const tab = tabs[id];
        if (!tab) throw new Error('No tab with id');
        if (m.type === 'nastycat:ping') return { ok: true };
        if (m.type === 'nastycat:cancel') { tab.cancelled = (tab.cancelled || []).concat(m.deliveryId); return { ok: true }; }
        if (m.type === 'nastycat:scan') return { ok: true, conversationId: tab.conversationId || require('../nastycat-shared.js').parseClaudeSession(tab.url), links: tab.links };
        if (m.type === 'nastycat:deliver') {
          tab.delivered.push(m);
          return tab.reply ? tab.reply(m) : { ok: true, phase: 'confirmed' };
        }
        throw new Error('unexpected');
      },
    },
  };
  const shared = require('../nastycat-shared.js');
  const context = { chrome, crypto, console, setTimeout, clearTimeout, URL, Date, self: { NastyCatShared: shared } };
  context.self.chrome = chrome;
  vm.createContext(context);
  vm.runInContext(fs.readFileSync(path.join(__dirname, '..', 'nastycat.js'), 'utf8'), context);
  const popup = (message) => new Promise((resolve) => {
    assert.equal(listeners.onMessage(message, { id: 'me' }, resolve), true);
  });
  const sessions = () => store.session['nastycat.sessions.v1'] || {};
  const poll = (id) => context.self.NastyCat.pollTab(id);
  return { popup, tabs, store, sessions, poll, listeners, hostState, alarms, nc: context.self.NastyCat, hostPorts };
}

const merged = (at) => ({ merged: true, merged_at: at || new Date(Date.now() + 5000).toISOString() });
const open = { merged: false, merged_at: null };

test('arming with no PRs sends the frozen prompt once and does not repeat on later polls', async () => {
  const t = boot();
  assert.equal((await t.popup({ type: 'nastycat:start', prompt: '#860 A 진행해', tabId: 7 })).ok, true);
  await new Promise((r) => setTimeout(r, 30));
  assert.equal(t.tabs[7].delivered.length, 1);
  assert.equal(t.tabs[7].delivered[0].prompt, '#860 A 진행해');
  assert.equal(t.alarms.active, true);
  await t.poll(7);
  await t.poll(7);
  assert.equal(t.tabs[7].delivered.length, 1);
  assert.equal(t.sessions()[7].startup, 'done');
});

test('an open PR at arming waits; its merge sends exactly once; old links never repeat', async () => {
  const t = boot();
  t.tabs[7].links = [PR(954), PR(957)];
  t.hostState.merged = { 'o/r#954': merged('2025-01-01T00:00:00Z'), 'o/r#957': open };
  await t.popup({ type: 'nastycat:start', prompt: 'go', tabId: 7 });
  await new Promise((r) => setTimeout(r, 30));
  assert.equal(t.tabs[7].delivered.length, 0);
  await t.poll(7);
  assert.equal(t.tabs[7].delivered.length, 0);
  t.hostState.merged['o/r#957'] = merged();
  await t.poll(7);
  assert.equal(t.tabs[7].delivered.length, 1);
  await t.poll(7);
  await t.poll(7);
  assert.equal(t.tabs[7].delivered.length, 1);
});

test('the baseline asks GitHub for every link; later polls skip settled PRs', async () => {
  const t = boot();
  t.tabs[7].links = [PR(1), PR(2)];
  t.hostState.merged = { 'o/r#1': merged('2025-01-01T00:00:00Z'), 'o/r#2': open };
  await t.popup({ type: 'nastycat:start', prompt: 'go', tabId: 7 });
  await new Promise((r) => setTimeout(r, 30));
  await t.poll(7);
  assert.deepEqual(t.hostState.requests.map((r) => r.prs.length), [2, 1]);
});

test('two Claude tabs keep independent prompts, state and lifecycle', async () => {
  const t = boot({ tabs: { 7: 'https://claude.ai/code/session_A', 8: 'https://claude.ai/code/session_B' } });
  await t.popup({ type: 'nastycat:start', prompt: '#860 A 진행해', tabId: 7 });
  await t.popup({ type: 'nastycat:start', prompt: '#860 B 진행해', tabId: 8 });
  await new Promise((r) => setTimeout(r, 50));
  assert.equal(t.tabs[7].delivered[0].prompt, '#860 A 진행해');
  assert.equal(t.tabs[8].delivered[0].prompt, '#860 B 진행해');
  assert.equal(t.tabs[7].delivered[0].conversationId, 'session_A');
  await t.listeners.onRemoved(7);
  await new Promise((r) => setTimeout(r, 10));
  assert.equal(t.sessions()[7], undefined);
  assert.equal(t.sessions()[8].active, true);
  assert.equal(t.alarms.active, true);
  await t.popup({ type: 'nastycat:stop', tabId: 8 });
  assert.equal(t.alarms.active, false);
});

test('rejects unsupported pages, empty prompts and duplicate arming', async () => {
  const t = boot({ tabs: { 7: 'https://claude.ai/chat/xyz', 8: 'https://chatgpt.com/c/abc', 9: 'https://claude.ai/code/session_C' } });
  assert.equal((await t.popup({ type: 'nastycat:start', prompt: 'go', tabId: 7 })).ok, false);
  assert.equal((await t.popup({ type: 'nastycat:start', prompt: 'go', tabId: 8 })).ok, false);
  assert.equal((await t.popup({ type: 'nastycat:start', prompt: '  ', tabId: 9 })).ok, false);
  assert.equal((await t.popup({ type: 'nastycat:start', prompt: 'go', tabId: 9 })).ok, true);
  assert.equal((await t.popup({ type: 'nastycat:start', prompt: 'go', tabId: 9 })).ok, false);
  assert.deepEqual(t.sessions()[7], undefined);
});

test('navigating to another conversation stops the session without sending there', async () => {
  const t = boot();
  t.tabs[7].links = [PR(1)];
  t.hostState.merged = { 'o/r#1': open };
  await t.popup({ type: 'nastycat:start', prompt: 'go', tabId: 7 });
  await new Promise((r) => setTimeout(r, 30));
  await t.listeners.onUpdated(7, { url: 'https://claude.ai/code/session_OTHER' });
  assert.equal(t.sessions()[7].active, false);
  t.hostState.merged['o/r#1'] = merged();
  await t.poll(7);
  assert.equal(t.tabs[7].delivered.length, 0);
});

test('a scan that reports a different conversation fails closed', async () => {
  const t = boot();
  t.tabs[7].links = [PR(1)];
  t.hostState.merged = { 'o/r#1': open };
  await t.popup({ type: 'nastycat:start', prompt: 'go', tabId: 7 });
  await new Promise((r) => setTimeout(r, 30));
  t.tabs[7].conversationId = 'session_OTHER';
  await t.poll(7);
  assert.equal(t.sessions()[7].active, false);
  assert.match(t.sessions()[7].error, /no longer the armed/);
});

test('a failed submission is retried; an unconfirmed one is never resent', async () => {
  const t = boot();
  let calls = 0;
  t.tabs[7].reply = () => (++calls === 1 ? { ok: false, phase: 'not_sent', reason: 'busy-editor' } : { ok: false, phase: 'uncertain', reason: 'unconfirmed' });
  await t.popup({ type: 'nastycat:start', prompt: 'go', tabId: 7 });
  await new Promise((r) => setTimeout(r, 30));
  assert.equal(t.tabs[7].delivered.length, 1);
  assert.match(t.sessions()[7].note, /busy-editor/);
  await t.poll(7); // retry -> uncertain
  assert.equal(t.tabs[7].delivered.length, 2);
  await t.poll(7);
  await t.poll(7);
  assert.equal(t.tabs[7].delivered.length, 2);
  assert.match(t.sessions()[7].error, /could not be confirmed/);
});

test('a delivery left in "sending" (worker died) becomes uncertain, not a resend', async () => {
  const t = boot();
  await t.popup({ type: 'nastycat:start', prompt: 'go', tabId: 7 });
  await new Promise((r) => setTimeout(r, 30));
  const all = JSON.parse(JSON.stringify(t.sessions()));
  all[7].delivery = { id: 'zz', phase: 'sending', keys: ['__startup__'] };
  all[7].startup = 'pending';
  t.store.session['nastycat.sessions.v1'] = all;
  await t.poll(7);
  assert.equal(t.tabs[7].delivered.length, 1);
  assert.equal(t.sessions()[7].delivery.phase, 'uncertain');
});

test('host failure keeps state and retries; no send happens without a completed baseline', async () => {
  const t = boot();
  t.tabs[7].links = [PR(1)];
  t.hostState.down = true;
  const timeoutless = t.nc;
  // answer with an error frame instead of hanging
  await t.popup({ type: 'nastycat:start', prompt: 'go', tabId: 7 });
  await new Promise((r) => setTimeout(r, 10));
  t.hostPorts[0].emit({ error: 'check failed', id: t.hostState.requests[0].id });
  await new Promise((r) => setTimeout(r, 30));
  assert.equal(t.tabs[7].delivered.length, 0);
  assert.equal(t.sessions()[7].phase, 'baseline');
  assert.match(t.sessions()[7].note, /retry/);
  assert.ok(timeoutless);
});

test('status, global prompt and bounded, prompt-free debug log', async () => {
  const t = boot();
  await t.popup({ type: 'nastycat:start', prompt: 'SECRET-PROMPT', tabId: 7 });
  await new Promise((r) => setTimeout(r, 30));
  const status = await t.popup({ type: 'nastycat:status', tabId: 7 });
  assert.equal(status.session.active, true);
  assert.equal(status.prompt, 'SECRET-PROMPT');
  const log = await t.popup({ type: 'nastycat:debug-log', tabId: 7 });
  assert.ok(log.log.length > 0);
  assert.ok(!JSON.stringify(log.log).includes('SECRET-PROMPT'));
  assert.equal((await t.popup({ type: 'nastycat:debug-log', tabId: 99 })).log.every((e) => e.tab === null), true);
});

test('ignores MissCat messages and content-script senders', async () => {
  const t = boot();
  assert.equal(t.listeners.onMessage({ type: 'badcat:status' }, { id: 'me' }, () => {}), false);
  assert.equal(t.listeners.onMessage({ type: 'nastycat:start', prompt: 'x', tabId: 7 }, { id: 'me', tab: { id: 7 } }, () => {}), false);
  assert.equal(t.listeners.onMessage({ type: 'nastycat:stop' }, { id: 'evil' }, () => {}), false);
});

test('stopping between storing the send intent and dispatching sends nothing and revokes it in the page', async () => {
  let release;
  const hold = { promise: new Promise((r) => { release = r; }), when: (obj) => {
    const list = obj['nastycat.debuglog.v1'] || [];
    return list.length > 0 && list[list.length - 1].event === 'delivery-start';
  } };
  const t = boot({ hold });
  await t.popup({ type: 'nastycat:start', prompt: 'go', tabId: 7 });
  await new Promise((r) => setTimeout(r, 30)); // poll stored the intent; deliver() is parked in its debug write
  assert.equal(t.sessions()[7].delivery.phase, 'sending');
  const stopping = t.popup({ type: 'nastycat:stop', tabId: 7 }); // its disarm log queues behind the parked write
  await new Promise((r) => setTimeout(r, 10));
  release();
  await stopping;
  await new Promise((r) => setTimeout(r, 30));
  assert.equal(t.tabs[7].delivered.length, 0);
  assert.equal(t.tabs[7].cancelled.length, 1);
  assert.equal(t.sessions()[7], undefined);
});

test('re-arming during that gap does not let the old intent through', async () => {
  let release;
  let armed = false;
  const hold = { promise: new Promise((r) => { release = r; }), when: (obj) => {
    const list = obj['nastycat.debuglog.v1'] || [];
    const last = list[list.length - 1];
    if (armed || !last || last.event !== 'delivery-start') return false;
    armed = true;
    return true;
  } };
  const t = boot({ hold });
  await t.popup({ type: 'nastycat:start', prompt: 'one', tabId: 7 });
  await new Promise((r) => setTimeout(r, 30));
  const stopping = t.popup({ type: 'nastycat:stop', tabId: 7 });
  await new Promise((r) => setTimeout(r, 10));
  t.tabs[7].links = [PR(1)];
  t.hostState.merged = { 'o/r#1': open };
  const restarting = t.popup({ type: 'nastycat:start', prompt: 'two', tabId: 7 });
  await new Promise((r) => setTimeout(r, 10));
  release();
  await stopping;
  await restarting;
  await new Promise((r) => setTimeout(r, 50));
  assert.deepEqual(t.tabs[7].delivered.map((m) => m.prompt), []);
});

test('an over-capacity conversation stops visibly instead of watching a truncated view', async () => {
  const t = boot();
  const N = require('../nastycat-shared.js');
  t.tabs[7].links = Array.from({ length: N.MAX_LINKS + 1 }, (_, i) => PR(i + 1));
  await t.popup({ type: 'nastycat:start', prompt: 'go', tabId: 7 });
  await new Promise((r) => setTimeout(r, 100));
  assert.equal(t.sessions()[7].active, false);
  assert.match(t.sessions()[7].error, /too many PR links/);
  assert.equal(t.tabs[7].delivered.length, 0);
});

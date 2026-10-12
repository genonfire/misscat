// Drives background.js against a fake `chrome`, as the service worker would run it.
const test = require('node:test');
const assert = require('node:assert/strict');

const HEAD1 = 'a'.repeat(40);
const HEAD2 = 'b'.repeat(40);
const { bootBackground: boot } = require('./background-fixture.js');

const plus1 = (pr, head, extra = {}) => ({ repo: 'o/r', pr, head, status: '+1', review_id: 100, delivery_id: 'c'.repeat(32), ...extra });

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
  assert.equal(t.sent.length, 1);
  assert.equal(t.sent[0].message.pr, 123);
  assert.equal(t.natives[0].posted[1].type, 'ack');
  assert.equal(t.natives[0].posted[1].review_id, 100);
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

const OFF_ON = async (t) => {
  assert.equal((await t.popup({ type: 'badcat:stop', tabId: 7 })).ok, true);
  assert.equal((await t.popup({ type: 'badcat:start', repo: 'o/r', tabId: 7 })).ok, true);
};
const wake = async (t) => { // fire the pending retry sleeps, then let the handlers run
  for (const { fn } of t.timers.splice(0)) fn();
  await t.settle();
};

test('a waiting retry is invalidated by OFF then ON on the same tab and repo', async () => {
  const t = await started({ fakeTimers: true, handoff: async () => ({ ok: false, error: 'busy' }) });
  t.natives[0].emit(plus1(5, HEAD1));
  await t.settle();
  assert.equal(t.sent.length, 1);
  assert.equal(t.timers.length, 1); // sleeping before the retry
  await OFF_ON(t);
  await wake(t); // the old retry wakes up inside the new session
  assert.equal(t.sent.length, 1);
  assert.equal(t.timers.length, 0);
  assert.equal(t.session().active, true);
  assert.equal(t.store['badcat.delivered.v2'], undefined);
});

test('after OFF/ON a new +1 is not held up by the old connection retry queue', async () => {
  const t = await started({ fakeTimers: true, handoff: async (m) => (m.pr === 5 ? { ok: false } : { ok: true }) });
  t.natives[0].emit(plus1(5, HEAD1));
  await t.settle();
  await OFF_ON(t);
  t.natives[1].emit(plus1(6, HEAD2));
  await t.settle(); // the old retry has not been woken
  assert.equal(JSON.stringify(t.sent.map((s) => s.message.pr)), JSON.stringify([5, 6]));
  await wake(t);
  assert.equal(t.sent.length, 2);
  assert.equal(JSON.stringify(t.store['badcat.delivered.v2']), JSON.stringify(['o/r#6@' + HEAD2 + ':100']));
});

test('a handoff that fails within a live connection gives up, and a later +1 still works', async () => {
  const t = await started({ fakeTimers: true, handoff: async (m) => (m.pr === 5 ? { ok: false } : { ok: true }) });
  t.natives[0].emit(plus1(5, HEAD1));
  await t.settle();
  for (let i = 0; i < 5; i++) await wake(t);
  assert.equal(t.sent.filter((s) => s.message.pr === 5).length, 3); // bounded retries
  assert.equal(t.timers.length, 0);
  t.natives[0].emit(plus1(6, HEAD2));
  await t.settle();
  assert.equal(t.sent.filter((s) => s.message.pr === 6).length, 1);
  assert.equal(t.session().active, true);
});

test('new review IDs on one HEAD submit twice, duplicates only re-ack', async () => {
  const t = await started();
  const p = t.natives[0];
  for (const rid of [100, 100, 200, 200]) p.emit(plus1(123, HEAD1, { review_id: rid }));
  await t.settle();
  assert.equal(t.sent.length, 2);
  assert.deepEqual(p.posted.filter(m => m.type === 'ack').map(m => m.review_id), [100, 100, 200, 200]);
});

test('durable deliveries survive browser restart; a lost ack is re-sent', async () => {
  const t = await started();
  const p = t.natives[0];
  const original = p.postMessage;
  p.postMessage = () => { throw Error('lost connection'); };
  p.emit(plus1(123, HEAD1));
  await t.settle();
  assert.equal(t.store['badcat.delivered.v2'].length, 1);
  p.postMessage = original;
  const restarted = await started({ seed: { 'badcat.delivered.v2': t.store['badcat.delivered.v2'] } });
  restarted.natives[0].emit(plus1(123, HEAD1, { delivery_id: 'd'.repeat(32) }));
  await restarted.settle();
  assert.equal(restarted.sent.length, 0);
  assert.equal(restarted.natives[0].posted[1].delivery_id, 'd'.repeat(32));
});

test('stop during in-flight submission never acknowledges into the next connection', async () => {
  let finish;
  const t = await started({ handoff: () => new Promise(resolve => { finish = resolve; }) });
  const old = t.natives[0];
  old.emit(plus1(123, HEAD1));
  await t.settle();
  await OFF_ON(t);
  finish({ ok: true });
  await t.settle();
  assert.equal(old.posted.filter(m => m.type === 'ack').length, 0);
  t.natives[1].emit(plus1(123, HEAD1));
  await t.settle();
  assert.equal(t.sent.length, 1);
  assert.equal(t.natives[1].posted[1].type, 'ack');
});

test('legacy HEAD keys are baselined once and future review IDs still submit', async () => {
  const t = await started({ seed: { 'badcat.delivered': ['o/r#123@' + HEAD1] } });
  t.natives[0].emit(plus1(123, HEAD1));
  await t.settle();
  assert.equal(t.sent.length, 0);
  t.natives[0].emit(plus1(123, HEAD1, { review_id: 200 }));
  await t.settle();
  assert.equal(t.sent.length, 1);
  assert.equal(t.natives[0].posted.filter(m => m.type === 'ack').length, 2);
});

test('two distinct in-flight events are submitted in order and acknowledged after saving', async () => {
  let finish;
  const t = await started({ handoff: m => m.pr === 1 ? new Promise(resolve => { finish = resolve; }) : Promise.resolve({ ok: true }) });
  t.natives[0].emit(plus1(1, HEAD1));
  t.natives[0].emit(plus1(1, HEAD1));
  t.natives[0].emit(plus1(2, HEAD1, { review_id: 200 }));
  await t.settle();
  assert.equal(t.sent.length, 1);
  assert.equal(t.natives[0].posted.length, 1);
  finish({ ok: true });
  await t.settle();
  assert.deepEqual(t.sent.map(s => s.message.pr), [1, 2]);
  assert.equal(t.store['badcat.delivered.v2'].length, 2);
});

test('failed content submission has bounded retries and no acknowledgement', async () => {
  const t = await started({ fakeTimers: true, handoff: async () => ({ ok: false }) });
  t.natives[0].emit(plus1(123, HEAD1));
  await t.settle();
  for (let i = 0; i < 5; i++) await wake(t);
  assert.equal(t.sent.length, 3);
  assert.equal(t.natives[0].posted.filter(m => m.type === 'ack').length, 0);
  assert.match(t.session().error, /Handoff failed/);
  assert.equal(t.store['badcat.delivered.v2'], undefined);
});

test('host disconnect cancels the original content session', async () => {
  const t = await started();
  const p = t.natives[0];
  p.drop();
  await t.settle();
  p.emit(plus1(1, HEAD1));
  await t.settle();
  assert.equal(t.session().active, false);
  assert.equal(t.sent.length, 0);
  assert.equal(p.posted.filter(m => m.type === 'ack').length, 0);
  const activate = t.controls.find(m => m.type === 'badcat:activate');
  const cancel = t.controls.find(m => m.type === 'badcat:cancel');
  assert.equal(cancel.session, activate.session);
});

test('host migration baseline does not suppress a fresh same-HEAD review in the old extension state', async () => {
  const t = await started({ seed: { 'badcat.delivered': ['o/r#123@' + HEAD1] } });
  t.natives[0].emit(plus1(123, HEAD1, { review_id: 200, legacy_review_id: 100 }));
  await t.settle();
  assert.equal(t.sent.length, 1);
  assert.equal(t.natives[0].posted[1].review_id, 200);
  assert.equal(t.store['badcat.delivered'].length, 0);
});

// --- conversation-limit rollover ---
const PROJECT_URL = 'https://chatgpt.com/g/g-p-1-proj/c/old';
const NEW_CHAT = 'https://chatgpt.com/';

async function rolled(opts = {}) {
  const t = await started({ tabUrl: PROJECT_URL, ...opts });
  const sid = t.natives[0].sessionId;
  assert.equal((await t.fromContent({ type: 'badcat:rollover', session: sid })).ok, true);
  return { t, sid };
}

test('rollover keeps Watching on the new-chat route, then settles on the new conversation in the same project', async () => {
  const { t } = await rolled();
  await t.navigate(NEW_CHAT);
  assert.equal(t.session().active, true);
  assert.equal(t.natives[0].disconnected, false);
  assert.equal(t.session().tabId, 7);
  await t.navigate('https://chatgpt.com/g/g-p-1-proj/c/new');
  assert.equal(t.session().active, true);
  assert.equal('rollover' in t.session(), false);
  await t.navigate(NEW_CHAT); // the rollover is over: the root is no longer a valid state
  assert.equal(t.session().active, false);
});

test('the new-chat route without an announced rollover still deactivates', async () => {
  const t = await started();
  await t.navigate(NEW_CHAT);
  assert.equal(t.session().active, false);
  assert.equal(t.natives[0].disconnected, true);
});

test('leaving chatgpt.com or opening another project during a rollover deactivates', async () => {
  let { t } = await rolled();
  await t.navigate('https://example.com/');
  assert.equal(t.session().active, false);
  ({ t } = await rolled());
  await t.navigate(NEW_CHAT);
  await t.navigate('https://chatgpt.com/g/g-p-2-other/c/new');
  assert.equal(t.session().active, false);
  assert.match(t.session().error, /project/);
});

test('rollover is accepted only from the bound tab, top frame, live session id and a conversation URL', async () => {
  const t = await started({ tabUrl: PROJECT_URL });
  const sid = t.natives[0].sessionId;
  const msg = { type: 'badcat:rollover', session: sid };
  assert.equal((await t.fromContent({ ...msg, session: 'other' })).ok, false);
  assert.equal((await t.fromContent(msg, { tab: { id: 8 } })).ok, false);
  assert.equal((await t.fromContent(msg, { frameId: 3 })).ok, false);
  assert.equal((await t.fromContent(msg, { url: 'https://chatgpt.com/' })).ok, false);
  assert.equal((await t.fromContent(msg, { id: 'other' })), undefined);
  assert.equal((await t.fromContent({ type: 'badcat:start', repo: 'o/r', tabId: 7 })), undefined); // no control from content
  assert.equal('rollover' in t.session(), false);
});

test('rollover without an active session is refused', async () => {
  const t = boot({ tabUrl: PROJECT_URL });
  assert.equal((await t.fromContent({ type: 'badcat:rollover', session: 'x' })).ok, false);
});

test('hello returns the live session id only to the bound tab on a valid page', async () => {
  const { t, sid } = await rolled();
  await t.navigate(NEW_CHAT);
  const answer = await t.fromContent({ type: 'badcat:hello' });
  assert.equal(answer.ok, true);
  assert.equal(answer.session, sid);
  assert.equal((await t.fromContent({ type: 'badcat:hello' }, { tab: { id: 8 } })).ok, false);
  assert.equal((await t.fromContent({ type: 'badcat:hello' }, { url: 'https://chatgpt.com/settings' })).ok, false);
  await t.popup({ type: 'badcat:stop', tabId: 7 });
  assert.equal((await t.fromContent({ type: 'badcat:hello' })).ok, false);
});

test('without a rollover the new-chat route is not a valid hello page', async () => {
  const t = await started();
  assert.equal((await t.fromContent({ type: 'badcat:hello' }, { url: NEW_CHAT })).ok, false);
});

test('a +1 on the new-chat route is handed off to the same tab and acknowledged; an old key is not resent', async () => {
  const { t } = await rolled();
  await t.navigate(NEW_CHAT);
  t.natives[0].emit(plus1(123, HEAD1));
  await t.settle();
  assert.equal(t.sent.length, 1);
  assert.equal(t.sent[0].id, 7);
  assert.equal(t.sent[0].message.requireProject, true);
  assert.equal(t.natives[0].posted.filter((m) => m.type === 'ack').length, 1);
  t.natives[0].emit(plus1(123, HEAD1)); // replay after a lost ack
  await t.settle();
  assert.equal(t.sent.length, 1);
  assert.equal(t.natives[0].posted.filter((m) => m.type === 'ack').length, 2);
});

test('stopping mid-rollover deactivates and the new-chat route is no longer valid', async () => {
  const { t } = await rolled();
  await t.popup({ type: 'badcat:stop', tabId: 7 });
  assert.equal(t.session().active, false);
  assert.equal(t.natives[0].disconnected, true);
  assert.equal('rollover' in t.session(), false);
});

test('closing the tab during a rollover deactivates', async () => {
  const { t } = await rolled();
  await t.listeners['tabs.onRemoved'](7);
  assert.equal(t.session().active, false);
});

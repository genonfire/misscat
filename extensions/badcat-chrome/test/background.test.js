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

// --- conversation-limit rollover (only ever started while a +1 is being handed off) ---
const PROJECT_URL = 'https://chatgpt.com/g/g-p-1-proj/c/old';
const PLAIN_URL = 'https://chatgpt.com/c/old';
const HOME = 'https://chatgpt.com/g/g-p-1-proj/project';
const NEW_CHAT = 'https://chatgpt.com/';

// A session whose first handoff attempt for E0 finds the limit alert: content announces the rollover and clicks.
// Later attempts succeed. The retry of E0 stays asleep until `wake`.
async function announced(opts = {}) {
  const box = {};
  const t = await started({ tabUrl: PROJECT_URL, fakeTimers: true, ...opts, handoff: async () => {
    if (box.announced) return { ok: true };
    box.announced = await box.t.fromContent({ type: 'badcat:rollover', session: box.t.natives[0].sessionId });
    return { ok: false, rollover: true, error: 'rolling over' };
  } });
  box.t = t;
  t.natives[0].emit(plus1(1, HEAD1));
  await t.settle();
  assert.equal(box.announced.ok, true);
  return { t, sid: t.natives[0].sessionId };
}

test('a +1 handoff authorizes a rollover; the session survives the new-chat route and settles on the new conversation in the same project', async () => {
  const { t } = await announced();
  assert.equal(t.session().rollover.project, 'g-p-1-proj');
  await t.navigate(NEW_CHAT);
  assert.deepEqual(t.updates, []); // no forced redirect
  assert.equal(t.session().active, true);
  assert.equal(t.natives[0].disconnected, false);
  assert.equal(t.session().tabId, 7);
  await t.navigate('https://chatgpt.com/g/g-p-1-proj/c/new');
  assert.equal(t.session().active, true);
  assert.equal('rollover' in t.session(), false);
  await t.navigate(NEW_CHAT); // the rollover is over: the root is no longer a valid state
  assert.equal(t.session().active, false);
});

test('the same pending +1 is delivered after the rollover, with the project, acknowledged once and never resent', async () => {
  const { t } = await announced();
  assert.equal(t.natives[0].posted.filter((m) => m.type === 'ack').length, 0);
  await t.navigate(NEW_CHAT);
  await wake(t); // the retry of the same event
  assert.equal(t.sent.length, 2);
  assert.equal(t.sent[1].id, 7);
  assert.equal(t.sent[1].message.project, 'g-p-1-proj');
  assert.equal(t.natives[0].posted.filter((m) => m.type === 'ack').length, 1);
  assert.equal(t.store['badcat.delivered.v2'].length, 1);
  t.natives[0].emit(plus1(1, HEAD1)); // replay after a lost ack
  await t.settle();
  assert.equal(t.sent.length, 2);
  assert.equal(t.natives[0].posted.filter((m) => m.type === 'ack').length, 2);
});

test('a plain (non-project) conversation rolls over to the bare new-chat route without a project', async () => {
  const { t } = await announced({ tabUrl: PLAIN_URL });
  assert.equal(t.session().rollover.project, null);
  await t.navigate(NEW_CHAT);
  assert.equal(t.session().active, true);
  await wake(t);
  assert.equal('project' in t.sent[1].message, false);
  assert.equal(t.natives[0].posted.filter((m) => m.type === 'ack').length, 1);
});

test('a rollover cannot be announced without a pending +1, nor twice while one is in progress', async () => {
  const t = await started({ tabUrl: PROJECT_URL, fakeTimers: true });
  const sid = t.natives[0].sessionId;
  assert.equal((await t.fromContent({ type: 'badcat:rollover', session: sid })).ok, false); // no event: no rollover
  assert.equal('rollover' in t.session(), false);
  const { t: u, sid: usid } = await announced();
  assert.equal((await u.fromContent({ type: 'badcat:rollover', session: usid })).ok, false);
});

test('the new-chat route without an announced rollover still deactivates', async () => {
  const t = await started();
  await t.navigate(NEW_CHAT);
  assert.equal(t.session().active, false);
  assert.equal(t.natives[0].disconnected, true);
  assert.deepEqual(t.updates, []);
});

test('during a rollover a conversation in another project, a project home or another site deactivates', async () => {
  for (const url of ['https://chatgpt.com/g/g-p-2-other/c/new', 'https://chatgpt.com/g/g-p-1-proj-archive/c/new',
    HOME, 'https://example.com/']) {
    const { t } = await announced();
    await t.navigate(NEW_CHAT);
    await t.navigate(url);
    assert.equal(t.session().active, false, url);
  }
});

test('rollover is accepted only from the bound tab, top frame, live session id and a conversation URL', async () => {
  const { t, sid } = await (async () => {
    // keep the event pending without announcing: the handoff just fails
    const t = await started({ tabUrl: PROJECT_URL, fakeTimers: true, handoff: async () => ({ ok: false, error: 'busy' }) });
    t.natives[0].emit(plus1(1, HEAD1));
    await t.settle();
    return { t, sid: t.natives[0].sessionId };
  })();
  const msg = { type: 'badcat:rollover', session: sid };
  assert.equal((await t.fromContent({ ...msg, session: 'other' })).ok, false);
  assert.equal((await t.fromContent(msg, { tab: { id: 8 } })).ok, false);
  assert.equal((await t.fromContent(msg, { frameId: 3 })).ok, false);
  assert.equal((await t.fromContent(msg, { url: 'https://chatgpt.com/' })).ok, false);
  assert.equal((await t.fromContent(msg, { id: 'other' })), undefined);
  assert.equal((await t.fromContent({ type: 'badcat:start', repo: 'o/r', tabId: 7 })), undefined); // no control from content
  assert.equal('rollover' in t.session(), false);
  assert.equal((await t.fromContent(msg)).ok, true); // the same message from the right place works
});

test('rollover without an active session is refused', async () => {
  const t = boot({ tabUrl: PROJECT_URL });
  assert.equal((await t.fromContent({ type: 'badcat:rollover', session: 'x' })).ok, false);
});

test('hello returns the live session id only to the bound tab on the new-chat route of a rollover', async () => {
  const { t, sid } = await announced();
  await t.navigate(NEW_CHAT);
  const answer = await t.fromContent({ type: 'badcat:hello' }, { url: NEW_CHAT });
  assert.equal(answer.ok, true);
  assert.equal(answer.session, sid);
  assert.equal((await t.fromContent({ type: 'badcat:hello' }, { url: HOME })).ok, false);
  assert.equal((await t.fromContent({ type: 'badcat:hello' }, { tab: { id: 8 } })).ok, false);
  await t.popup({ type: 'badcat:stop', tabId: 7 });
  assert.equal((await t.fromContent({ type: 'badcat:hello' }, { url: NEW_CHAT })).ok, false);
});

test('without a rollover neither "/" nor a project home is a valid hello page', async () => {
  const t = await started();
  assert.equal((await t.fromContent({ type: 'badcat:hello' }, { url: NEW_CHAT })).ok, false);
  assert.equal((await t.fromContent({ type: 'badcat:hello' }, { url: HOME })).ok, false);
});

test('a +1 is not sent when the tab is already in another project\'s conversation, even before onUpdated has run', async () => {
  const { t } = await announced();
  t.tabs[7].url = 'https://chatgpt.com/g/g-p-2-other/c/new'; // no onUpdated delivered yet
  await wake(t);
  assert.equal(t.sent.length, 1); // only the attempt that announced the rollover
  assert.equal(t.natives[0].posted.filter((m) => m.type === 'ack').length, 0);
  assert.equal(t.session().active, false);
});

test('stopping mid-rollover deactivates; the sleeping retry never sends or acknowledges', async () => {
  const { t } = await announced();
  await t.popup({ type: 'badcat:stop', tabId: 7 });
  assert.equal(t.session().active, false);
  assert.equal(t.natives[0].disconnected, true);
  assert.equal('rollover' in t.session(), false);
  await wake(t);
  assert.equal(t.sent.length, 1);
  assert.equal(t.natives[0].posted.filter((m) => m.type === 'ack').length, 0);
});

test('closing the tab during a rollover deactivates', async () => {
  const { t } = await announced();
  await t.listeners['tabs.onRemoved'](7);
  assert.equal(t.session().active, false);
});

test('Watching alone (no event) never triggers anything: no message to the tab beyond activation', async () => {
  const t = await started({ tabUrl: PROJECT_URL });
  await t.settle();
  assert.equal(t.sent.length, 0);
  assert.equal(t.controls.filter((m) => m.type !== 'badcat:ping' && m.type !== 'badcat:activate').length, 0);
});

test('an unconfirmed submission is neither retried, acknowledged nor marked delivered; the error stays visible', async () => {
  const t = await started({ fakeTimers: true, handoff: async () => ({ ok: false, ambiguous: true, error: 'unconfirmed' }) });
  t.natives[0].emit(plus1(5, HEAD1));
  await t.settle();
  assert.equal(t.sent.length, 1);
  assert.equal(t.timers.length, 0); // no retry sleep
  assert.equal(t.natives[0].posted.filter((m) => m.type === 'ack').length, 0);
  assert.equal(t.store['badcat.delivered.v2'], undefined);
  assert.match(t.session().error, /could not be confirmed/);
  assert.equal(t.session().active, true);
});

test('an unconfirmed event stays blocked after a content-script reload and a host replay; the error stays', async () => {
  let n = 0;
  const t = await started({ fakeTimers: true, handoff: async () => (++n === 1 ? { ok: false, ambiguous: true, error: 'unconfirmed' } : { ok: true }) });
  t.natives[0].emit(plus1(5, HEAD1));
  await t.settle();
  assert.equal(t.sent.length, 1);
  assert.equal((await t.fromContent({ type: 'badcat:hello' })).ok, true); // the reloaded tab rebinds
  t.natives[0].emit(plus1(5, HEAD1)); // the host replays the unacknowledged event
  await t.settle();
  assert.equal(t.sent.length, 1);
  assert.equal(t.natives[0].posted.filter((m) => m.type === 'ack').length, 0);
  assert.equal(t.store['badcat.delivered.v2'], undefined);
  assert.match(t.session().error, /could not be confirmed/);
  assert.equal(t.session().active, true);
  t.natives[0].emit(plus1(6, HEAD2, { review_id: 101 })); // a different event is not blocked
  await t.settle();
  assert.equal(t.sent.length, 2);
  assert.equal(t.natives[0].posted.filter((m) => m.type === 'ack').length, 1);
});

test('the block survives a service-worker restart and is lifted only by an explicit new Start', async () => {
  const t = await started({ fakeTimers: true, handoff: async () => ({ ok: false, ambiguous: true, error: 'unconfirmed' }) });
  t.natives[0].emit(plus1(5, HEAD1));
  await t.settle();
  assert.deepEqual(t.store['badcat.uncertain.v1'], ['o/r#5@' + HEAD1 + ':100']);
  await t.popup({ type: 'badcat:stop', tabId: 7 });
  assert.deepEqual(t.store['badcat.uncertain.v1'], ['o/r#5@' + HEAD1 + ':100']); // Stop alone keeps it
  await t.popup({ type: 'badcat:start', repo: 'o/r', tabId: 7 });
  assert.equal(t.store['badcat.uncertain.v1'], undefined);
});

// --- attempt marker: a broken channel after the submit must never cause a resend ---
const KEY5 = 'o/r#5@' + HEAD1 + ':100';
const uncertainStore = (t) => t.store['badcat.uncertain.v1'];
const acks = (t, i = 0) => t.natives[i].posted.filter((m) => m.type === 'ack').length;

// A handoff stub that records the attempt first (as the content script does right before Enter), then ends as `how`.
function attempting(boxRef, how) {
  return async () => {
    const t = boxRef.t;
    const res = await t.fromContent({ type: 'badcat:attempt', session: t.natives[t.natives.length - 1].sessionId });
    assert.equal(res.ok, true);
    if (how === 'throw') throw new Error('The message channel is closed before a response was received.');
    return how === 'ok' ? { ok: true } : how === 'ambiguous' ? { ok: false, ambiguous: true, error: 'u' } : { ok: false, error: 'message was not submitted' };
  };
}
const bootAttempting = async (how) => {
  const box = {};
  box.t = await started({ fakeTimers: true, handoff: attempting(box, how) });
  return box.t;
};

test('the channel breaks after the attempt was recorded (page navigation right after the submit): no retry, no ACK, no resend after replay or hello', async () => {
  const t = await bootAttempting('throw');
  t.natives[0].emit(plus1(5, HEAD1));
  await t.settle();
  assert.equal(t.sent.length, 1);
  assert.equal(t.timers.length, 0); // no retry sleep
  assert.deepEqual(uncertainStore(t), [KEY5]);
  assert.equal(acks(t), 0);
  assert.equal(t.store['badcat.delivered.v2'], undefined);
  assert.match(t.session().error, /could not be confirmed/);
  assert.equal((await t.fromContent({ type: 'badcat:hello' })).ok, true); // the new page rebinds
  t.natives[0].emit(plus1(5, HEAD1)); // the host replays the event
  await t.settle();
  assert.equal(t.sent.length, 1);
  assert.equal(acks(t), 0);
});

test('control: a channel error before any attempt (nothing was submitted) is retried as before', async () => {
  const t = await started({ fakeTimers: true, handoff: async () => { throw new Error('Could not establish connection. Receiving end does not exist.'); } });
  t.natives[0].emit(plus1(5, HEAD1));
  await t.settle();
  assert.equal(t.timers.length, 1); // sleeping before the retry
  assert.equal(uncertainStore(t), undefined);
});

test('a definite "not submitted" after the attempt withdraws the marker and keeps the retry policy', async () => {
  const t = await bootAttempting('notsubmitted');
  t.natives[0].emit(plus1(5, HEAD1));
  await t.settle();
  assert.equal(t.timers.length, 1);
  assert.deepEqual(uncertainStore(t), []);
});

test('a confirmed submission after the attempt clears the marker, saves delivered and acknowledges once', async () => {
  const t = await bootAttempting('ok');
  t.natives[0].emit(plus1(5, HEAD1));
  await t.settle();
  assert.deepEqual(uncertainStore(t), []);
  assert.deepEqual(t.store['badcat.delivered.v2'], [KEY5]);
  assert.equal(acks(t), 1);
});

test('an attempt is accepted only for the event being sent, from the bound tab and the live session', async () => {
  const t = await started({ fakeTimers: true, handoff: async () => ({ ok: false, error: 'busy' }) });
  const sid = t.natives[0].sessionId;
  assert.equal((await t.fromContent({ type: 'badcat:attempt', session: sid })).ok, false); // nothing in flight
  assert.equal(uncertainStore(t), undefined);
});

test('late answers of an older session never touch the new session (OFF then ON while a handoff was in flight)', async () => {
  for (const how of ['ambiguous', 'throw', 'ok', 'notsubmitted']) {
    let release;
    const gate = new Promise((r) => { release = r; });
    const box = {};
    let first = true;
    box.t = await started({ fakeTimers: true, handoff: async () => {
      if (first) {
        first = false;
        const t = box.t;
        await t.fromContent({ type: 'badcat:attempt', session: t.natives[0].sessionId }); // A had recorded an attempt
        await gate;
        if (how === 'throw') throw new Error('closed');
        return how === 'ok' ? { ok: true } : how === 'ambiguous' ? { ok: false, ambiguous: true } : { ok: false, error: 'x' };
      }
      return { ok: true };
    } });
    const t = box.t;
    t.natives[0].emit(plus1(5, HEAD1));
    await t.settle();
    await OFF_ON(t); // session B; its start clears the stale marker of A
    assert.equal(t.store['badcat.uncertain.v1'], undefined, how);
    release();
    await t.settle();
    if (how !== 'ok') assert.equal(t.store['badcat.uncertain.v1'], undefined, how); // A's late answer left no trace
    assert.equal(t.session().error, null, how);
    assert.equal(t.session().active, true, how);
    assert.equal(acks(t, 1), 0, how);
    t.natives[1].emit(plus1(5, HEAD1, { review_id: 100 })); // the same event in B is handled normally
    await t.settle();
    assert.equal(acks(t, 1) >= 1, true, how);
  }
});

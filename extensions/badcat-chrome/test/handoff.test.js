// Real worker -> content listener -> composer, with Chrome/DOM adapters only.
const test = require('node:test');
const assert = require('node:assert/strict');
const { bootBackground } = require('./background-fixture.js');
const { bootContent } = require('./content-fixture.js');
const { eventKey } = require('../shared.js');
const event = (extra = {}) => ({ repo: 'o/r', pr: 558,
  head: '2abfbcf2560b958be0bb5ee64991d218d6510517', status: '+1',
  review_id: 5481963549, delivery_id: 'c'.repeat(32), ...extra });
const command = '현재 작업 끝났으면 PR #558 리뷰해.';

async function boot(options = {}, workerOptions = {}) {
  const content = bootContent(options);
  const worker = bootBackground({ fakeTimers: true, control: m => content.send(m),
    handoff: m => content.send(m), ...workerOptions });
  assert.equal((await worker.popup({ type: 'badcat:start', repo: 'o/r', tabId: 7 })).ok, true);
  const port = worker.natives[0];
  const original = port.postMessage;
  port.postMessage = function (m) {
    if (m.type === 'ack') {
      assert.ok(content.log.submitted.length > 0, 'ACK follows actual submission');
      assert.ok(worker.store['badcat.delivered.v2'].includes(eventKey(m)), 'ACK follows durable storage');
    }
    original.call(this, m);
  };
  return { content, worker, port };
}
const acks = p => JSON.parse(JSON.stringify(p.posted.filter(m => m.type === 'ack')));
const flush = async w => { for (let i = 0; i < 100; i++) await w.settle(); };
const retries = async w => {
  for (let i = 0; i < 3; i++) {
    await flush(w);
    for (const { fn } of w.timers.splice(0)) fn();
  }
  await flush(w);
};

test('native +1 crosses the real worker/content boundary; duplicates re-ACK and new IDs submit', async () => {
  const { content, worker, port } = await boot({ initial: 'user text' });
  const raw = event();
  port.emit(raw);
  await flush(worker);
  assert.deepEqual(JSON.parse(JSON.stringify(worker.sent[0].message.event)), raw);
  assert.deepEqual(content.log.submitted, ['user text ' + command]);
  assert.deepEqual(acks(port), [{ type: 'ack', repo: raw.repo, pr: raw.pr, head: raw.head,
    review_id: raw.review_id, delivery_id: raw.delivery_id }]);
  port.emit(event({ delivery_id: 'd'.repeat(32) }));
  await flush(worker);
  assert.equal(content.log.submitted.length, 1);
  assert.equal(acks(port)[1].delivery_id, 'd'.repeat(32));
  port.emit(event({ review_id: raw.review_id + 1 }));
  await flush(worker);
  assert.deepEqual(content.log.submitted, ['user text ' + command, command]);
  assert.equal(acks(port).length, 3);
});

test('invalid raw status, repo, HEAD and IDs never reach the composer or ACK', async () => {
  const { content, worker, port } = await boot();
  for (const extra of [{ status: undefined }, { status: '+2' }, { status: '-1' },
    { repo: 'other/repo' }, { head: 'wrong' }, { head: 'g'.repeat(40) },
    { review_id: undefined }, { review_id: '5481963549' }, { review_id: 0 },
    { delivery_id: undefined }, { delivery_id: 'z'.repeat(32) }]) port.emit(event(extra));
  await flush(worker);
  assert.equal(worker.sent.length, 0);
  assert.deepEqual(content.log.fills, []);
  assert.deepEqual(acks(port), []);
});

test('failed content submission stays unacknowledged, host replay succeeds once', async () => {
  const { content, worker, port } = await boot({ enterSubmits: false });
  port.emit(event());
  await retries(worker);
  assert.equal(worker.sent.length, 3);
  assert.deepEqual(acks(port), []);
  assert.equal(worker.store['badcat.delivered.v2'], undefined);
  assert.equal(content.text(), command);
  assert.equal(content.log.fills.length, 1);
  content.setEnterSubmits(true);
  port.emit(event());
  await flush(worker);
  assert.deepEqual(content.log.submitted, [command]);
  assert.equal(acks(port).length, 1);
});

test('Stop and same-tab restart cancel pending content and old host retries', async () => {
  const { content, worker, port } = await boot({ enterSubmits: false, buttonSubmits: false });
  port.emit(event());
  // Allow typing, the button and the Enter fallback, but stop during content's confirmation wait.
  for (let i = 0; i < 100 && content.log.keys.length === 0; i++) await worker.settle();
  assert.ok(content.log.keys.length > 0, "handoff reached the composer");
  const stale = worker.sent[0].message;
  await worker.popup({ type: 'badcat:stop', tabId: 7 });
  await worker.popup({ type: 'badcat:start', repo: 'o/r', tabId: 7 });
  assert.equal((await content.send(stale)).ok, false);
  await retries(worker);
  port.emit(event());
  await flush(worker);
  assert.equal(content.log.clicks, 1); // only the click of the first, stopped attempt
  assert.deepEqual(content.log.submitted, []);
  assert.deepEqual(acks(port), []);
  content.setEnterSubmits(true);
  worker.natives[1].emit(event());
  await flush(worker);
  assert.deepEqual(content.log.submitted, [command]);
  assert.equal(acks(worker.natives[1]).length, 1);
});

test('host disconnect cancels content before send fallback', async () => {
  const { content, worker, port } = await boot({ enterSubmits: false, buttonSubmits: false });
  port.emit(event());
  for (let i = 0; i < 100 && content.log.keys.length === 0; i++) await worker.settle();
  assert.ok(content.log.keys.length > 0, "handoff reached the composer");
  port.drop();
  await retries(worker);
  assert.equal(content.log.clicks, 1);
  assert.deepEqual(content.log.submitted, []);
  assert.deepEqual(acks(port), []);
});


test('storage failure after submission withholds ACK; retry saves without another prompt', async () => {
  let fail = true;
  const { content, worker, port } = await boot({}, { beforeLocalSet: async obj => {
    if ('badcat.delivered.v2' in obj && fail) throw Error('storage unavailable');
  } });
  port.emit(event());
  await retries(worker);
  assert.deepEqual(content.log.submitted, [command]);
  assert.equal(worker.store['badcat.delivered.v2'], undefined);
  assert.deepEqual(acks(port), []);
  fail = false;
  port.emit(event());
  await flush(worker);
  assert.deepEqual(content.log.submitted, [command]);
  assert.equal(acks(port).length, 1);
});

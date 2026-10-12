// Runs content.js against a minimal reproduction of ChatGPT's composer DOM (no jsdom: nothing to
// install). The tiny selector engine below evaluates the real selectors from content.js against that
// tree, so if a selector stops matching the real structure these tests fail.
//
//   <form>                                       <- common container
//     <div data-composer-input>                  <- nearest ancestor of the editor; the send button is NOT inside it
//       <div><div contenteditable="true" data-composer-markdown class="ProseMirror"><p>…</p></div></div>
//     </div>
//     <footer>
//       <button type="button">  (attach / voice / stop: never the send button)
//       <button type="submit">  (send)
//     </footer>
//   </form>
//   plus decoys elsewhere: another contenteditable, another form's submit button, a legacy #prompt-textarea.
const test = require('node:test');
const assert = require('node:assert/strict');

const TEXT = '현재 작업 끝났으면 PR #123 리뷰해.';

const { bootContent: boot } = require('./content-fixture.js');

test('ping answers only the extension itself', async () => {
  const t = boot();
  assert.equal((await t.send({ type: 'badcat:ping' })).ok, true);
  assert.equal(await t.send({ type: 'badcat:ping' }, { id: 'other' }), undefined);
  assert.equal(await t.send({ type: 'unknown' }), undefined);
});

test('finds the composer by its data-composer-markdown contenteditable, not by #prompt-textarea', async () => {
  const t = boot();
  const res = await t.send({ type: 'badcat:handoff', pr: 123 });
  assert.equal(res.ok, true);
  assert.deepEqual(t.log.submitted, [TEXT]);
  assert.equal(t.log.fills.length, 1); // the other contenteditable and the legacy textarea were left alone
});

test('puts exactly "현재 작업 끝났으면 PR #123 리뷰해." in the composer and submits it with Enter', async () => {
  const t = boot();
  const res = await t.send({ type: 'badcat:handoff', pr: 123 });
  assert.equal(res.ok, true);
  assert.deepEqual(t.log.submitted, [TEXT]);
  assert.deepEqual(t.log.keys, ['keydown:Enter', 'keyup:Enter']);
  assert.equal(t.log.clicks, 0); // Enter was enough; the button is only a fallback
});

test('falls back to the submit button inside the composer form when Enter does not submit', async () => {
  const t = boot({ enterSubmits: false, buttonSubmits: true });
  const res = await t.send({ type: 'badcat:handoff', pr: 123 });
  assert.equal(res.ok, true);
  assert.equal(t.log.clicks, 1);
  assert.equal(t.log.decoyClicks, 0); // not the other form's submit, not the type="button" controls
  assert.deepEqual(t.log.submitted, [TEXT]);
});

test('with no submit button in the composer form (e.g. ChatGPT is answering) nothing else is clicked', async () => {
  const t = boot({ enterSubmits: false, sendButton: false });
  const res = await t.send({ type: 'badcat:handoff', pr: 123 });
  assert.equal(res.ok, false);
  assert.equal(t.log.clicks + t.log.decoyClicks, 0);
  assert.equal(t.text(), TEXT);
});

test('a failed submission leaves the text in the composer', async () => {
  const t = boot({ enterSubmits: false, buttonSubmits: false });
  const res = await t.send({ type: 'badcat:handoff', pr: 123 });
  assert.equal(res.ok, false);
  assert.equal(t.text(), TEXT);
  assert.deepEqual(t.log.fills, [TEXT]); // never cleared
  assert.deepEqual(t.log.submitted, []);
});

test('a pending handoff is kept and the next PR is appended after one space, then submitted', async () => {
  const t = boot({ initial: '현재 작업 끝났으면 PR #365 리뷰해.', enterSubmits: false });
  t.setEnterSubmits(true);
  const res = await t.send({ type: 'badcat:handoff', pr: 366 });
  assert.equal(res.ok, true);
  assert.deepEqual(t.log.submitted, ['현재 작업 끝났으면 PR #365 리뷰해. 현재 작업 끝났으면 PR #366 리뷰해.']);
});

test('an empty composer gets just the command', async () => {
  const t = boot({ initial: '   ' });
  assert.equal((await t.send({ type: 'badcat:handoff', pr: 365 })).ok, true);
  assert.deepEqual(t.log.submitted, ['현재 작업 끝났으면 PR #365 리뷰해.']);
});

test('a retry or duplicate event does not append the same handoff twice', async () => {
  const t = boot({ enterSubmits: false });
  await t.send({ type: 'badcat:handoff', pr: 5 });
  await t.send({ type: 'badcat:handoff', pr: 5 });
  assert.equal(t.text(), '현재 작업 끝났으면 PR #5 리뷰해.');
  await t.send({ type: 'badcat:handoff', pr: 6 });
  assert.equal(t.text(), '현재 작업 끝났으면 PR #5 리뷰해. 현재 작업 끝났으면 PR #6 리뷰해.');
  await t.send({ type: 'badcat:handoff', pr: 6 });
  await t.send({ type: 'badcat:handoff', pr: 5 });
  assert.equal(t.text(), '현재 작업 끝났으면 PR #5 리뷰해. 현재 작업 끝났으면 PR #6 리뷰해.');
  t.setEnterSubmits(true);
  assert.equal((await t.send({ type: 'badcat:handoff', pr: 5 })).ok, true);
  assert.deepEqual(t.log.submitted, ['현재 작업 끝났으면 PR #5 리뷰해. 현재 작업 끝났으면 PR #6 리뷰해.']);
});

test('PR #5 is not mistaken for already pending when only PR #15 is', async () => {
  const t = boot({ initial: '현재 작업 끝났으면 PR #15 리뷰해.', enterSubmits: false });
  await t.send({ type: 'badcat:handoff', pr: 5 });
  assert.equal(t.text(), '현재 작업 끝났으면 PR #15 리뷰해. 현재 작업 끝났으면 PR #5 리뷰해.');
});

test('fails when the composer cannot be found or the page is not a conversation', async () => {
  let t = boot({ composer: false });
  assert.match((await t.send({ type: 'badcat:handoff', pr: 1 })).error, /composer not found/);
  assert.deepEqual(t.log.fills, []); // the unrelated contenteditable and legacy textarea are not used
  t = boot({ url: 'https://chatgpt.com/' });
  assert.match((await t.send({ type: 'badcat:handoff', pr: 1 })).error, /not a conversation/);
  assert.deepEqual(t.log.fills, []);
});

test('rejects a PR number that is not a positive integer; nothing is typed', async () => {
  const t = boot();
  for (const pr of ['1', '1; x', 0, -1, 1.5, null, undefined]) {
    const res = await t.send({ type: 'badcat:handoff', pr });
    assert.equal(res.ok, false, String(pr));
  }
  assert.deepEqual(t.log.fills, []);
});

const event = (rid = 100) => ({ repo: 'o/r', pr: 123, head: 'a'.repeat(40), status: '+1', review_id: rid, delivery_id: 'c'.repeat(32) });
test('same ID is idempotent across worker connections; fresh same-HEAD ID submits', async () => {
  const t = boot({ initial: 'user text' });
  await t.send({ type: 'badcat:activate', session: 'old' });
  const message = { type: 'badcat:handoff', pr: 123, event: event(), session: 'old' };
  assert.equal((await t.send(message)).ok, true);
  await t.send({ type: 'badcat:activate', session: 'new' });
  assert.equal((await t.send({ ...message, session: 'new' })).ok, true);
  assert.equal(t.log.submitted.length, 1);
  assert.equal(t.log.submitted[0], 'user text ' + TEXT);
  assert.equal((await t.send({ ...message, session: 'new', event: event(200) })).ok, true);
  assert.equal(t.log.submitted.length, 2);
});

test('stop while waiting never clicks a send fallback or permits a stale session', async () => {
  const t = boot({ enterSubmits: false, buttonSubmits: true });
  await t.send({ type: 'badcat:activate', session: 'old' });
  const pending = t.send({ type: 'badcat:handoff', pr: 123, event: event(), session: 'old' });
  await new Promise(resolve => setImmediate(resolve));
  await t.send({ type: 'badcat:cancel', session: 'old' });
  assert.equal((await pending).ok, false);
  assert.equal(t.log.clicks, 0);
  assert.equal(t.log.submitted.length, 0);
  assert.equal((await t.send({ type: 'badcat:handoff', pr: 123, event: event(), session: 'old' })).ok, false);
});

// --- conversation-limit rollover ---
const SESSION = 's1';
const LIMIT = '이 대화의 최대 길이에 도달했으나,새 채팅을 시작해 계속 이야기할 수 있습니다.';
const activated = async (opts) => {
  const t = boot(opts);
  await t.send({ type: 'badcat:activate', session: SESSION });
  await t.settle();
  return t;
};

test('the real limit alert is announced to the background, then "새 대화 시작" is clicked exactly once', async () => {
  const t = await activated();
  const alert = t.limitAlert(LIMIT, ['새 대화 시작', '새 대화 시작']); // duplicated button as captured
  t.mutate(); t.mutate(); t.mutate();
  await t.settle(); await t.settle();
  assert.equal(JSON.stringify(t.runtimeCalls.filter((m) => m.type === 'badcat:rollover')), JSON.stringify([{ type: 'badcat:rollover', session: SESSION }]));
  assert.deepEqual(alert.buttons.map((b) => b.clicks), [1, 0]);
});

test('the alert already on the page at activation is handled by the initial check ("최대 깊이" wording too)', async () => {
  const t = boot();
  const alert = t.limitAlert('이 대화는 최대 깊이에 도달했습니다.', ['새 대화 시작']);
  await t.send({ type: 'badcat:activate', session: SESSION });
  await t.settle(); await t.settle();
  assert.equal(alert.buttons[0].clicks, 1);
});

test('other alerts, or a limit text without the new-chat button, are never clicked', async () => {
  const t = await activated();
  const other = t.limitAlert('네트워크 오류가 발생했습니다.', ['새 대화 시작']);
  const noButton = t.limitAlert(LIMIT, ['닫기']);
  t.mutate();
  await t.settle(); await t.settle();
  assert.equal(other.buttons[0].clicks + noButton.buttons[0].clicks, 0);
  assert.equal(t.runtimeCalls.length, 1); // only the hello at load
});

test('without an active session nothing is clicked; a refusal by the background leaves the alert alone', async () => {
  let t = boot();
  const a = t.limitAlert(LIMIT, ['새 대화 시작']);
  t.mutate();
  await t.settle(); await t.settle();
  assert.equal(a.buttons[0].clicks, 0);
  t = boot({ rollover: { ok: false } });
  const b = t.limitAlert(LIMIT, ['새 대화 시작']);
  await t.send({ type: 'badcat:activate', session: SESSION });
  await t.settle(); await t.settle();
  t.mutate(); await t.settle(); await t.settle();
  assert.equal(b.buttons[0].clicks, 0);
  assert.equal(t.runtimeCalls.filter((m) => m.type === 'badcat:rollover').length, 1); // no retry loop
});

test('stopping while the rollover is being announced cancels the click', async () => {
  const t = boot();
  const a = t.limitAlert(LIMIT, ['새 대화 시작']);
  await t.send({ type: 'badcat:activate', session: SESSION });
  await t.send({ type: 'badcat:cancel', session: SESSION });
  await t.settle(); await t.settle();
  assert.equal(a.buttons[0].clicks, 0);
  assert.equal(t.observers[0].disconnected, true);
});

test('a freshly loaded page rebinds through the hello handshake', async () => {
  const t = boot({ url: 'https://chatgpt.com/', hello: { ok: true, session: SESSION } });
  await t.settle();
  const res = await t.send({ type: 'badcat:handoff', pr: 123, session: SESSION, event: EVENT });
  assert.equal(res.ok, true);
  assert.equal(t.observers.length, 1);
});

const EVENT = { repo: 'o/r', pr: 123, head: 'a'.repeat(40), status: '+1', review_id: 1, delivery_id: 'c'.repeat(32) };
const handoffMsg = (extra = {}) => ({ type: 'badcat:handoff', pr: 123, session: SESSION, event: EVENT, ...extra });

test('new chat after rollover: a restored draft that is this very command is submitted once', async () => {
  const t = await activated({ url: 'https://chatgpt.com/', initial: TEXT });
  const res = await t.send(handoffMsg({ requireProject: true }));
  assert.equal(res.ok, true);
  assert.deepEqual(t.log.submitted, [TEXT]);
  assert.deepEqual(t.log.fills, []); // not typed a second time
});

test('new chat after rollover: an empty composer is filled and submitted', async () => {
  const t = await activated({ url: 'https://chatgpt.com/' });
  assert.equal((await t.send(handoffMsg())).ok, true);
  assert.deepEqual(t.log.submitted, [TEXT]);
});

test('new chat after rollover: unrelated or other-PR drafts are kept and nothing is sent', async () => {
  for (const initial of ['내 메모', '현재 작업 끝났으면 PR #586 리뷰해.']) {
    const t = await activated({ url: 'https://chatgpt.com/', initial });
    const res = await t.send(handoffMsg());
    assert.equal(res.ok, false);
    assert.match(res.error, /unrelated draft/);
    assert.equal(t.text(), initial);
    assert.deepEqual(t.log.submitted, []);
  }
});

test('new chat after rollover: a lost project binding stops the handoff; without a required project it is fine', async () => {
  let t = await activated({ url: 'https://chatgpt.com/', label: '' });
  const res = await t.send(handoffMsg({ requireProject: true }));
  assert.equal(res.ok, false);
  assert.match(res.error, /project/);
  assert.deepEqual(t.log.submitted, []);
  t = await activated({ url: 'https://chatgpt.com/', label: '' });
  assert.equal((await t.send(handoffMsg())).ok, true);
});

test('the new-chat route is rejected for a handoff that carries no session', async () => {
  const t = boot({ url: 'https://chatgpt.com/' });
  assert.match((await t.send({ type: 'badcat:handoff', pr: 1 })).error, /not a conversation/);
});

test('an already submitted event is not sent again after the rollover', async () => {
  const t = await activated();
  assert.equal((await t.send(handoffMsg())).ok, true);
  t.setUrl('https://chatgpt.com/');
  assert.equal((await t.send(handoffMsg())).ok, true);
  assert.equal(t.log.submitted.length, 1);
});

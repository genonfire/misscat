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

const pending = (t, extra = {}) => t.send({ type: 'badcat:handoff', pr: 123, session: SESSION, event: EVENT, ...extra });
const announced = (t) => t.runtimeCalls.filter((m) => m.type === 'badcat:rollover');
const clicks = (alert) => alert.buttons.map((b) => b.clicks);

test('a +1 handoff on a limited conversation announces the rollover, then clicks "새 대화 시작" once and types nothing', async () => {
  const t = await activated();
  const alert = t.limitAlert(LIMIT, ['새 대화 시작', '새 대화 시작']); // duplicated button as captured
  const res = await pending(t);
  assert.equal(res.ok, false);
  assert.equal(res.rollover, true);
  assert.equal(JSON.stringify(announced(t)), JSON.stringify([{ type: 'badcat:rollover', session: SESSION }]));
  assert.deepEqual(clicks(alert), [1, 0]);
  assert.deepEqual(t.log.fills, []);
  assert.deepEqual(t.log.keys, []);
  const again = await pending(t); // the retry while the old page is still shown: no second click
  assert.equal(again.rollover, true);
  assert.deepEqual(clicks(alert), [1, 0]);
  assert.equal(announced(t).length, 1);
});

test('an old limited conversation is left alone without a handoff: no observer, no announcement, no click', async () => {
  const t = boot();
  const alert = t.limitAlert('이 대화는 최대 깊이에 도달했습니다.', ['새 대화 시작']);
  await t.send({ type: 'badcat:activate', session: SESSION });
  t.mutate();
  await t.settle(); await t.settle();
  assert.equal(t.observers.length, 0);
  assert.deepEqual(clicks(alert), [0]);
  assert.equal(announced(t).length, 0);
  const res = await pending(t); // "최대 깊이" wording is recognized once an event is pending
  assert.equal(res.rollover, true);
  assert.deepEqual(clicks(alert), [1]);
});

test('English wording is recognized too', async () => {
  const t = await activated();
  const alert = t.limitAlert('You have reached the maximum length for this conversation.', ['Start a new chat']);
  assert.equal((await pending(t)).rollover, true);
  assert.deepEqual(clicks(alert), [1]);
});

test('no alert: the handoff is delivered normally in the existing conversation', async () => {
  const t = await activated();
  assert.equal((await pending(t)).ok, true);
  assert.equal(announced(t).length, 0);
  assert.deepEqual(t.log.submitted, [TEXT]);
});

test('false-positive alerts or a limit text without the new-chat button are never clicked; delivery proceeds', async () => {
  const t = await activated();
  const other = t.limitAlert('네트워크 오류가 발생했습니다.', ['새 대화 시작']);
  const noButton = t.limitAlert(LIMIT, ['닫기']);
  assert.equal((await pending(t)).ok, true);
  assert.deepEqual([...clicks(other), ...clicks(noButton)], [0, 0]);
  assert.equal(announced(t).length, 0);
});

test('a refusal by the background leaves the alert alone', async () => {
  const t = await activated({ rollover: { ok: false } });
  const alert = t.limitAlert(LIMIT, ['새 대화 시작']);
  await pending(t);
  assert.deepEqual(clicks(alert), [0]);
  assert.equal(announced(t).length, 1);
});

test('stopping while the rollover is being announced cancels the click', async () => {
  let t;
  t = await activated({ rollover: async () => { await t.send({ type: 'badcat:cancel', session: SESSION }); return { ok: true }; } });
  const alert = t.limitAlert(LIMIT, ['새 대화 시작']);
  const res = await pending(t);
  assert.equal(res.rollover, undefined);
  assert.deepEqual(clicks(alert), [0]);
  assert.deepEqual(t.log.submitted, []);
});

test('a limit alert that appears in response to a failed send is caught by one bounded recheck', async () => {
  const t = await activated({ enterSubmits: false, sendButton: false, alertOnEnter: { text: LIMIT, labels: ['새 대화 시작'] } });
  const res = await pending(t);
  assert.equal(res.rollover, true);
  assert.deepEqual(clicks(t.ctx.alert), [1]);
  assert.equal(announced(t).length, 1);
});

test('a failed send without any alert reports its own error and does not roll over', async () => {
  const t = await activated({ enterSubmits: false, sendButton: false });
  const res = await pending(t);
  assert.equal(res.ok, false);
  assert.equal(res.rollover, undefined);
  assert.equal(announced(t).length, 0);
});

test('a handoff without a session never rolls over', async () => {
  const t = boot();
  const alert = t.limitAlert(LIMIT, ['새 대화 시작']);
  await t.send({ type: 'badcat:handoff', pr: 123 });
  assert.deepEqual(clicks(alert), [0]);
  assert.equal(announced(t).length, 0);
});

test('a freshly loaded page rebinds through the hello handshake', async () => {
  const t = boot({ url: 'https://chatgpt.com/', hello: { ok: true, session: SESSION } });
  await t.settle();
  const res = await t.send({ type: 'badcat:handoff', pr: 123, session: SESSION, event: EVENT });
  assert.equal(res.ok, true);
});

const EVENT = { repo: 'o/r', pr: 123, head: 'a'.repeat(40), status: '+1', review_id: 1, delivery_id: 'c'.repeat(32) };
const handoffMsg = (extra = {}) => ({ type: 'badcat:handoff', pr: 123, session: SESSION, event: EVENT, ...extra });

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

const SLUG = 'g-p-6acc5689a7f48191b27144cee2289958';
const MENU = [{ text: 'Other', value: 'g-p-999-other' }, { text: 'Code Review', value: 'g-p-6acc5689a7f48191b27144cee2289958' }];
const projectOpts = (extra = {}) => ({ url: 'https://chatgpt.com/', selector: 'Code Review', menu: MENU, ...extra });

test('project rollover: the selected project is verified by ID, then the restored command is sent exactly once', async () => {
  const t = await activated(projectOpts({ initial: TEXT }));
  assert.equal((await t.send(handoffMsg({ project: SLUG }))).ok, true);
  assert.deepEqual(t.log.submitted, [TEXT]);
  assert.deepEqual(t.log.fills, []);
  assert.equal(t.selectorButton.attrs['aria-expanded'], 'false'); // the menu was closed again
  assert.equal((await t.send(handoffMsg({ project: SLUG }))).ok, true); // replay: deduped
  assert.deepEqual(t.log.submitted, [TEXT]);
});

test('project rollover: an empty composer is filled after verification', async () => {
  const t = await activated(projectOpts());
  assert.equal((await t.send(handoffMsg({ project: SLUG }))).ok, true);
  assert.deepEqual(t.log.submitted, [TEXT]);
});

test('project rollover: a same-named project with another ID, a duplicate name, an overlapping name or no selector stops before typing', async () => {
  const cases = {
    'ID with a suffix': projectOpts({ menu: [{ text: 'Code Review', value: 'g-p-6acc5689a7f48191b27144cee2289958-code-review' }] }),
    'ID that is only a prefix': projectOpts({ menu: [{ text: 'Code Review', value: 'g-p-6acc5689' }] }),
    'another ID': projectOpts({ menu: [{ text: 'Code Review', value: 'g-p-777-code-review' }] }),
    'duplicate name': projectOpts({ menu: [...MENU, { text: 'Code Review', value: 'g-p-777' }] }),
    'overlapping name': projectOpts({ selector: 'Code Review Archive', menu: [{ text: 'Code Review Archive', value: 'g-p-777' }, ...MENU] }),
    'name not in the menu': projectOpts({ selector: 'Code Review Archive' }),
    'plain chat (no selector)': { url: 'https://chatgpt.com/' },
    'empty menu': projectOpts({ menu: [] }),
    'archive prefix is no match': projectOpts({ selector: 'Archive Code Review', menu: [{ text: 'Code Review', value: 'g-p-6acc5689a7f48191b27144cee2289958' }] }),
    'no selected name': projectOpts({ selector: '' }),
  };
  for (const [name, opts] of Object.entries(cases)) {
    const t = await activated({ ...opts, initial: TEXT });
    const res = await t.send(handoffMsg({ project: SLUG }));
    assert.equal(res.ok, false, name);
    assert.match(res.error, /cannot verify/, name);
    assert.equal(t.text(), TEXT, name);
    assert.deepEqual(t.log.keys, [], name);
    assert.deepEqual(t.log.submitted, [], name);
    if (t.selectorButton) assert.equal(t.selectorButton.attrs['aria-expanded'], 'false', name);
  }
});

test('project rollover: a project switch right before sending aborts; the command stays pending, nothing is submitted', async () => {
  const t = await activated(projectOpts({ renameOnFill: 'Other' }));
  const res = await t.send(handoffMsg({ project: SLUG }));
  assert.equal(res.ok, false);
  assert.match(res.error, /project changed/);
  assert.deepEqual(t.log.submitted, []);
  assert.deepEqual(t.log.keys, []);
});

test('project rollover: an unrelated draft is still kept untouched and not sent', async () => {
  const t = await activated(projectOpts({ initial: '내 메모' }));
  const res = await t.send(handoffMsg({ project: SLUG }));
  assert.equal(res.ok, false);
  assert.equal(t.text(), '내 메모');
  assert.deepEqual(t.log.submitted, []);
});

test('plain conversation rollover needs no project verification', async () => {
  const t = await activated({ url: 'https://chatgpt.com/' });
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

test('project rollover: a draft restored while the picker is open is kept, nothing is typed or sent; the retry sends the pending command once', async () => {
  let t = await activated(projectOpts({ restoreOnMenuOpen: '내 메모' }));
  let res = await t.send(handoffMsg({ project: SLUG }));
  assert.equal(res.ok, false);
  assert.match(res.error, /changed during verification/);
  assert.equal(t.text(), '내 메모');
  assert.deepEqual(t.log.keys, []);
  assert.deepEqual(t.log.fills, []);
  assert.deepEqual(t.log.submitted, []);
  // the exact command restored late: also not typed over; the retry (which reads it up front) submits it once
  t = await activated(projectOpts({ restoreOnMenuOpen: TEXT }));
  res = await t.send(handoffMsg({ project: SLUG }));
  assert.equal(res.ok, false);
  assert.deepEqual(t.log.fills, []);
  assert.deepEqual(t.log.submitted, []);
  assert.equal(t.text(), TEXT);
  res = await t.send(handoffMsg({ project: SLUG }));
  assert.equal(res.ok, true);
  assert.deepEqual(t.log.submitted, [TEXT]);
});

test('a conversation of another project is rejected right before sending when a project is required', async () => {
  const t = await activated({ url: 'https://chatgpt.com/g/g-p-2-other/c/new' });
  const res = await t.send(handoffMsg({ project: SLUG }));
  assert.equal(res.ok, false);
  assert.match(res.error, /original project/);
  assert.deepEqual(t.log.keys, []);
  const same = await activated({ url: 'https://chatgpt.com/g/' + SLUG + '/c/new' });
  assert.equal((await same.send(handoffMsg({ project: SLUG }))).ok, true);
});

test('moving to another chat while the project picker is open: nothing is typed or sent, even though composer and label still look right', async () => {
  for (const url of ['https://chatgpt.com/g/g-p-2-other/c/new', 'https://chatgpt.com/c/elsewhere', 'https://chatgpt.com/g/' + SLUG + '/c/new']) {
    const t = await activated(projectOpts({ navigateOnMenuOpen: url }));
    const res = await t.send(handoffMsg({ project: SLUG }));
    assert.equal(res.ok, false, url);
    assert.match(res.error, /page changed/, url);
    assert.deepEqual(t.log.keys, [], url);
    assert.deepEqual(t.log.fills, [], url);
    assert.deepEqual(t.log.submitted, [], url);
  }
});

test('moving to another conversation while waiting for the composer: nothing is typed or sent', async () => {
  const start = 'https://chatgpt.com/g/' + SLUG + '/c/old';
  const t = await activated({ url: start, lateComposer: true,
    onTimer: (ms, ctx) => { ctx.url = 'https://chatgpt.com/g/g-p-2-other/c/new'; } });
  const res = await t.send(handoffMsg({ project: SLUG }));
  assert.equal(res.ok, false);
  assert.match(res.error, /page changed/);
  assert.deepEqual(t.log.keys, []);
  assert.deepEqual(t.log.fills, []);
  // without the move, the late composer is used normally
  const ok = await activated({ url: start, lateComposer: true });
  assert.equal((await ok.send(handoffMsg({ project: SLUG }))).ok, true);
  assert.deepEqual(ok.log.submitted, [TEXT]);
});

test('moving during the new-chat settle wait: nothing is typed or sent', async () => {
  const t = await activated(projectOpts({ onTimer: (ms, ctx) => { if (ms === 800) ctx.url = 'https://chatgpt.com/c/elsewhere'; } }));
  const res = await t.send(handoffMsg({ project: SLUG }));
  assert.equal(res.ok, false);
  assert.match(res.error, /page changed/);
  assert.deepEqual(t.log.keys, []);
  assert.deepEqual(t.log.submitted, []);
});

// --- a successful first send that moves the page and re-mounts the composer ---
test('new chat: Enter submits, the URL moves and the editor is replaced (old node keeps its text): confirmed by the rendered message, sent once', async () => {
  const t = await activated(projectOpts({ afterSubmit: { url: 'https://chatgpt.com/g/' + SLUG + '/c/new-id', render: true } }));
  const res = await t.send(handoffMsg({ project: SLUG }));
  assert.equal(res.ok, true);
  assert.deepEqual(t.log.submitted, [TEXT]);
  assert.equal((await t.send(handoffMsg({ project: SLUG }))).ok, true); // replay: acknowledged without resending
  assert.deepEqual(t.log.submitted, [TEXT]);
  assert.equal(t.log.clicks, 0);
});

test('the URL moved but no such message appeared: unconfirmed, no ACK path, and never sent again', async () => {
  const t = await activated(projectOpts({ afterSubmit: { url: 'https://chatgpt.com/g/' + SLUG + '/c/new-id', render: false } }));
  const res = await t.send(handoffMsg({ project: SLUG }));
  assert.equal(res.ok, false);
  assert.equal(res.ambiguous, true);
  const again = await t.send(handoffMsg({ project: SLUG })); // the retry / host replay
  assert.equal(again.ok, false);
  assert.equal(again.ambiguous, true);
  assert.deepEqual(t.log.submitted, [TEXT]); // typed and sent exactly once
  assert.equal(t.log.clicks, 0);
});

test('the message appeared but in a conversation of another project: not confirmed', async () => {
  const t = await activated(projectOpts({ afterSubmit: { url: 'https://chatgpt.com/g/g-p-2-other/c/x', render: true } }));
  const res = await t.send(handoffMsg({ project: SLUG }));
  assert.equal(res.ok, false);
  assert.equal(res.ambiguous, true);
});

test('a different event after an unconfirmed one is not blocked by it', async () => {
  const t = await activated(projectOpts({ afterSubmit: { url: 'https://chatgpt.com/g/' + SLUG + '/c/n', render: false } }));
  await t.send(handoffMsg({ project: SLUG }));
  const other = await t.send({ ...handoffMsg({ project: SLUG }), event: { ...EVENT, review_id: 2 } });
  assert.notEqual(other.error, UNCERTAIN_TEXT);
});
const UNCERTAIN_TEXT = 'submission could not be confirmed; not sent again';

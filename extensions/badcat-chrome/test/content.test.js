// Runs content.js against a minimal reproduction of ChatGPT's composer DOM (no jsdom: nothing to
// install). The tiny selector engine below evaluates the real selectors from content.js against that
// tree, so if a selector stops matching the real structure these tests fail.
//
//   <form>                                   <- composer container
//     <div>
//       <div contenteditable="true" data-composer-markdown class="ProseMirror"><p>…</p></div>
//     </div>
//     <button type="button">  (attach / voice / stop: never the send button)
//     <button type="submit">  (send)
//   </form>
//   plus decoys elsewhere: another contenteditable, another form's submit button, a legacy #prompt-textarea.
const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const GOOD_URL = 'https://chatgpt.com/c/abc-123';
const TEXT = '현재 작업 끝났으면 PR#123 리뷰해.';

// --- minimal DOM: elements with attributes, parent links, and selectors of the form
// "a, b" where each part is `tag?[attr]*` / `[attr="value"]` (no combinators, no classes, no ids).
function parseSelector(selector) {
  return selector.split(',').map((part) => {
    const m = part.trim().match(/^([a-z]*)((?:\[[a-z-]+(?:="[^"]*")?\])*)$/);
    if (!m || !part.trim()) throw new Error('fixture selector engine does not support: ' + selector);
    const attrs = [...m[2].matchAll(/\[([a-z-]+)(?:="([^"]*)")?\]/g)].map((a) => [a[1], a[2]]);
    return { tag: m[1], attrs };
  });
}

function node(tag, attrs = {}, children = []) {
  const el = {
    tagName: tag.toUpperCase(), attrs, children, parentElement: null, disabled: false,
    matches(selector) {
      return parseSelector(selector).some(({ tag: t, attrs: wanted }) =>
        (!t || el.tagName === t.toUpperCase()) &&
        wanted.every(([k, v]) => k in el.attrs && (v === undefined || el.attrs[k] === v)));
    },
    closest(selector) {
      for (let n = el; n; n = n.parentElement) if (n.matches(selector)) return n;
      return null;
    },
    querySelector(selector) {
      for (const child of el.children) {
        if (child.matches(selector)) return child;
        const deeper = child.querySelector(selector);
        if (deeper) return deeper;
      }
      return null;
    },
  };
  children.forEach((c) => { c.parentElement = el; });
  return el;
}

function boot({ url = GOOD_URL, composer = true, enterSubmits = true, buttonSubmits = false, sendButton = true, initial = '' } = {}) {
  let now = 0; // virtual clock: waits finish instantly
  let enterSubmitsNow = enterSubmits;
  const log = { submitted: [], keys: [], clicks: 0, decoyClicks: 0, fills: [] };

  const editor = node('div', { contenteditable: 'true', 'data-composer-markdown': '', class: 'ProseMirror' });
  Object.assign(editor, {
    textContent: initial, selected: false, collapsed: false,
    focus() {},
    dispatchEvent(ev) {
      log.keys.push(ev.type + ':' + ev.key);
      if (ev.type === 'keydown' && ev.key === 'Enter' && enterSubmitsNow) submit();
      return true;
    },
  });
  const text = () => editor.textContent;
  const submit = () => { log.submitted.push(text()); editor.textContent = ''; };

  const send = node('button', { type: 'submit', 'data-testid': 'whatever-it-is-now' });
  send.click = () => { log.clicks += 1; if (buttonSubmits) submit(); };
  const stopOrVoice = node('button', { type: 'button' });
  stopOrVoice.click = () => { log.decoyClicks += 1; };
  const otherSubmit = node('button', { type: 'submit' }); // e.g. a search form elsewhere on the page
  otherSubmit.click = () => { log.decoyClicks += 1; };
  const otherEditable = node('div', { contenteditable: 'true' }); // contenteditable without data-composer-markdown
  const legacy = node('textarea', { id: 'prompt-textarea' }); // the old selector must no longer be relied on
  legacy.click = () => {};

  const form = node('form', {}, [
    node('div', {}, composer ? [editor] : [otherEditable]),
    stopOrVoice,
    ...(sendButton ? [send] : []),
  ]);
  const page = node('body', {}, [node('form', {}, [otherSubmit]), legacy, ...(composer ? [otherEditable] : []), form]);

  const document = {
    querySelector: (sel) => page.querySelector(sel),
    execCommand(cmd, _ui, value) {
      assert.equal(cmd, 'insertText');
      editor.textContent = editor.collapsed ? editor.textContent + value : value; // collapsed to the end = append
      log.fills.push(value);
      return true;
    },
  };
  const listeners = [];
  const context = {
    document, location: { href: url },
    window: { getSelection: () => ({
      selectAllChildren() { editor.selected = true; editor.collapsed = false; },
      collapseToEnd() { editor.collapsed = true; },
    }) },
    KeyboardEvent: class { constructor(type, init) { this.type = type; Object.assign(this, init); } },
    chrome: { runtime: { id: 'me', onMessage: { addListener: (fn) => listeners.push(fn) } } },
    setTimeout: (fn, ms) => { now += ms; setImmediate(fn); },
    Date: { now: () => now },
    console,
  };
  context.self = { BadCatShared: require('../shared.js') };
  vm.createContext(context);
  vm.runInContext(fs.readFileSync(path.join(__dirname, '..', 'content.js'), 'utf8'), context);

  const sendMessage = (message, sender = { id: 'me' }) => new Promise((resolve) => {
    const keep = listeners[0](message, sender, resolve);
    if (!keep) resolve(undefined);
  });
  return { log, send: sendMessage, text, setEnterSubmits: (v) => { enterSubmitsNow = v; } };
}

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

test('puts exactly "현재 작업 끝났으면 PR#123 리뷰해." in the composer and submits it with Enter', async () => {
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
  const t = boot({ initial: '현재 작업 끝났으면 PR#365 리뷰해.', enterSubmits: false });
  t.setEnterSubmits(true);
  const res = await t.send({ type: 'badcat:handoff', pr: 366 });
  assert.equal(res.ok, true);
  assert.deepEqual(t.log.submitted, ['현재 작업 끝났으면 PR#365 리뷰해. 현재 작업 끝났으면 PR#366 리뷰해.']);
});

test('an empty composer gets just the command', async () => {
  const t = boot({ initial: '   ' });
  assert.equal((await t.send({ type: 'badcat:handoff', pr: 365 })).ok, true);
  assert.deepEqual(t.log.submitted, ['현재 작업 끝났으면 PR#365 리뷰해.']);
});

test('a retry or duplicate event does not append the same handoff twice', async () => {
  const t = boot({ enterSubmits: false });
  await t.send({ type: 'badcat:handoff', pr: 5 });
  await t.send({ type: 'badcat:handoff', pr: 5 });
  assert.equal(t.text(), '현재 작업 끝났으면 PR#5 리뷰해.');
  await t.send({ type: 'badcat:handoff', pr: 6 });
  assert.equal(t.text(), '현재 작업 끝났으면 PR#5 리뷰해. 현재 작업 끝났으면 PR#6 리뷰해.');
  await t.send({ type: 'badcat:handoff', pr: 6 });
  await t.send({ type: 'badcat:handoff', pr: 5 });
  assert.equal(t.text(), '현재 작업 끝났으면 PR#5 리뷰해. 현재 작업 끝났으면 PR#6 리뷰해.');
  t.setEnterSubmits(true);
  assert.equal((await t.send({ type: 'badcat:handoff', pr: 5 })).ok, true);
  assert.deepEqual(t.log.submitted, ['현재 작업 끝났으면 PR#5 리뷰해. 현재 작업 끝났으면 PR#6 리뷰해.']);
});

test('PR #5 is not mistaken for already pending when only PR #15 is', async () => {
  const t = boot({ initial: '현재 작업 끝났으면 PR#15 리뷰해.', enterSubmits: false });
  await t.send({ type: 'badcat:handoff', pr: 5 });
  assert.equal(t.text(), '현재 작업 끝났으면 PR#15 리뷰해. 현재 작업 끝났으면 PR#5 리뷰해.');
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

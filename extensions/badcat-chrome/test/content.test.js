// Runs content.js against a small fake DOM (no jsdom: nothing to install). Covers the handoff flow:
// fill the composer, press Enter, verify it was submitted, fall back to the send button, fail safely.
const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const GOOD_URL = 'https://chatgpt.com/c/abc-123';
const TEXT = 'PR #123 리뷰해';

function boot({ url = GOOD_URL, composer = true, enterSubmits = true, buttonSubmits = false, tag = 'DIV' } = {}) {
  let now = 0; // virtual clock: waits finish instantly
  const log = { submitted: [], keys: [], clicks: 0, fills: [] };

  const textareaProto = {
    get value() { return this._v; },
    set value(v) { this._v = v; },
  };
  const el = Object.assign(tag === 'TEXTAREA' ? Object.create(textareaProto) : {}, {
    tagName: tag, textContent: '', selected: false, _v: '',
    focus() {},
    dispatchEvent(ev) {
      if (ev.type === 'input') return true;
      log.keys.push(ev.type + ':' + ev.key);
      if (ev.type === 'keydown' && ev.key === 'Enter' && enterSubmits) submit();
      return true;
    },
  });
  if (tag !== 'TEXTAREA') Object.defineProperty(el, 'value', { value: undefined, writable: true });
  const text = () => (tag === 'TEXTAREA' ? el.value : el.textContent);
  const submit = () => {
    log.submitted.push(text());
    if (tag === 'TEXTAREA') el.value = ''; else el.textContent = '';
  };
  const button = {
    disabled: false,
    click() { log.clicks += 1; if (buttonSubmits) submit(); },
  };

  const document = {
    querySelector(sel) {
      if (sel === '#prompt-textarea') return composer ? el : null;
      if (sel === 'button[data-testid="send-button"]') return button;
      return null;
    },
    execCommand(cmd, _ui, value) {
      assert.equal(cmd, 'insertText');
      el.textContent = value; // the composer was selected first, so this replaces its content
      log.fills.push(value);
      return true;
    },
  };
  const listeners = [];
  const context = {
    document, location: { href: url },
    window: { getSelection: () => ({ selectAllChildren() { el.selected = true; } }) },
    KeyboardEvent: class { constructor(type, init) { this.type = type; Object.assign(this, init); } },
    Event: class { constructor(type) { this.type = type; } },
    HTMLTextAreaElement: { prototype: textareaProto },
    chrome: { runtime: { id: 'me', onMessage: { addListener: (fn) => listeners.push(fn) } } },
    setTimeout: (fn, ms) => { now += ms; setImmediate(fn); },
    Date: { now: () => now },
    console,
  };
  context.self = { BadCatShared: require('../shared.js') };
  vm.createContext(context);
  vm.runInContext(fs.readFileSync(path.join(__dirname, '..', 'content.js'), 'utf8'), context);

  const send = (message, sender = { id: 'me' }) => new Promise((resolve) => {
    const keep = listeners[0](message, sender, resolve);
    if (!keep) resolve(undefined);
  });
  return { el, log, send, text };
}

test('ping answers only the extension itself', async () => {
  const t = boot();
  assert.equal((await t.send({ type: 'badcat:ping' })).ok, true);
  assert.equal(await t.send({ type: 'badcat:ping' }, { id: 'other' }), undefined);
  assert.equal(await t.send({ type: 'unknown' }), undefined);
});

test('puts exactly "PR #123 리뷰해" in the composer and submits it with Enter', async () => {
  const t = boot();
  const res = await t.send({ type: 'badcat:handoff', pr: 123 });
  assert.equal(res.ok, true);
  assert.deepEqual(t.log.submitted, [TEXT]);
  assert.deepEqual(t.log.keys, ['keydown:Enter', 'keyup:Enter']);
  assert.equal(t.log.clicks, 0); // Enter was enough; the button is only a fallback
});

test('works with a plain textarea composer too', async () => {
  const t = boot({ tag: 'TEXTAREA' });
  const res = await t.send({ type: 'badcat:handoff', pr: 7 });
  assert.equal(res.ok, true);
  assert.deepEqual(t.log.submitted, ['PR #7 리뷰해']);
});

test('falls back to the send button when Enter does not submit', async () => {
  const t = boot({ enterSubmits: false, buttonSubmits: true });
  const res = await t.send({ type: 'badcat:handoff', pr: 123 });
  assert.equal(res.ok, true);
  assert.equal(t.log.clicks, 1);
  assert.deepEqual(t.log.submitted, [TEXT]);
});

test('fails (and leaves no stray draft) when nothing submits the message', async () => {
  const t = boot({ enterSubmits: false, buttonSubmits: false });
  const res = await t.send({ type: 'badcat:handoff', pr: 123 });
  assert.equal(res.ok, false);
  assert.equal(t.text(), '');
  assert.deepEqual(t.log.submitted, []);
});

test('a retry replaces the composer text instead of duplicating it', async () => {
  const t = boot({ enterSubmits: false });
  await t.send({ type: 'badcat:handoff', pr: 5 });
  await t.send({ type: 'badcat:handoff', pr: 5 });
  // each attempt fills the composer once (the '' entries are the stray-draft cleanup)
  assert.deepEqual(t.log.fills.filter(Boolean), ['PR #5 리뷰해', 'PR #5 리뷰해']);
  assert.equal(t.text(), '');
});

test('fails when the composer cannot be found or the page is not a conversation', async () => {
  let t = boot({ composer: false });
  assert.match((await t.send({ type: 'badcat:handoff', pr: 1 })).error, /composer not found/);
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

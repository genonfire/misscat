// nastycat-content.js against a small fake DOM: link scan and the single-submit delivery protocol.
const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

function boot({ pendingMs = 0, shows = true, href = 'https://claude.ai/code/session_A', draft = '', anchors = [], sendAppears = true, consumeOnClick = true, insertWorks = true } = {}) {
  const messages = [];
  const state = { messages, text: draft, sendVisible: false, clicks: 0, href, deleted: 0 };
  const editor = {
    focus() {},
    contains() { return false; },
    get textContent() { return state.text; },
  };
  const button = {
    disabled: false,
    getAttribute() { return null; },
    click() {
      state.clicks++;
      if (!consumeOnClick) return;
      const sent = state.text;
      state.text = ''; state.sendVisible = false;
      if (!shows) return;
      const parent = { textContent: sent + ' Pending', parentElement: null };
      const node = { children: [], textContent: sent, parentElement: parent };
      messages.push(node);
      setTimeout(() => { parent.textContent = sent; }, pendingMs);
    },
  };
  const document = {
    querySelector(sel) {
      if (sel === '[data-testid="code-prompt-input"]') return editor;
      if (sel === '[data-testid="code-prompt-send"]') return state.sendVisible ? button : null;
      return null;
    },
    querySelectorAll(sel) { return sel === '*' ? messages : anchors.map((h) => ({ href: h })); },
    execCommand(cmd, _ui, value) {
      if (cmd === 'insertText') {
        if (!insertWorks) return false;
        state.text += value;
        if (sendAppears) state.sendVisible = true;
        return true;
      }
      if (cmd === 'delete') { state.text = ''; state.sendVisible = false; state.deleted++; return true; }
      return false;
    },
    createRange() { return { selectNodeContents() {} }; },
  };
  const listeners = [];
  const root = {
    document, location: { get href() { return state.href; } },
    getSelection: () => ({ removeAllRanges() {}, addRange() {} }),
    chrome: { runtime: { id: 'me', onMessage: { addListener: (fn) => listeners.push(fn) } } },
    setTimeout, NastyCatShared: require('../nastycat-shared.js'),
  };
  root.self = root;
  vm.createContext(root);
  vm.runInContext(fs.readFileSync(path.join(__dirname, '..', 'nastycat-content.js'), 'utf8'), root);
  const api = root.NastyCatContent;
  Object.assign(api.timing, { settleMs: 15, buttonWaitMs: 60, confirmMs: 60, pollMs: 5 });
  const msg = (message, sender = { id: 'me' }) => new Promise((resolve) => {
    const keep = listeners[0](message, sender, resolve);
    if (!keep) resolve(undefined);
  });
  return { api, state, msg, root };
}

const deliver = (id = 'd1', conv = 'session_A', prompt = '#860 A 진행해') => ({ type: 'nastycat:deliver', conversationId: conv, deliveryId: id, prompt });

test('scan returns the conversation identity and every valid PR link, nothing else', async () => {
  const t = boot({ anchors: ['https://github.com/o/r/pull/954', 'https://github.com/o/r/pull/957', 'https://example.com/', 'https://github.com/o/r/issues/3'] });
  const scan = await t.msg({ type: 'nastycat:scan' });
  assert.equal(scan.conversationId, 'session_A');
  assert.equal(scan.links.length, 2);
});

test('delivery inserts the exact prompt, waits for the send button, clicks once and confirms', async () => {
  const t = boot();
  const out = await t.msg(deliver());
  assert.equal(out.phase, 'confirmed');
  assert.equal(t.state.clicks, 1);
});

test('the same delivery id is never submitted twice', async () => {
  const t = boot();
  await t.msg(deliver('same'));
  const again = await t.msg(deliver('same'));
  assert.equal(again.phase, 'uncertain');
  assert.equal(t.state.clicks, 1);
});

test('a foreign draft is never overwritten', async () => {
  const t = boot({ draft: 'half-typed note' });
  const out = await t.msg(deliver());
  assert.deepEqual([out.phase, out.reason], ['not_sent', 'busy-editor']);
  assert.equal(t.state.text, 'half-typed note');
  assert.equal(t.state.clicks, 0);
});

test('a missing send button cleans up what it inserted and sends nothing', async () => {
  const t = boot({ sendAppears: false });
  const out = await t.msg(deliver());
  assert.deepEqual([out.phase, out.reason], ['not_sent', 'no-send-button']);
  assert.equal(t.state.text, '');
  assert.equal(t.state.clicks, 0);
});

test('a failed insertion is not_sent', async () => {
  const t = boot({ insertWorks: false });
  assert.equal((await t.msg(deliver())).reason, 'insert-failed');
});

test('Pending then success is confirmed only once the message is no longer Pending', async () => {
  const t = boot({ pendingMs: 40 });
  const started = Date.now();
  const out = await t.msg(deliver());
  assert.equal(out.phase, 'confirmed');
  assert.ok(Date.now() - started >= 35);
  assert.equal(t.state.clicks, 1);
});

test('Pending that never resolves, or no transcript message at all, is uncertain', async () => {
  const stuck = boot({ pendingMs: 300 });
  assert.equal((await stuck.msg(deliver())).phase, 'uncertain');
  const none = boot({ shows: false });
  assert.equal((await none.msg(deliver())).phase, 'uncertain');
  assert.equal(none.state.clicks, 1);
});

test('an identical earlier message does not count as confirmation', async () => {
  const t = boot({ shows: false });
  t.state.messages.push({ children: [], textContent: '#860 A 진행해', parentElement: null });
  assert.equal((await t.msg(deliver())).phase, 'uncertain');
});

test('a delivery cancelled before it was clicked is withdrawn and cleaned up', async () => {
  const t = boot();
  await t.msg({ type: 'nastycat:cancel', deliveryId: 'gone' });
  const early = await t.msg(deliver('gone'));
  assert.deepEqual([early.phase, early.reason], ['not_sent', 'cancelled']);
  const racing = t.msg(deliver('late'));
  await new Promise((r) => setTimeout(r, 3)); // inserted, waiting for the settle delay
  await t.msg({ type: 'nastycat:cancel', deliveryId: 'late' });
  const out = await racing;
  assert.deepEqual([out.phase, out.reason], ['not_sent', 'cancelled']);
  assert.equal(t.state.clicks, 0);
  assert.equal(t.state.text, '');
});

test('a click whose result never shows is uncertain, not retried', async () => {
  const t = boot({ consumeOnClick: false });
  const out = await t.msg(deliver());
  assert.deepEqual([out.phase, out.reason], ['uncertain', 'unconfirmed']);
  assert.equal(t.state.clicks, 1);
});

test('a delivery for another conversation, or after navigation, is refused', async () => {
  const t = boot();
  assert.equal((await t.msg(deliver('d1', 'session_B'))).reason, 'wrong-conversation');
  t.state.href = 'https://claude.ai/code/session_Z';
  assert.equal((await t.msg(deliver('d2'))).reason, 'wrong-conversation');
  assert.equal(t.state.clicks, 0);
});

test('only extension senders and nastycat messages are handled', async () => {
  const t = boot();
  assert.equal(await t.msg({ type: 'nastycat:ping' }, { id: 'evil' }), undefined);
  assert.equal(await t.msg({ type: 'badcat:ping' }), undefined);
  assert.equal((await t.msg({ type: 'nastycat:ping' })).ok, true);
});

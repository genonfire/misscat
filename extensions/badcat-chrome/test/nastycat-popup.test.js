// Popup routing: a Claude tab gets NastyCat (same layout, new text, accent class); ChatGPT keeps MissCat.
const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

async function boot({ url, session = { active: false }, prompt = 'last prompt' }) {
  const sent = [];
  const handlers = {};
  const classes = new Set();
  const el = (extra = {}) => { const o = { textContent: '', value: '', disabled: false, hidden: false, readOnly: false, addEventListener(t, fn) { (o.h = o.h || {})[t] = fn; }, ...extra }; return o; };
  const elements = { repo: el(), toggle: el(), 'status-text': el(), error: el(), note: el(), 'copy-log': el() };
  const title = { textContent: 'MissCat' };
  const label = { textContent: 'Repository' };
  const chrome = {
    runtime: {
      async sendMessage(m) {
        sent.push(m);
        if (m.type === 'nastycat:status') return { ok: true, session, prompt };
        if (m.type === 'badcat:status') return { ok: true, session: { active: false } };
        return { ok: true };
      },
    },
    storage: { local: { async get() { return {}; }, async set() {} }, onChanged: { addListener() {} } },
    tabs: { async query() { return [{ id: 7, url }]; } },
  };
  const context = {
    document: {
      getElementById: (id) => elements[id],
      querySelector: (sel) => (sel === '.title' ? title : sel === 'label[for="repo"]' ? label : null),
      body: { classList: { add: (c) => classes.add(c), toggle: (c, on) => (on ? classes.add(c) : classes.delete(c)) } },
      title: 'MissCat',
    },
    chrome, navigator: { clipboard: { async writeText() {} } },
    self: { BadCatShared: require('../shared.js'), NastyCatShared: require('../nastycat-shared.js') },
  };
  vm.createContext(context);
  vm.runInContext(fs.readFileSync(path.join(__dirname, '..', 'nastycat-popup.js'), 'utf8'), context);
  context.self.NastyCatPopup = context.self.NastyCatPopup || {};
  vm.runInContext(fs.readFileSync(path.join(__dirname, '..', 'popup.js'), 'utf8'), context);
  await new Promise((r) => setTimeout(r, 20));
  return { elements, sent, handlers, classes, title, label, context };
}

test('a Claude tab shows NastyCat: heading, Prompt label, accent, button text, last prompt', async () => {
  const t = await boot({ url: 'https://claude.ai/code/session_A' });
  assert.equal(t.title.textContent, 'NastyCat');
  assert.equal(t.label.textContent, 'Prompt');
  assert.ok(t.classes.has('nasty'));
  assert.equal(t.elements.toggle.textContent, 'No napping, Meow!');
  assert.equal(t.elements['status-text'].textContent, 'Sleeping');
  assert.equal(t.elements.repo.value, 'last prompt');
  assert.ok(t.sent.every((m) => m.type.startsWith('nastycat:')));
});

test('an armed Claude tab is read-only with the Nap button; start sends the prompt verbatim with the tab id', async () => {
  const armed = await boot({ url: 'https://claude.ai/code/session_A', session: { active: true, prompt: '#860 A 진행해', note: 'Watching 1 open PR.' } });
  assert.equal(armed.elements.toggle.textContent, 'Nap time, Meow!');
  assert.equal(armed.elements.repo.readOnly, true);
  assert.equal(armed.elements.repo.value, '#860 A 진행해');
  const idle = await boot({ url: 'https://claude.ai/code/session_A' });
  idle.elements.repo.value = '  #860 B 진행해';
  await idle.elements.toggle.h.click();
  assert.deepEqual(JSON.parse(JSON.stringify(idle.sent.find((m) => m.type === 'nastycat:start'))), { type: 'nastycat:start', prompt: '  #860 B 진행해', tabId: 7 });
});

test('a ChatGPT tab is the unchanged MissCat popup', async () => {
  const t = await boot({ url: 'https://chatgpt.com/c/abc' });
  assert.equal(t.title.textContent, 'MissCat');
  assert.ok(!t.classes.has('nasty'));
  assert.equal(t.elements['status-text'].textContent, 'Sleeping');
  assert.equal(t.elements.toggle.textContent, "Catch 'em all, Meow!");
  assert.ok(t.sent.some((m) => m.type === 'badcat:status'));
});

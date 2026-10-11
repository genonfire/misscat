const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const GOOD_URL = 'https://chatgpt.com/c/abc-123';
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
    node('div', { 'data-composer-input': '' }, [node('div', {}, composer ? [editor] : [otherEditable])]),
    node('footer', {}, [stopOrVoice, ...(sendButton ? [send] : [])]),
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

module.exports = { bootContent: boot };

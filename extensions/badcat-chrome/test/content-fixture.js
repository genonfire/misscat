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
    getAttribute(name) { return name in el.attrs ? el.attrs[name] : null; },
    querySelectorAll(selector) {
      const found = [];
      for (const child of el.children) {
        if (child.matches(selector)) found.push(child);
        found.push(...child.querySelectorAll(selector));
      }
      return found;
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

function boot({ url = GOOD_URL, composer = true, enterSubmits = true, buttonSubmits = false, sendButton = true, initial = '', label = 'Code Review의 새 채팅', rollover = { ok: true }, hello = { ok: false }, attempt = { ok: true }, selector = null, menu = [], renameOnFill = null, restoreOnMenuOpen = null, alertOnEnter = null, onTimer = null, lateComposer = false, navigateOnMenuOpen = null, afterSubmit = null, priorBubbles = [], buttonDisabled = false, emptyMenuOpens = 0, menuLater = null, fillDelayed = false, execFails = false, pasteFails = false } = {}) {
  let delayedFill = null;
  let now = 0; // virtual clock: waits finish instantly
  let enterSubmitsNow = enterSubmits;
  const log = { submitted: [], keys: [], clicks: 0, decoyClicks: 0, fills: [], pastes: [] };

  const editor = node('div', { contenteditable: 'true', 'data-composer-markdown': '', class: 'ProseMirror', 'aria-label': label });
  Object.assign(editor, {
    textContent: initial, selected: false, collapsed: false,
    focus() {},
    dispatchEvent(ev) {
      if (ev.type === 'paste') { // a synthetic paste event: the fallback for a window without focus
        log.pastes.push(ev.clipboardData.getData('text/plain'));
        if (!pasteFails) editor.textContent = ev.clipboardData.getData('text/plain');
        return true;
      }
      log.keys.push(ev.type + ':' + ev.key);
      if (alertOnEnter && ev.type === 'keydown' && !alertOnEnter.done) { alertOnEnter.done = true; ctx.alert = limitAlert(alertOnEnter.text, alertOnEnter.labels); }
      if (ev.type === 'keydown' && ev.key === 'Enter' && enterSubmitsNow) submit();
      return true;
    },
  });
  const text = () => editor.textContent;
  let pendingBubble = null;
  const materialize = () => {
    if (pendingBubble && now >= pendingBubble.due) {
      pendingBubble.msg.parentElement = page;
      page.children.push(pendingBubble.msg);
      pendingBubble = null;
    }
  };
  const submit = () => {
    log.submitted.push(text());
    if (!afterSubmit) { editor.textContent = ''; return; }
    // ChatGPT leaves the new chat: the old editor node is detached with its text still in it, a fresh composer
    // is mounted, and (when `render`) the user's message shows up in the conversation.
    ctx.url = afterSubmit.url;
    const wrapEl = editor.parentElement;
    wrapEl.children.splice(wrapEl.children.indexOf(editor), 1);
    const fresh = node('div', { contenteditable: 'true', 'data-composer-markdown': '', class: 'ProseMirror' });
    fresh.textContent = '';
    fresh.parentElement = wrapEl;
    wrapEl.children.push(fresh);
    if (afterSubmit.render) {
      // `shown` is what the bubble renders (default: exactly what was sent); it appears after `delayMs`.
      const msg = node('div', { 'data-user-message-bubble': 'true' });
      msg.textContent = afterSubmit.shown !== undefined ? afterSubmit.shown : log.submitted[log.submitted.length - 1];
      pendingBubble = { msg, due: now + (afterSubmit.delayMs || 0) };
    }
  };

  const send = node('button', { type: 'submit', 'data-testid': 'whatever-it-is-now' });
  send.disabled = buttonDisabled;
  send.click = () => { log.clicks += 1; if (buttonSubmits) submit(); };
  const stopOrVoice = node('button', { type: 'button' });
  stopOrVoice.click = () => { log.decoyClicks += 1; };
  const otherSubmit = node('button', { type: 'submit' }); // e.g. a search form elsewhere on the page
  otherSubmit.click = () => { log.decoyClicks += 1; };
  const otherEditable = node('div', { contenteditable: 'true' }); // contenteditable without data-composer-markdown
  const legacy = node('textarea', { id: 'prompt-textarea' }); // the old selector must no longer be relied on
  legacy.click = () => {};

  const wrap = node('div', {}, composer && !lateComposer ? [editor] : composer ? [] : [otherEditable]);
  let lateAttach = lateComposer ? () => { wrap.children.push(editor); editor.parentElement = wrap; } : null; // the composer mounts after the first wait
  const form = node('form', {}, [
    node('div', { 'data-composer-input': '' }, [wrap]),
    node('footer', {}, [stopOrVoice, ...(sendButton ? [send] : [])]),
  ]);
  const prior = priorBubbles.map((t) => { const b = node('div', { 'data-user-message-bubble': 'true' }); b.textContent = t; return b; });
  const page = node('body', {}, [...prior, node('form', {}, [otherSubmit]), legacy, ...(composer ? [otherEditable] : []), form]);

  // The project selector in the composer footer; clicking it lists the projects as [data-value] items.
  let projectMenu = null;
  const selectorButton = selector === null ? null : node('button', { type: 'button', 'data-composer-navigation-target': 'workspace-project', 'aria-expanded': 'false' });
  if (selectorButton) {
    selectorButton.attrs['aria-label'] = selector === '' ? '' : '프로젝트 변경: ' + selector; // the real button carries its name only in the label
    selectorButton.clicks = 0;
    selectorButton.click = () => {
      selectorButton.clicks += 1;
      if (projectMenu) {
        page.children.splice(page.children.indexOf(projectMenu), 1);
        projectMenu = null;
        selectorButton.attrs['aria-expanded'] = 'false';
      } else {
        const items = (selectorButton.clicks <= emptyMenuOpens ? [] : menuLater && selectorButton.clicks > 2 ? menuLater : menu).map((m) => { const i = node('div', { 'data-value': m.value }); i.textContent = m.text; return i; });
        projectMenu = node('div', { role: 'dialog' }, items);
        projectMenu.parentElement = page;
        page.children.push(projectMenu);
        selectorButton.attrs['aria-expanded'] = 'true';
        if (navigateOnMenuOpen !== null) ctx.url = navigateOnMenuOpen; // the user moves on while the picker is open
        if (restoreOnMenuOpen !== null) editor.textContent = restoreOnMenuOpen; // ChatGPT restores a draft late
      }
    };
    form.children.push(selectorButton);
    selectorButton.parentElement = form;
  }

  const document = {
    body: page,
    querySelector: (sel) => page.querySelector(sel),
    querySelectorAll: (sel) => { materialize(); return page.querySelectorAll(sel); },
    execCommand(cmd, _ui, value) {
      assert.equal(cmd, 'insertText');
      if (execFails) { log.fills.push(value); return false; } // no focus: nothing is inserted
      const next = editor.collapsed ? editor.textContent + value : value; // collapsed to the end = append
      if (fillDelayed) delayedFill = next; else editor.textContent = next; // ProseMirror may apply it a tick later
      log.fills.push(value);
      if (renameOnFill !== null && selectorButton) selectorButton.attrs['aria-label'] = '프로젝트 변경: ' + renameOnFill;
      return true;
    },
  };
  const listeners = [];
  const runtimeCalls = [];
  const observers = [];
  const ctx = { url };
  const limitAlert = (text, labels) => {
    const buttons = labels.map((l) => { const b = node('button', { type: 'button' }); b.textContent = l; b.clicks = 0; b.click = () => { b.clicks += 1; }; return b; });
    const aside = node('aside', { role: 'alert' }, buttons);
    aside.textContent = text + labels.join('');
    aside.buttons = buttons;
    page.children.push(aside);
    aside.parentElement = page;
    return aside;
  };
  const mutate = () => observers.forEach((o) => o.callback());
  const context = {
    document, location: { get href() { return ctx.url; } },
    window: { getSelection: () => ({
      selectAllChildren() { editor.selected = true; editor.collapsed = false; },
      collapseToEnd() { editor.collapsed = true; },
    }) },
    DataTransfer: class { constructor() { this.d = {}; } setData(k, v) { this.d[k] = v; } getData(k) { return this.d[k] || ''; } },
    ClipboardEvent: class { constructor(type, init) { this.type = type; Object.assign(this, init); } },
    KeyboardEvent: class { constructor(type, init) { this.type = type; Object.assign(this, init); } },
    MutationObserver: class {
      constructor(callback) { this.callback = callback; this.disconnected = false; observers.push(this); }
      observe() {}
      disconnect() { this.disconnected = true; }
    },
    chrome: { runtime: { id: 'me', onMessage: { addListener: (fn) => listeners.push(fn) },
      async sendMessage(m) { runtimeCalls.push(m); return m.type === 'badcat:hello' ? hello : m.type === 'badcat:attempt' ? attempt : (typeof rollover === 'function' ? rollover(m) : rollover); } } },
    setTimeout: (fn, ms) => {
      now += ms;
      if (delayedFill !== null) { editor.textContent = delayedFill; delayedFill = null; }
      if (lateAttach) { lateAttach(); lateAttach = null; }
      if (onTimer) onTimer(ms, ctx);
      setImmediate(fn);
    },
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
  return { log, ctx, selectorButton, runtimeCalls, observers, limitAlert, mutate, setUrl: (u) => { ctx.url = u; }, editor, sendButton: send, setMenu: (m) => { menu = m; }, settle: () => new Promise((r) => setImmediate(r)), send: sendMessage, text, setEnterSubmits: (v) => { enterSubmitsNow = v; } };
}

module.exports = { bootContent: boot };

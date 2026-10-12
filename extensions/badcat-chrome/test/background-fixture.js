const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const crypto = require('node:crypto').webcrypto;

const GOOD_URL = 'https://chatgpt.com/c/abc-123';

function boot({ tabUrl = GOOD_URL, handoff = async () => ({ ok: true }), seed = {}, fakeTimers = false, control = async () => ({ ok: true }), beforeLocalSet = async () => {} } = {}) {
  const timers = [];
  const store = { ...seed };
  const tabs = { 7: { id: 7, url: tabUrl } };
  const sent = [];
  const controls = [];
  const listeners = {};
  const natives = [];
  const hook = (name) => ({ addListener: (fn) => { listeners[name] = fn; } });
  const chrome = {
    runtime: {
      id: 'me',
      lastError: null,
      onMessage: hook('runtime.onMessage'),
      connectNative(name) {
        const port = {
          name, posted: [], disconnected: false,
          postMessage(m) { this.posted.push(m); },
          disconnect() { this.disconnected = true; },
          onMessage: { addListener(fn) { port.emit = fn; } },
          onDisconnect: { addListener(fn) { port.drop = fn; } },
        };
        natives.push(port);
        return port;
      },
    },
    storage: {
      session: {
        async get(key) { return key in store ? { [key]: store[key] } : {}; },
        async set(obj) { Object.assign(store, JSON.parse(JSON.stringify(obj))); },
      },
      local: {
        async remove(key) { delete store[key]; },
        async get(key) { return key in store ? { [key]: store[key] } : {}; },
        async set(obj) { await beforeLocalSet(obj); Object.assign(store, JSON.parse(JSON.stringify(obj))); },
      },
    },
    tabs: {
      onRemoved: hook('tabs.onRemoved'),
      onUpdated: hook('tabs.onUpdated'),
      updates: [],
      async update(id, props) { tabs[id].url = props.url; this.updates.push(props.url); },
      async get(id) { if (!tabs[id]) throw new Error('No tab'); return tabs[id]; },
      async sendMessage(id, message) {
        if (message.type !== 'badcat:handoff') { controls.push(message); return control(message); }
        sent.push({ id, message });
        return handoff(message);
      },
    },
  };
  const shared = require('../shared.js');
  const context = {
    chrome, crypto, console: { warn() {}, error() {}, log() {} }, setTimeout: fakeTimers ? (fn, ms) => { timers.push({ fn, ms }); } : setTimeout, URL,
    importScripts() {}, self: { BadCatShared: shared },
  };
  context.self.chrome = chrome;
  vm.createContext(context);
  vm.runInContext(fs.readFileSync(path.join(__dirname, '..', 'background.js'), 'utf8'), context);

  const popup = (message) => new Promise((resolve) => {
    const keep = listeners['runtime.onMessage'](message, { id: 'me' }, resolve);
    assert.equal(keep, true);
  });
  // A message from the content script of `tabId` (default: the bound tab), as chrome would deliver it.
  const fromContent = (message, sender = {}) => new Promise((resolve) => {
    const full = { id: 'me', tab: { id: 7 }, frameId: 0, url: tabs[7].url, ...sender };
    const keep = listeners['runtime.onMessage'](message, full, resolve);
    if (!keep) resolve(undefined);
  });
  const navigate = async (url, id = 7) => { tabs[id].url = url; await listeners['tabs.onUpdated'](id, { url }); };
  const session = () => store['badcat.session'];
  const settle = () => new Promise((r) => setImmediate(r));
  return { popup, fromContent, updates: chrome.tabs.updates, navigate, session, store, tabs, sent, controls, listeners, natives, settle, timers };
}

module.exports = { bootBackground: boot };

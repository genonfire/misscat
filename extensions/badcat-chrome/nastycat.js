// NastyCat, service-worker side (loaded by background.js via importScripts). Owns the per-tab Claude sessions,
// the 60 s alarm poll, the nastycat-host connection and delivery bookkeeping. Namespaces: `nastycat:*` messages,
// `nastycat.*` storage keys; nothing here touches the MissCat/BadCat session.
(function () {
  'use strict';
  const N = self.NastyCatShared;

  const SESSIONS_KEY = 'nastycat.sessions.v1'; // chrome.storage.session: {tabId: session}; gone on browser restart
  const PROMPT_KEY = 'nastycat.prompt'; // the one global last-entered prompt (popup convenience only)
  const DEBUG_KEY = 'nastycat.debuglog.v1';
  const MAX_DEBUG = 300; // NastyCat's own budget, independent of MissCat's
  const ALARM = 'nastycat-poll';
  const HOST_TIMEOUT_MS = 55000;
  const MAX_CHECK = 30;

  let storageQueue = Promise.resolve();
  let debugQueue = Promise.resolve();
  let port = null;
  let requestCounter = 0;
  const waiting = new Map();
  const busy = new Set();

  function shortId(id) {
    return String(id || '').slice(-6);
  }

  // Never records prompt text or conversation content: PR keys, phases and error kinds only.
  function debugLog(event, tabId, fields) {
    const entry = { at: new Date().toISOString(), event: event, tab: Number.isInteger(tabId) ? tabId : null, ...fields };
    debugQueue = debugQueue.then(async () => {
      const stored = await chrome.storage.local.get(DEBUG_KEY);
      const list = Array.isArray(stored[DEBUG_KEY]) ? stored[DEBUG_KEY] : [];
      list.push(entry);
      await chrome.storage.local.set({ [DEBUG_KEY]: list.slice(-MAX_DEBUG) });
    }).catch(() => {});
    return debugQueue;
  }

  async function loadAll() {
    const stored = await chrome.storage.session.get(SESSIONS_KEY);
    const all = stored[SESSIONS_KEY];
    return all && typeof all === 'object' ? all : {};
  }

  // Serialized read-modify-write of the sessions map. `fn(all)` mutates and returns nothing.
  function mutate(fn) {
    const run = storageQueue.then(async () => {
      const all = await loadAll();
      const result = fn(all);
      await chrome.storage.session.set({ [SESSIONS_KEY]: all });
      return result;
    });
    storageQueue = run.catch(() => {});
    return run;
  }

  // Replace one tab's session only if it is still the same arming (the user may have stopped or re-armed).
  function update(tabId, armId, next) {
    return mutate((all) => {
      const current = all[tabId];
      if (!current || current.armId !== armId) return false;
      all[tabId] = next(current);
      return true;
    });
  }

  function publicView(s) {
    if (!s) return { active: false, error: null };
    return { active: s.active, prompt: s.prompt, tabId: s.tabId, error: s.error, note: s.note };
  }

  async function syncAlarm() {
    const all = await loadAll();
    const any = Object.values(all).some((s) => s && s.active);
    if (any) chrome.alarms.create(ALARM, { periodInMinutes: 1 });
    else await chrome.alarms.clear(ALARM);
  }

  // --------------------------------------------------------------------------- host connection

  function hostDown(reason) {
    port = null;
    for (const [, w] of waiting) w.reject(new Error(reason));
    waiting.clear();
  }

  function connect() {
    if (port) return port;
    const connection = chrome.runtime.connectNative(N.HOST_NAME);
    connection.onMessage.addListener((message) => {
      const w = message && typeof message.id === 'string' ? waiting.get(message.id) : null;
      if (w) w.resolve(message);
    });
    connection.onDisconnect.addListener(() => { if (port === connection) hostDown('host disconnected'); });
    port = connection;
    return connection;
  }

  function hostCheck(links) {
    const id = 'c' + Date.now().toString(36) + (requestCounter++).toString(36);
    return new Promise((resolve, reject) => {
      const timer = setTimeout(() => { waiting.delete(id); reject(new Error('host timeout')); }, HOST_TIMEOUT_MS);
      waiting.set(id, {
        resolve: (m) => { clearTimeout(timer); waiting.delete(id); resolve(m); },
        reject: (e) => { clearTimeout(timer); reject(e); },
      });
      try {
        connect().postMessage({ type: 'check', id: id, prs: links.map((l) => ({ repo: l.repo, pr: l.pr })) });
      } catch (e) {
        clearTimeout(timer);
        waiting.delete(id);
        port = null;
        reject(e);
      }
    }).then((message) => {
      if (message.error) throw new Error('host error');
      const results = N.parseHostResult(message, id, links);
      if (results === null) throw new Error('bad host reply');
      return results;
    });
  }

  async function checkAll(links) {
    const results = {};
    for (let i = 0; i < links.length; i += MAX_CHECK) {
      Object.assign(results, await hostCheck(links.slice(i, i + MAX_CHECK)));
    }
    return results;
  }

  // --------------------------------------------------------------------------- polling

  async function failSession(tabId, armId, error, event) {
    await update(tabId, armId, (s) => ({ ...s, active: false, error: error }));
    await debugLog(event, tabId, { error: error });
    await syncAlarm();
  }

  async function pollTab(tabId) {
    if (busy.has(tabId)) return;
    busy.add(tabId);
    try {
      const s = (await loadAll())[tabId];
      if (!s || !s.active) return;
      const armId = s.armId;
      if (s.delivery) {
        if (s.delivery.phase === 'sending') { // a worker restart or lost reply: it may have been submitted
          await update(tabId, armId, (cur) => N.settle(cur, s.delivery.id, 'uncertain'));
          await debugLog('delivery-uncertain', tabId, { reason: 'stale-sending', conversation: shortId(s.conversationId) });
        }
        return; // never an automatic retry of an uncertain delivery
      }
      let scan;
      try {
        scan = await chrome.tabs.sendMessage(tabId, { type: 'nastycat:scan' });
      } catch (e) {
        await update(tabId, armId, (cur) => ({ ...cur, note: 'Cannot read the Claude conversation yet.' }));
        await debugLog('scan-failed', tabId, { conversation: shortId(s.conversationId) });
        return;
      }
      if (!scan || scan.ok !== true || scan.conversationId !== s.conversationId) {
        await failSession(tabId, armId, 'This tab is no longer the armed Claude conversation. Re-arm to continue.', 'conversation-mismatch');
        return;
      }
      const links = N.collectLinks(scan.links);
      const ask = N.keysToCheck(s, links);
      let results = {};
      try {
        if (ask.length) results = await checkAll(ask);
      } catch (e) {
        await update(tabId, armId, (cur) => ({ ...cur, note: 'GitHub check failed; will retry.' }));
        await debugLog('merge-check-failed', tabId, { count: ask.length, reason: String(e && e.message) });
        return;
      }
      await debugLog('poll', tabId, { conversation: shortId(s.conversationId), links: links.length, checked: ask.length, phase: s.phase });
      const newId = Array.from(crypto.getRandomValues(new Uint8Array(8)), (b) => b.toString(16).padStart(2, '0')).join('');
      let send = null;
      const saved = await update(tabId, armId, (cur) => {
        const out = N.decide(cur, links, results, Date.now(), newId);
        send = out.send;
        return out.session;
      });
      if (saved && send) await deliver(tabId, armId, s, send);
    } finally {
      busy.delete(tabId);
    }
  }

  const NO_RECEIVER = /Receiving end does not exist|Could not establish connection|No tab with id/;

  async function deliver(tabId, armId, s, send) {
    await debugLog('delivery-start', tabId, { keys: send.keys, conversation: shortId(s.conversationId) });
    let outcome;
    let reason = '';
    try {
      const reply = await chrome.tabs.sendMessage(tabId, {
        type: 'nastycat:deliver', conversationId: s.conversationId, deliveryId: send.id, prompt: s.prompt,
      });
      outcome = reply && ['confirmed', 'not_sent', 'uncertain'].includes(reply.phase) ? reply.phase : 'uncertain';
      reason = reply && typeof reply.reason === 'string' ? reply.reason : '';
    } catch (e) {
      outcome = NO_RECEIVER.test(String(e && e.message)) ? 'not_sent' : 'uncertain';
      reason = 'send-message-failed';
    }
    await update(tabId, armId, (cur) => {
      const next = N.settle(cur, send.id, outcome);
      if (outcome === 'not_sent') next.note = 'Could not submit the prompt (' + (reason || 'unknown') + '); will retry.';
      return next;
    });
    await debugLog('delivery-' + outcome, tabId, { keys: send.keys, reason: reason });
  }

  async function pollAll() {
    const all = await loadAll();
    const tabs = Object.values(all).filter((s) => s && s.active).map((s) => s.tabId);
    if (!tabs.length) { await syncAlarm(); return; }
    await Promise.all(tabs.map((id) => pollTab(id)));
  }

  // --------------------------------------------------------------------------- control

  async function ensureContent(tabId) {
    try {
      const pong = await chrome.tabs.sendMessage(tabId, { type: 'nastycat:ping' });
      if (pong && pong.ok) return true;
    } catch (e) { /* not injected yet */ }
    try {
      await chrome.scripting.executeScript({ target: { tabId: tabId }, files: ['nastycat-shared.js', 'nastycat-content.js'] });
      const pong = await chrome.tabs.sendMessage(tabId, { type: 'nastycat:ping' });
      return Boolean(pong && pong.ok);
    } catch (e) {
      return false;
    }
  }

  async function start(promptInput, tabId) {
    const prompt = N.normalizePrompt(promptInput);
    if (prompt === null) return { ok: false, error: 'Enter the prompt to send.' };
    if (!Number.isInteger(tabId)) return { ok: false, error: 'No active tab.' };
    let tab;
    try {
      tab = await chrome.tabs.get(tabId);
    } catch (e) {
      return { ok: false, error: 'The tab is gone.' };
    }
    const conversationId = N.parseClaudeSession(tab.url);
    if (conversationId === null) return { ok: false, error: 'Open a Claude Code session (claude.ai/code/...) first.' };
    const existing = (await loadAll())[tabId];
    if (existing && existing.active) return { ok: false, error: 'Already watching this tab.' };
    if (!(await ensureContent(tabId))) return { ok: false, error: 'Cannot reach the Claude page. Reload the tab.' };
    const armId = Array.from(crypto.getRandomValues(new Uint8Array(8)), (b) => b.toString(16).padStart(2, '0')).join('');
    await chrome.storage.local.set({ [PROMPT_KEY]: prompt });
    // The prompt is frozen into this tab's own record: later edits of the global draft never reach it.
    await mutate((all) => { all[tabId] = N.newSession(tabId, conversationId, prompt, armId, Date.now()); });
    await debugLog('arm', tabId, { conversation: shortId(conversationId) });
    await syncAlarm();
    pollTab(tabId).catch(() => {}); // the baseline check runs now; the popup does not wait for it
    return { ok: true };
  }

  async function stop(tabId) {
    const had = await mutate((all) => {
      const had = Boolean(all[tabId]);
      delete all[tabId];
      return had;
    });
    if (had) await debugLog('disarm', tabId, {});
    await syncAlarm();
    return { ok: true };
  }

  chrome.runtime.onMessage.addListener((message, sender, sendResponse) => {
    if (sender.id !== chrome.runtime.id || !message || typeof message.type !== 'string' ||
        !message.type.startsWith('nastycat:') || sender.tab) return false; // only extension pages (the popup)
    const run = async () => {
      if (message.type === 'nastycat:status') {
        const s = (await loadAll())[message.tabId];
        const stored = await chrome.storage.local.get(PROMPT_KEY);
        return { ok: true, session: publicView(s), prompt: typeof stored[PROMPT_KEY] === 'string' ? stored[PROMPT_KEY] : '' };
      }
      if (message.type === 'nastycat:start') return start(message.prompt, message.tabId);
      if (message.type === 'nastycat:stop') return stop(message.tabId);
      if (message.type === 'nastycat:debug-log') {
        await debugQueue;
        const stored = await chrome.storage.local.get(DEBUG_KEY);
        const list = Array.isArray(stored[DEBUG_KEY]) ? stored[DEBUG_KEY] : [];
        return { ok: true, log: list.filter((e) => e.tab === null || e.tab === message.tabId) };
      }
      return { ok: false, error: 'unknown message' };
    };
    run().then(sendResponse, (e) => sendResponse({ ok: false, error: String(e && e.message) }));
    return true;
  });

  chrome.alarms.onAlarm.addListener((alarm) => {
    if (alarm.name === ALARM) pollAll().catch(() => {});
  });

  chrome.tabs.onRemoved.addListener((tabId) => {
    mutate((all) => { delete all[tabId]; }).then(async () => {
      await debugLog('tab-removed', tabId, {});
      await syncAlarm();
    }).catch(() => {});
  });

  chrome.tabs.onUpdated.addListener(async (tabId, changeInfo) => {
    if (!changeInfo.url) return;
    const s = (await loadAll())[tabId];
    if (!s || !s.active) return;
    if (N.parseClaudeSession(changeInfo.url) !== s.conversationId) {
      await failSession(tabId, s.armId, 'The tab moved to a different page. Watching stopped; re-arm to continue.', 'conversation-changed');
    }
  });

  self.NastyCat = { pollTab, pollAll, start, stop, SESSIONS_KEY, PROMPT_KEY, DEBUG_KEY };
})();

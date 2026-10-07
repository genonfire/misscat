// Service worker: owns the Native Messaging connection and the target-tab binding.
// The popup only asks it to start/stop; closing the popup changes nothing here.
importScripts('shared.js');

const { HOST_NAME, normalizeRepo, isConversationUrl, parseEvent, eventKey } = self.BadCatShared;

const SESSION_KEY = 'badcat.session'; // {active, repo, tabId, error}
const DELIVERED_KEY = 'badcat.delivered'; // keys of handoffs already submitted
const MAX_DELIVERED = 500;
const RETRY_DELAYS_MS = [0, 5000, 15000]; // e.g. ChatGPT still answering when the +1 arrives

let port = null; // the live badcat-host connection, if any
let queue = Promise.resolve(); // host events are handled one at a time

const INACTIVE = { active: false, repo: null, tabId: null, error: null };

async function getSession() {
  const stored = await chrome.storage.session.get(SESSION_KEY);
  return stored[SESSION_KEY] || INACTIVE;
}

function setSession(session) {
  return chrome.storage.session.set({ [SESSION_KEY]: session });
}

// A connection cannot survive a service-worker restart, so a stored "active" session without a
// live port is stale: mark it inactive instead of guessing.
const ready = (async () => {
  const session = await getSession();
  if (session.active && port === null) {
    await setSession({ ...INACTIVE, error: 'Monitoring was interrupted. Start again.' });
  }
})();

async function deactivate(error) {
  const current = port;
  port = null;
  if (current) {
    try {
      current.disconnect(); // the host sees EOF and exits cleanly
    } catch (e) { /* already gone */ }
  }
  await setSession({ ...INACTIVE, error: error || null });
}

async function start(repoInput, tabId) {
  const session = await getSession();
  if (session.active) return { ok: false, error: 'Already watching.' };
  const repo = normalizeRepo(repoInput);
  if (repo === null) return { ok: false, error: 'Enter the repository as owner/repo.' };
  if (!Number.isInteger(tabId)) return { ok: false, error: 'No target tab.' };

  let tab;
  try {
    tab = await chrome.tabs.get(tabId);
  } catch (e) {
    return { ok: false, error: 'Open a ChatGPT conversation tab first.' };
  }
  if (!isConversationUrl(tab.url)) {
    return { ok: false, error: 'Open a ChatGPT conversation tab first.' };
  }
  try {
    const pong = await chrome.tabs.sendMessage(tabId, { type: 'badcat:ping' });
    if (!pong || pong.ok !== true) throw new Error('no pong');
  } catch (e) {
    return { ok: false, error: 'Reload the ChatGPT tab, then try again.' };
  }

  let connection;
  try {
    connection = chrome.runtime.connectNative(HOST_NAME);
  } catch (e) {
    return { ok: false, error: 'Cannot reach badcat-host.' };
  }
  port = connection;
  connection.onMessage.addListener((message) => onHostMessage(connection, message));
  connection.onDisconnect.addListener(() => onHostDisconnect(connection));
  await setSession({ active: true, repo: repo, tabId: tabId, error: null });
  connection.postMessage({ type: 'start', repo: repo });
  return { ok: true };
}

async function stop() {
  await deactivate(null);
  return { ok: true };
}

function onHostDisconnect(connection) {
  if (connection !== port) return; // a connection we already closed
  const reason = chrome.runtime.lastError && chrome.runtime.lastError.message;
  port = null;
  console.warn('badcat-host disconnected', reason || '');
  deactivate(reason ? 'badcat-host: ' + reason.slice(0, 200) : 'badcat-host disconnected.');
}

function onHostMessage(connection, message) {
  if (connection !== port) return;
  if (message && typeof message === 'object' && typeof message.error === 'string') {
    // The host rejected the request or stopped monitoring: nothing more will arrive.
    console.warn('badcat-host error:', message.error);
    deactivate('badcat-host: ' + message.error.slice(0, 200)); // not queued: retries may be sleeping
    return;
  }
  queue = queue.then(() => handleEvent(message)).catch((e) => console.error('handoff failed', e));
}

async function delivered() {
  const stored = await chrome.storage.session.get(DELIVERED_KEY);
  return stored[DELIVERED_KEY] || [];
}

const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

async function handleEvent(message) {
  const session = await getSession();
  if (!session.active) return;
  const event = parseEvent(message, session.repo);
  if (event === null) return; // invalid payload, +2, other repo: do nothing
  const key = eventKey(event);

  for (const delay of RETRY_DELAYS_MS) {
    if (delay) await sleep(delay);
    const current = await getSession();
    if (!current.active || current.repo !== session.repo || current.tabId !== session.tabId) return;
    const sent = await delivered();
    if (sent.includes(key)) return; // idempotent: same repo + PR + HEAD is handed off once

    let tab;
    try {
      tab = await chrome.tabs.get(current.tabId);
    } catch (e) {
      await deactivate('The ChatGPT tab was closed.');
      return;
    }
    if (!isConversationUrl(tab.url)) {
      await deactivate('The tab is no longer a ChatGPT conversation.');
      return;
    }

    try {
      const result = await chrome.tabs.sendMessage(current.tabId, {
        type: 'badcat:handoff',
        pr: event.pr,
      });
      if (result && result.ok === true) {
        await chrome.storage.session.set({
          [DELIVERED_KEY]: sent.concat(key).slice(-MAX_DELIVERED),
        });
        return;
      }
      console.error('handoff not delivered:', result && result.error);
    } catch (e) {
      console.error('handoff not delivered:', e && e.message);
    }
  }
  console.error('giving up on', key, '- not marked delivered');
}

chrome.runtime.onMessage.addListener((message, sender, sendResponse) => {
  // Only the extension's own pages (the popup) may control the session, never a content script.
  if (sender.id !== chrome.runtime.id || sender.tab) return false;
  if (!message || typeof message !== 'object') return false;
  const run = async () => {
    await ready;
    if (message.type === 'badcat:status') return { ok: true, session: await getSession() };
    if (message.type === 'badcat:start') return start(message.repo, message.tabId);
    if (message.type === 'badcat:stop') return stop();
    return { ok: false, error: 'unknown message' };
  };
  run().then(sendResponse, (e) => sendResponse({ ok: false, error: String(e && e.message) }));
  return true;
});

chrome.tabs.onRemoved.addListener(async (tabId) => {
  await ready;
  const session = await getSession();
  if (session.active && session.tabId === tabId) {
    await deactivate('The ChatGPT tab was closed.');
  }
});

chrome.tabs.onUpdated.addListener(async (tabId, changeInfo) => {
  if (!changeInfo.url) return;
  await ready;
  const session = await getSession();
  if (session.active && session.tabId === tabId && !isConversationUrl(changeInfo.url)) {
    await deactivate('The tab is no longer a ChatGPT conversation.');
  }
});

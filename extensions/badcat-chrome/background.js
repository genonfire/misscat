// Service worker: owns the Native Messaging connection and the target-tab binding.
// The popup only asks it to start/stop; closing the popup changes nothing here.
importScripts('shared.js');

const { HOST_NAME, normalizeRepo, isConversationUrl, isNewChatUrl, projectOf, parseEvent, eventKey } = self.BadCatShared;

const SESSION_KEY = 'badcat.session'; // {active, repo, tabId, error, rollover?: {project}}
const DELIVERED_KEY = 'badcat.delivered.v2';
const LEGACY_KEY = 'badcat.delivered';
const BASELINES_KEY = 'badcat.delivery-baselines'; // keys of handoffs already submitted
const MAX_DELIVERED = 500;
const RETRY_DELAYS_MS = [0, 5000, 15000]; // e.g. ChatGPT still answering when the +1 arrives

let storageQueue = Promise.resolve();
let controlQueue = Promise.resolve();
let port = null; // the live badcat-host connection, if any
let queue = Promise.resolve(); // events of the live connection are handled one at a time; reset per connection

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
  port = null; // invalidate before the first asynchronous operation
  const session = await getSession();
  if (current && session.tabId !== null) {
    chrome.tabs.sendMessage(session.tabId, { type: 'badcat:cancel', session: current.sessionId }).catch(() => {});
  }
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
  connection.sessionId = crypto.randomUUID();
  port = connection;
  queue = Promise.resolve();
  connection.onMessage.addListener((message) => onHostMessage(connection, message));
  connection.onDisconnect.addListener(() => onHostDisconnect(connection));
  await setSession({ active: true, repo: repo, tabId: tabId, error: null });
  if (connection !== port) return { ok: false, error: 'badcat-host disconnected.' };
  try {
    const activated = await chrome.tabs.sendMessage(tabId, { type: 'badcat:activate', session: connection.sessionId });
    if (!activated || activated.ok !== true) throw new Error('activation failed');
    if (connection !== port) return { ok: false, error: 'badcat-host disconnected.' };
    connection.postMessage({ type: 'start', repo: repo });
  } catch (e) {
    if (connection === port) await deactivate('Cannot start the ChatGPT handoff session.');
    return { ok: false, error: 'Cannot start the ChatGPT handoff session.' };
  }
  return { ok: true };
}

async function stop(tabId) {
  // Only the tab that started the session may stop it; the popup of any other tab must not.
  const session = await getSession();
  if (session.active && session.tabId !== tabId) {
    return { ok: false, error: 'Watching in another tab.' };
  }
  await deactivate(null);
  return { ok: true };
}

function onHostDisconnect(connection) {
  if (connection !== port) return; // a connection we already closed
  const reason = chrome.runtime.lastError && chrome.runtime.lastError.message;
  console.warn('badcat-host disconnected', reason || '');
  deactivate(reason ? 'badcat-host: ' + reason.slice(0, 200) : 'badcat-host disconnected.');
}

function onHostMessage(connection, message) {
  if (connection !== port) return;
  if (message && typeof message.warning === 'string') {
    reportError(connection, message.warning);
    return;
  }
  if (message && typeof message === 'object' && typeof message.error === 'string') {
    // The host rejected the request or stopped monitoring: nothing more will arrive.
    console.warn('badcat-host error:', message.error);
    deactivate('badcat-host: ' + message.error.slice(0, 200)); // not queued: retries may be sleeping
    return;
  }
  queue = queue.then(() => handleEvent(connection, message)).catch(async (e) => {
    console.error('handoff failed', e);
    await reportError(connection, 'Handoff failed; delivery remains retryable.');
  });
}

async function delivered() {
  const stored = await chrome.storage.local.get(DELIVERED_KEY);
  return stored[DELIVERED_KEY] || [];
}

async function reportError(connection, error) {
  const session = await getSession();
  if (connection === port && session.active) {
    await setSession({ ...session, error: String(error || 'Handoff failed').slice(0, 200) });
  }
}

async function clearError(connection) {
  const session = await getSession();
  if (connection === port && session.active && session.error) {
    await setSession({ ...session, error: null });
  }
}

function acknowledge(connection, event) {
  if (connection !== port) return;
  try {
    connection.postMessage({ type: 'ack', repo: event.repo, pr: event.pr, head: event.head,
      review_id: event.review_id, delivery_id: event.delivery_id });
  } catch (e) {
    reportError(connection, 'Acknowledgement failed; the host may retry.');
  }
}

async function legacyDelivered(event) {
  // Resolve each old HEAD key once. Future review IDs on that HEAD remain eligible.
  const old = await chrome.storage.session.get(LEGACY_KEY);
  const legacyKey = event.repo.toLowerCase() + '#' + event.pr + '@' + event.head;
  const saved = await chrome.storage.local.get(BASELINES_KEY);
  const baselines = saved[BASELINES_KEY] || {};
  if (!(legacyKey in baselines) && (old[LEGACY_KEY] || []).includes(legacyKey)) {
    baselines[legacyKey] = event.legacy_review_id === undefined ? event.review_id : event.legacy_review_id;
    await chrome.storage.local.set({ [BASELINES_KEY]: Object.fromEntries(Object.entries(baselines).slice(-MAX_DELIVERED)) });
  }
  if (legacyKey in baselines && (old[LEGACY_KEY] || []).includes(legacyKey)) {
    await chrome.storage.session.set({ [LEGACY_KEY]: old[LEGACY_KEY].filter(k => k !== legacyKey) });
  }
  return event.review_id === baselines[legacyKey];
}

const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

// Every handoff belongs to the connection that received the event; once that connection is no longer
// the live one, the handoff is abandoned, even if repo and tab are unchanged in a newer session.
let pendingEvents = 0; // validated +1 handoffs in progress; a rollover is only ever authorized by one of them

async function handleEvent(connection, message) {
  pendingEvents += 1;
  try {
    await deliverEvent(connection, message);
  } finally {
    pendingEvents -= 1;
  }
}

async function deliverEvent(connection, message) {
  if (connection !== port) return;
  const session = await getSession();
  if (connection !== port || !session.active) return;
  const event = parseEvent(message, session.repo);
  if (event === null) return; // invalid payload, +2, other repo: do nothing
  const key = eventKey(event);

  for (const delay of RETRY_DELAYS_MS) {
    if (delay) await sleep(delay);
    if (connection !== port) return;
    const current = await getSession();
    if (connection !== port || !current.active || current.repo !== session.repo || current.tabId !== session.tabId) return;
    const sent = await delivered();
    if (connection !== port) return;
    if (sent.includes(key) || await legacyDelivered(event)) {
      await clearError(connection);
      acknowledge(connection, event); // replay after a lost ack: never submit twice
      return;
    }
    if (connection !== port) return;

    let tab;
    try {
      tab = await chrome.tabs.get(current.tabId);
    } catch (e) {
      if (connection === port) await deactivate('The ChatGPT tab was closed.');
      return;
    }
    if (connection !== port) return;
    // Mid-rollover the tab legitimately sits on the new-chat route until the first message is sent.
    const newChat = rolloverPage(current, tab.url);
    // Before the onUpdated handler has run, a conversation of another project must still not get a message.
    if (current.rollover && current.rollover.project && isConversationUrl(tab.url) &&
        projectOf(tab.url) !== current.rollover.project) {
      await deactivate('The new conversation is not in the original project.');
      return;
    }
    if (!isConversationUrl(tab.url) && !newChat) {
      await deactivate('The tab is no longer a ChatGPT conversation.');
      return;
    }

    try {
      const request = { type: 'badcat:handoff', pr: event.pr, event: event, session: connection.sessionId };
      if (current.rollover && current.rollover.project) request.project = current.rollover.project;
      const result = await chrome.tabs.sendMessage(current.tabId, request);
      if (result && result.ok === true) {
        // Save even if Stop raced with the response: the submission really happened.
        // ACK is still restricted to the original live connection.
        const save = storageQueue.then(async () => {
          const latest = await delivered();
          await chrome.storage.local.set({
            [DELIVERED_KEY]: [...new Set(latest.concat(key))].slice(-MAX_DELIVERED),
          });
        });
        storageQueue = save.catch(() => {});
        await save;
        await clearError(connection);
        acknowledge(connection, event);
        return;
      }
      console.error('handoff not delivered:', result && result.error);
    } catch (e) {
      console.error('handoff not delivered:', e && e.message);
    }
  }
  console.error('giving up on', key, '- not marked delivered');
  await reportError(connection, 'Handoff failed; waiting for host retry.');
}

chrome.runtime.onMessage.addListener((message, sender, sendResponse) => {
  if (sender.id !== chrome.runtime.id || !message || typeof message !== 'object') return false;
  // A content script may only rebind itself to, or announce a rollover of, the session's own tab.
  if (sender.tab) {
    if (message.type !== 'badcat:hello' && message.type !== 'badcat:rollover') return false;
    const handle = async () => {
      await ready;
      return message.type === 'badcat:hello' ? hello(sender) : rollover(message, sender);
    };
    const action = controlQueue.then(handle);
    controlQueue = action.catch(() => {});
    action.then(sendResponse, () => sendResponse({ ok: false }));
    return true;
  }
  // Only the extension's own pages (the popup) may control the session, never a content script.
  const run = async () => {
    await ready;
    if (message.type === 'badcat:status') return { ok: true, session: await getSession() };
    if (message.type === 'badcat:start') return start(message.repo, message.tabId);
    if (message.type === 'badcat:stop') return stop(message.tabId);
    return { ok: false, error: 'unknown message' };
  };
  const action = controlQueue.then(run);
  controlQueue = action.catch(() => {});
  action.then(sendResponse, (e) => sendResponse({ ok: false, error: String(e && e.message) }));
  return true;
});

// The exact top-level frame of the bound tab, on a page the session may legitimately use.
// Mid-rollover, the plain new-chat route is a valid page (the content script verifies the project).
const rolloverPage = (session, url) => Boolean(session.rollover) && isNewChatUrl(url);

async function boundSender(sender, allowNewChat) {
  const session = await getSession();
  if (!session.active || port === null || sender.frameId !== 0 || !sender.tab || sender.tab.id !== session.tabId) return null;
  const url = sender.url;
  if (isConversationUrl(url) || (allowNewChat && rolloverPage(session, url))) return session;
  return null;
}

// A freshly loaded content script (e.g. after a full-page rollover navigation) asks for its session id.
async function hello(sender) {
  const session = await boundSender(sender, true);
  return session === null ? { ok: false } : { ok: true, session: port.sessionId };
}

// The content script saw the conversation-limit alert and is about to click "new chat". Record it
// first so the resulting navigation to the new-chat route is not mistaken for leaving the conversation.
async function rollover(message, sender) {
  const session = await boundSender(sender, false);
  if (session === null || port === null || message.session !== port.sessionId) return { ok: false };
  // Only while a +1 is being handed off, and only one rollover at a time (until its new conversation exists).
  if (pendingEvents === 0 || session.rollover) return { ok: false };
  await setSession({ ...session, rollover: { project: projectOf(sender.url) } });
  return { ok: true };
}

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
  if (!session.active || session.tabId !== tabId) return;
  if (isConversationUrl(changeInfo.url)) {
    if (!session.rollover) return;
    const { rollover: { project }, ...rest } = session; // rollover finished: the new conversation exists
    if (project && projectOf(changeInfo.url) !== project) {
      await deactivate('The new conversation is not in the original project.');
      return;
    }
    await setSession(rest);
    return;
  }
  if (rolloverPage(session, changeInfo.url)) return;
  await deactivate('The tab is no longer a ChatGPT conversation.');
});

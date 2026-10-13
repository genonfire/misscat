// Service worker: owns the Native Messaging connection and the target-tab binding.
// The popup only asks it to start/stop; closing the popup changes nothing here.
importScripts('shared.js');

const { HOST_NAME, normalizeRepo, isConversationUrl, isChatGptUrl, isNewChatUrl, urlShape, projectOf, inProject, parseEvent, eventKey } = self.BadCatShared;

const SESSION_KEY = 'badcat.session'; // {active, repo, tabId, error, rollover?: {project}, explicit?: 'armed'|'running'|'used', test?: {ok, message}}
const DELIVERED_KEY = 'badcat.delivered.v2';
const LEGACY_KEY = 'badcat.delivered';
const UNCERTAIN_KEY = 'badcat.uncertain.v1'; // handoffs that may have been submitted but could not be confirmed
const BASELINES_KEY = 'badcat.delivery-baselines'; // keys of handoffs already submitted
const MAX_DELIVERED = 500;
const RETRY_DELAYS_MS = [0, 5000, 15000]; // e.g. ChatGPT still answering when the +1 arrives
const ROLLOVER_WAIT_MS = 3000; // a started rollover is progress, not a failed attempt: look again shortly
const MAX_ROLLOVER_WAITS = 10;
const EXPLICIT_WAIT_MS = 1000; // a +1 never overlaps a running ON test: it waits for the test's result (bounded)
const MAX_EXPLICIT_WAITS = 90;
const PENDING_KEY = 'badcat.pending.v1'; // {eventKey: {repo, pr}}: handoffs that were started but never confirmed delivered
const MAX_PENDING = 50;
const DEBUG_KEY = 'badcat.debuglog.v1'; // ring buffer of what the worker saw; URL shapes only, never ids or queries
const MAX_DEBUG = 300;

let storageQueue = Promise.resolve();
let debugQueue = Promise.resolve();
let controlQueue = Promise.resolve();
let port = null; // the live badcat-host connection, if any
let queue = Promise.resolve(); // events of the live connection are handled one at a time; reset per connection

const INACTIVE = { active: false, repo: null, tabId: null, error: null };

// Best-effort diagnostics for "why did it stop?": a failure to log must never change behavior.
function debugLog(event, fields) {
  const entry = { at: new Date().toISOString(), event: event, ...fields };
  debugQueue = debugQueue.then(async () => {
    const stored = await chrome.storage.local.get(DEBUG_KEY);
    const list = Array.isArray(stored[DEBUG_KEY]) ? stored[DEBUG_KEY] : [];
    list.push(entry);
    await chrome.storage.local.set({ [DEBUG_KEY]: list.slice(-MAX_DEBUG) });
  }).catch(() => {});
  return debugQueue;
}

async function tabShapeNow(tabId) {
  try {
    return urlShape((await chrome.tabs.get(tabId)).url);
  } catch (e) {
    return '(tab gone)';
  }
}

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
    debugLog('worker-restart-interrupted', { tabId: session.tabId });
    await setSession({ ...INACTIVE, error: 'Monitoring was interrupted. Start again.' });
  }
})();

async function deactivate(error) {
  const current = port;
  port = null; // invalidate before the first asynchronous operation
  debugLog('deactivate', { error: error || null, hadConnection: current !== null });
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
  debugLog('start', { tabId: tabId });
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
  const ping = async () => {
    const pong = await chrome.tabs.sendMessage(tabId, { type: 'badcat:ping' });
    if (!pong || pong.ok !== true) throw new Error('no pong');
  };
  try {
    await ping();
  } catch (e) {
    // No live content script (e.g. the extension was reloaded after the tab opened). Inject it instead of asking
    // for a page reload: a reload would make a conversation-limit alert disappear.
    try {
      await chrome.scripting.executeScript({ target: { tabId: tabId }, files: ['shared.js', 'content.js'] });
      await ping();
    } catch (e2) {
      return { ok: false, error: 'Reload the ChatGPT tab, then try again.' };
    }
  }

  await clearUncertain(); // starting again is the explicit recovery from an unconfirmed send
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
  // Watching ON arms exactly one explicit rollover/submit test; it needs no +1 event (see explicitProbe).
  await setSession({ active: true, repo: repo, tabId: tabId, error: null, explicit: 'armed' });
  if (connection !== port) return { ok: false, error: 'badcat-host disconnected.' };
  try {
    const activated = await chrome.tabs.sendMessage(tabId, { type: 'badcat:activate', session: connection.sessionId });
    if (!activated || activated.ok !== true) throw new Error('activation failed');
    if (connection !== port) return { ok: false, error: 'badcat-host disconnected.' };
    connection.postMessage({ type: 'start', repo: repo });
    // One bounded look for the real limit alert; the content script reports every outcome back.
    chrome.tabs.sendMessage(tabId, { type: 'badcat:explicit', session: connection.sessionId }).catch(() => disarmExplicit(connection));
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
  debugLog('stop-requested', { tabId: tabId });
  await deactivate(null);
  return { ok: true };
}

function onHostDisconnect(connection) {
  if (connection !== port) return; // a connection we already closed
  const reason = chrome.runtime.lastError && chrome.runtime.lastError.message;
  console.warn('badcat-host disconnected', reason || '');
  debugLog('host-disconnect', { hasReason: Boolean(reason) });
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
    debugLog('host-error', {});
    deactivate('badcat-host: ' + message.error.slice(0, 200)); // not queued: retries may be sleeping
    return;
  }
  const arrived = receivedKey(message);
  if (arrived !== null) received.set(arrived.key, { repo: arrived.repo, pr: arrived.pr });
  queue = queue.then(() => handleEvent(connection, message)).catch(async (e) => {
    console.error('handoff failed', e);
    await reportError(connection, 'Handoff failed; delivery remains retryable.');
  });
}

async function delivered() {
  const stored = await chrome.storage.local.get(DELIVERED_KEY);
  return stored[DELIVERED_KEY] || [];
}

async function uncertainKeys() {
  const stored = await chrome.storage.local.get(UNCERTAIN_KEY);
  return stored[UNCERTAIN_KEY] || [];
}

// Every write of the uncertain list goes through storageQueue, in order with Start's reset. A write on behalf
// of a connection that is no longer the live one is dropped when its turn comes. Returns whether it was saved.
function rememberUncertain(key, connection) {
  let saved = false;
  const save = storageQueue.then(async () => {
    if (connection !== port) return;
    const latest = await uncertainKeys();
    await chrome.storage.local.set({ [UNCERTAIN_KEY]: [...new Set(latest.concat(key))].slice(-MAX_DELIVERED) });
    saved = true;
  });
  storageQueue = save.catch(() => {});
  return save.then(() => saved);
}

// The ON test submits a restored command that no event is attached to. The only event it can be tied to is one whose
// handoff this extension started for that PR and never confirmed: that exact event becomes "uncertain" (never sent
// again, never ACKed, cleared only by the user's next Start), with no timeout. Every other event, including a new
// HEAD or review of the same PR, is independent and goes out at once.
const validPr = (pr) => Number.isSafeInteger(pr) && pr >= 1;

function updatePending(change) {
  const save = storageQueue.then(async () => {
    const stored = await chrome.storage.local.get(PENDING_KEY);
    const map = { ...(stored[PENDING_KEY] || {}) };
    change(map);
    await chrome.storage.local.set({ [PENDING_KEY]: Object.fromEntries(Object.entries(map).slice(-MAX_PENDING)) });
  });
  storageQueue = save.catch(() => {});
  return save;
}

const markPending = (key, event) => updatePending((map) => { delete map[key]; map[key] = { repo: event.repo, pr: event.pr }; });
const unmarkPending = (key) => updatePending((map) => { delete map[key]; });

// At the ON test's attempt: events whose handoff verifiably left this very command in a composer (pending), and the
// events received from the host and still unfinished during the test for the same PR, whose independence cannot be shown (they overlap it in time),
// are tied to the command. Every other event is independent.
async function linkTestedEvents(repo, pr, connection) {
  const pending = [];
  await updatePending((map) => {
    for (const [key, value] of Object.entries(map)) {
      if (value.repo === repo.toLowerCase() && value.pr === pr) { pending.push(key); delete map[key]; }
    }
  });
  for (const key of pending) await rememberUncertain(key, connection);
  return { pending: pending, held: await linkHeldEvents(repo, pr, connection) };
}

async function linkHeldEvents(repo, pr, connection) {
  const held = [];
  for (const [key, value] of received) {
    if (value.repo === repo.toLowerCase() && value.pr === pr) {
      await rememberUncertain(key, connection);
      held.push(key);
    }
  }
  return held;
}

// The test definitely submitted nothing: the command is still waiting, so the tied events are retryable again.
async function unlinkTestedEvents(linked) {
  for (const key of linked.pending || []) {
    await forgetUncertain(key);
    const [repo, rest] = key.split('#');
    await updatePending((map) => { map[key] = { repo: repo, pr: Number(rest.split('@')[0]) }; });
  }
  for (const key of linked.held || []) await forgetUncertain(key);
}

function clearUncertain() {
  const clear = storageQueue.then(() => chrome.storage.local.remove(UNCERTAIN_KEY));
  storageQueue = clear.catch(() => {});
  return clear;
}

function forgetUncertain(key) {
  const save = storageQueue.then(async () => {
    const latest = await uncertainKeys();
    await chrome.storage.local.set({ [UNCERTAIN_KEY]: latest.filter((k) => k !== key) });
  });
  storageQueue = save.catch(() => {});
  return save;
}

async function reportError(connection, error) {
  const session = await getSession();
  if (connection === port && session.active) {
    await setSession({ ...session, error: String(error || 'Handoff failed').slice(0, 200) });
  }
}

async function disarmExplicit(connection) {
  const session = await getSession();
  if (connection === port && session.active && session.explicit === 'armed') {
    const { explicit, ...rest } = session;
    await setSession(rest);
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
let inFlight = null; // {connection, key}: the handoff currently being sent to the tab (one at a time)
// Events received from the host and not finished yet (queued behind another one, waiting for the ON test, or retrying).
// Registered on arrival: the connection handles events one at a time, so a queued one has not started yet.
const received = new Map(); // eventKey -> {repo, pr}
function receivedKey(message) {
  if (message === null || typeof message !== 'object' || message.status !== '+1' || typeof message.repo !== 'string' ||
      !Number.isSafeInteger(message.pr) || typeof message.head !== 'string' || !Number.isSafeInteger(message.review_id)) return null;
  return { key: eventKey({ repo: message.repo, pr: message.pr, head: message.head, review_id: message.review_id }),
    repo: message.repo.toLowerCase(), pr: message.pr };
}
let pendingEvents = 0; // validated +1 handoffs in progress; a rollover is only ever authorized by one of them

async function handleEvent(connection, message) {
  pendingEvents += 1;
  try {
    await deliverEvent(connection, message);
  } finally {
    pendingEvents -= 1;
    const done = receivedKey(message);
    if (done !== null) received.delete(done.key);
  }
}

async function deliverEvent(connection, message) {
  if (connection !== port) return;
  const session = await getSession();
  if (connection !== port || !session.active) return;
  const event = parseEvent(message, session.repo);
  if (event === null) return; // invalid payload, +2, other repo: do nothing
  const key = eventKey(event);

  let attemptNo = 0;
  let rolloverWaits = 0;
  let explicitWaits = 0;
  let pause = 0;
  while (attemptNo < RETRY_DELAYS_MS.length) {
    const delay = pause || RETRY_DELAYS_MS[attemptNo];
    pause = 0;
    if (delay) await sleep(delay);
    if (connection !== port) return;
    const current = await getSession();
    if (connection !== port || !current.active || current.repo !== session.repo || current.tabId !== session.tabId) return;
    if (current.explicit === 'running' && explicitWaits < MAX_EXPLICIT_WAITS) {
      explicitWaits += 1; // the ON test owns the tab until it reports; waiting is not a failed attempt
      pause = EXPLICIT_WAIT_MS;
      continue;
    }
    const sent = await delivered();
    if (connection !== port) return;
    if (sent.includes(key) || await legacyDelivered(event)) {
      await clearError(connection);
      acknowledge(connection, event); // replay after a lost ack: never submit twice
      return;
    }
    if (connection !== port) return;
    if ((await uncertainKeys()).includes(key)) {
      // A host replay or a reloaded tab must not resubmit what may already have gone out.
      await reportError(connection, 'Delivery could not be confirmed; check ChatGPT. Not sent again.');
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
    debugLog('delivery-check', { attempt: attemptNo, tab: urlShape(tab.url), rollover: Boolean(current.rollover), newChat: newChat, explicit: current.explicit || null });
    // Before the onUpdated handler has run, a conversation of another project must still not get a message.
    if (current.rollover && current.rollover.project && isConversationUrl(tab.url) &&
        !inProject(tab.url, current.rollover.project)) {
      await deactivate('The new conversation is not in the original project.');
      return;
    }
    if (!isConversationUrl(tab.url) && !newChat) {
      if (isChatGptUrl(tab.url)) {
        // Still on chatgpt.com, just not in a conversation: keep watching, count a failed attempt and retry.
        attemptNo += 1;
        continue;
      }
      await deactivate('The tab is no longer a ChatGPT conversation. [delivery: ' + urlShape(tab.url) + ']');
      return;
    }

    // The ON test may have started while the checks above ran: look again right before the tab is asked.
    const fresh = await getSession();
    if (connection !== port || !fresh.active) return;
    if (fresh.explicit === 'running' && explicitWaits < MAX_EXPLICIT_WAITS) {
      explicitWaits += 1;
      pause = EXPLICIT_WAIT_MS;
      continue;
    }
    if ((await uncertainKeys()).includes(key)) {
      await reportError(connection, 'Delivery could not be confirmed; check ChatGPT. Not sent again.');
      return;
    }
    if (connection !== port) return;
    inFlight = { connection: connection, key: key };
    try {
      const request = { type: 'badcat:handoff', pr: event.pr, event: event, session: connection.sessionId };
      if (current.rollover && current.rollover.project) request.project = current.rollover.project;
      const result = await chrome.tabs.sendMessage(current.tabId, request);
      debugLog('handoff-result', { attempt: attemptNo, ok: Boolean(result && result.ok), ambiguous: Boolean(result && result.ambiguous), rollover: Boolean(result && result.rollover), draft: Boolean(result && result.draft), error: result && typeof result.error === 'string' ? result.error.slice(0, 120) : null });
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
        await forgetUncertain(key); // it was confirmed after all
        await unmarkPending(key);
        await clearError(connection);
        acknowledge(connection, event);
        return;
      }
      // From here on a late answer of an older session must not touch the live one.
      if (connection !== port) return;
      if (result && result.ambiguous === true) {
        // It may have been submitted: never resend automatically, never ACK; leave it visible to the user.
        console.error('handoff unconfirmed:', key);
        await rememberUncertain(key, connection);
        await reportError(connection, 'Delivery could not be confirmed; check ChatGPT. Not sent again.');
        return;
      }
      // Definitely not submitted: the attempt marker (if any) is withdrawn and the retry policy applies.
      console.error('handoff not delivered:', result && result.error);
      await forgetUncertain(key);
      // Only a draft that verifiably holds this command can later be restored and submitted by the ON test.
      if (result && result.draft === true) await markPending(key, event);
      if (result && result.rollover === true && rolloverWaits < MAX_ROLLOVER_WAITS) {
        // The tab is moving to the new chat: that is not a failed submission, so it keeps its attempts.
        rolloverWaits += 1;
        pause = ROLLOVER_WAIT_MS;
        continue;
      }
    } catch (e) {
      console.error('handoff not delivered:', e && e.message);
      debugLog('handoff-exception', { attempt: attemptNo, message: String(e && e.message).slice(0, 120) });
      if (connection !== port) return;
      // The channel broke (e.g. a full page navigation right after the submit): if the content script had
      // already recorded the attempt, the message may be out there. Never resend it automatically.
      if ((await uncertainKeys()).includes(key)) {
        await reportError(connection, 'Delivery could not be confirmed; check ChatGPT. Not sent again.');
        return;
      }
    } finally {
      if (inFlight && inFlight.connection === connection) inFlight = null;
    }
    attemptNo += 1;
  }
  console.error('giving up on', key, '- not marked delivered');
  await reportError(connection, 'Handoff failed; waiting for host retry.');
}

chrome.runtime.onMessage.addListener((message, sender, sendResponse) => {
  if (sender.id !== chrome.runtime.id || !message || typeof message !== 'object') return false;
  // A content script may only rebind itself to, or announce a rollover of, the session's own tab.
  if (sender.tab) {
    const types = ['badcat:hello', 'badcat:rollover', 'badcat:attempt', 'badcat:explicit-result'];
    if (!types.includes(message.type)) return false;
    const handle = async () => {
      await ready;
      if (message.type === 'badcat:hello') return hello(sender);
      if (message.type === 'badcat:explicit-result') return explicitResult(message, sender);
      return message.type === 'badcat:attempt' ? attempt(message, sender) : rollover(message, sender);
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
    if (message.type === 'badcat:debug-log') {
      await debugQueue;
      const stored = await chrome.storage.local.get(DEBUG_KEY);
      return { ok: true, log: Array.isArray(stored[DEBUG_KEY]) ? stored[DEBUG_KEY] : [] };
    }
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

// The content script is about to press Enter / click send: persist "this event may now be submitted" first, so
// that a broken messaging channel afterwards can never lead to an automatic resubmission.
async function attempt(message, sender) {
  const session = await boundSender(sender, true);
  if (session === null || port === null || message.session !== port.sessionId) return { ok: false };
  if (message.explicit === true) {
    // The ON test submits the restored command once. It has no event: no delivered/uncertain record, no ACK.
    // 'running' lasts until the RESULT is in: the attempt only marks the single send, real +1s keep waiting meanwhile.
    if (session.explicit !== 'running' || session.attempted === true || !session.rollover || !session.rollover.explicit || !validPr(message.pr)) return { ok: false };
    // Before the send: a broken channel afterwards must not allow a resend of the events this command belongs to.
    const linked = await linkTestedEvents(session.repo, message.pr, port);
    await setSession({ ...session, attempted: true, linked: linked, testPr: message.pr }); // single use, persisted before the send
    return { ok: true };
  }
  if (inFlight === null || inFlight.connection !== port) return { ok: false };
  return { ok: await rememberUncertain(inFlight.key, inFlight.connection) };
}

// Outcome of the ON test, shown in the popup. It is a test result only: never a delivery or a review result.
async function explicitResult(message, sender) {
  const session = await boundSender(sender, true);
  if (session === null || port === null || message.session !== port.sessionId) return { ok: false };
  const { explicit, linked, testPr, attempted, ...rest } = session;
  // The test's own exception ends with its result (success, failure or no alert): what remains of a started
  // rollover is the ordinary one a +1 would have made, with the same project checks.
  if (rest.rollover && rest.rollover.explicit) rest.rollover = { project: rest.rollover.project };
  if (message.none === true) {
    if (explicit === 'armed') await setSession(rest);
    return { ok: true };
  }
  if (explicit !== 'running' && explicit !== 'used') return { ok: false };
  // Definitely not submitted: the command is still waiting, so the events tied to it are retryable again.
  if (message.submitted !== true && linked) await unlinkTestedEvents(linked);
  // Events that arrived at the host while the command was going out are tied to it as well.
  if (message.submitted === true && validPr(testPr)) await linkHeldEvents(rest.repo, testPr, port);
  const text = String(message.message || '').slice(0, 200);
  await setSession({ ...rest, explicit: 'used', test: { ok: message.ok === true, message: text },
    error: message.ok === true ? rest.error : 'ON test: ' + text });
  return { ok: true };
}

// A freshly loaded content script (e.g. after a full-page rollover navigation) asks for its session id.
async function hello(sender) {
  const session = await boundSender(sender, true);
  if (session === null) return { ok: false };
  // A reloaded page continues a running ON test (its rollover was announced, its single attempt is unused).
  const resume = session.explicit === 'running' && session.attempted !== true && session.rollover && session.rollover.explicit;
  return resume ? { ok: true, session: port.sessionId, explicit: { project: session.rollover.project || '' } }
    : { ok: true, session: port.sessionId };
}

// The content script saw the conversation-limit alert and is about to click "new chat". Record it
// first so the resulting navigation to the new-chat route is not mistaken for leaving the conversation.
async function rollover(message, sender) {
  const session = await boundSender(sender, false);
  if (session === null || port === null || message.session !== port.sessionId) return { ok: false };
  if (session.rollover) return { ok: false }; // one rollover at a time (until its new conversation exists)
  if (message.explicit === true) {
    // Authorized once by the user's Watching ON, without a +1.
    if (session.explicit !== 'armed') return { ok: false };
    await setSession({ ...session, explicit: 'running', rollover: { project: projectOf(sender.url), explicit: true } });
    return { ok: true };
  }
  // Otherwise only while a +1 is being handed off.
  if (pendingEvents === 0) return { ok: false };
  await setSession({ ...session, rollover: { project: projectOf(sender.url) } });
  return { ok: true };
}

chrome.tabs.onRemoved.addListener(async (tabId) => {
  await ready;
  const session = await getSession();
  if (session.active && session.tabId === tabId) {
    debugLog('tab-removed', { tabId: tabId });
    await deactivate('The ChatGPT tab was closed.');
  }
});

chrome.tabs.onUpdated.addListener(async (tabId, changeInfo) => {
  const interesting = changeInfo.url || changeInfo.status || changeInfo.discarded !== undefined;
  if (!interesting) return;
  await ready;
  const session = await getSession();
  if (!session.active || session.tabId !== tabId) return;
  debugLog('tab-updated', {
    url: changeInfo.url ? urlShape(changeInfo.url) : null,
    status: changeInfo.status || null,
    discarded: changeInfo.discarded === undefined ? null : changeInfo.discarded,
    rollover: Boolean(session.rollover),
  });
  if (!changeInfo.url) return;
  if (isConversationUrl(changeInfo.url)) {
    if (!session.rollover) return;
    const { rollover: { project }, ...rest } = session; // rollover finished: the new conversation exists
    if (project && !inProject(changeInfo.url, project)) {
      await deactivate('The new conversation is not in the original project.');
      return;
    }
    await setSession(rest);
    return;
  }
  if (rolloverPage(session, changeInfo.url)) return;
  if (isChatGptUrl(changeInfo.url)) return; // anywhere on chatgpt.com keeps the session
  debugLog('update-not-conversation', { fromEvent: urlShape(changeInfo.url), tabNow: await tabShapeNow(tabId) });
  await deactivate('The tab is no longer a ChatGPT conversation. [update: ' + urlShape(changeInfo.url) + ']');
});

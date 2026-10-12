// Runs only on https://chatgpt.com/*. One narrow action: put the fixed handoff text into the
// composer and submit it. It reads nothing but the composer's own text. A handoff that could not be
// submitted (e.g. ChatGPT still answering) is left in the composer; the next handoff is appended to it.
(function () {
  'use strict';
  const { isConversationUrl, isNewChatUrl, projectOf, reviewMessage, parseEvent, eventKey } = self.BadCatShared;

  // ChatGPT's composer is a ProseMirror contenteditable. Match on semantic attributes only (no class
  // names or localized aria-labels); the send button is the submit button in the composer's own form (it sits outside [data-composer-input]).
  const COMPOSER = '[contenteditable="true"][data-composer-markdown]';
  const COMPOSER_FORM = 'form'; // wraps both the [data-composer-input] block and the footer with the send button
  const SEND_BUTTON = 'button[type="submit"]';
  // The conversation-limit alert (observed wording; matched on text, never on generated classes).
  const LIMIT_ALERT = 'aside[role="alert"]';
  const LIMIT_TEXT_RE = /최대\s*(길이|깊이)|maximum\s+(conversation\s+)?(length|depth)/i;
  const NEW_CHAT_LABELS = ['새 대화 시작', '새 채팅 시작', 'Start new chat', 'Start a new chat'];
  const NEW_CHAT_SETTLE_MS = 800; // ChatGPT restores the unsent draft shortly after the composer appears
  // The new chat's project selector (semantic attribute only) and the menu items it lists.
  const PROJECT_BUTTON = '[data-composer-navigation-target="workspace-project"]';
  const MENUS = '[role="dialog"]'; // the project picker opens as a dialog of div[data-value] entries
  const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

  async function waitFor(check, timeoutMs) {
    const deadline = Date.now() + timeoutMs;
    for (;;) {
      const value = check();
      if (value) return value;
      if (Date.now() >= deadline) return null;
      await sleep(100);
    }
  }

  // What the selector currently shows: its aria-label ("<localized prefix>: <project name>"), else its text.
  const projectLabel = () => {
    const button = document.querySelector(PROJECT_BUTTON);
    return button ? (button.getAttribute('aria-label') || button.textContent || '').trim() : '';
  };

  // True when `label` is exactly the entry's name or ends with it right after a colon (the localized prefix
  // is never hardcoded; a longer name such as "Archive Code Review" does not match "Code Review").
  const labelNames = (label, name) => name !== '' && (label === name || new RegExp(':\\s*' + name.replace(/[.*+?^${}()|[\]\\]/g, '\\$&') + '$').test(label));

  const menuItems = () => {
    const items = [];
    for (const menu of document.querySelectorAll(MENUS)) items.push(...menu.querySelectorAll('[data-value]'));
    return items;
  };

  // The ID (data-value) the selector's menu lists must equal the project ID of the old conversation (the
  // value between /g/ and /c/ in its URL, compared exactly). Returns the verified selected name, or null when the
  // name is missing, listed twice, or maps to another ID. The menu is opened, read and closed again.
  async function verifyProject(slug) {
    const button = document.querySelector(PROJECT_BUTTON);
    const label = projectLabel();
    if (!button || !label) return null;
    button.click();
    const items = await waitFor(() => { const found = menuItems(); return found.length ? found : null; }, 2000);
    let verified = null;
    if (items) {
      const same = items.filter((item) => labelNames(label, (item.textContent || '').trim()));
      const id = same.length === 1 ? same[0].getAttribute('data-value') : null;
      if (id && id === slug) verified = label;
    }
    if (button.getAttribute('aria-expanded') === 'true') button.click();
    await waitFor(() => menuItems().length === 0, 1000);
    return verified;
  }

  const readComposer = (el) => (el.textContent || '').trim();

  // True when `command` already sits in `existing` as its own space-delimited piece.
  function containsCommand(existing, command) {
    for (let i = existing.indexOf(command); i !== -1; i = existing.indexOf(command, i + 1)) {
      const end = i + command.length;
      if ((i === 0 || /\s/.test(existing[i - 1])) && (end === existing.length || /\s/.test(existing[end]))) return true;
    }
    return false;
  }

  function appendToComposer(el, existing, text) {
    const insert = existing ? ' ' + text : text;
    el.focus();
    // Insert at the end so whatever is already there is kept untouched.
    const selection = window.getSelection();
    selection.selectAllChildren(el);
    if (existing) selection.collapseToEnd(); // blank/whitespace-only content is simply replaced
    document.execCommand('insertText', false, insert);
    return existing + insert;
  }

  function findSendButton(composer) {
    const form = composer.closest(COMPOSER_FORM);
    return form ? form.querySelector(SEND_BUTTON) : null;
  }

  function pressEnter(el) {
    const init = { key: 'Enter', code: 'Enter', keyCode: 13, which: 13, bubbles: true, cancelable: true };
    el.dispatchEvent(new KeyboardEvent('keydown', init));
    el.dispatchEvent(new KeyboardEvent('keyup', init));
  }

  let activeSession = null;
  let queue = Promise.resolve();
  const UNCERTAIN = { ok: false, ambiguous: true, error: 'submission could not be confirmed; not sent again' };
  const ROLLOVER_COOLDOWN_MS = 30000;
  let rolloverClickedAt = null;
  // The "new chat" button of the real limit alert, or null. Duplicated buttons: the first match wins.
  function findLimitButton() {
    for (const alert of document.querySelectorAll(LIMIT_ALERT)) {
      if (!LIMIT_TEXT_RE.test(alert.textContent || '')) continue; // some other alert
      for (const button of alert.querySelectorAll('button')) {
        if (NEW_CHAT_LABELS.includes((button.textContent || '').trim())) return button;
      }
    }
    return null;
  }

  // Only ever called from a pending, validated handoff (never from an observer or timer): a single,
  // bounded look for the real limit alert. Returns a retryable result when a rollover was started or is
  // still in progress, otherwise null (the caller carries on, or reports its own failure).
  async function tryRollover(session) {
    if (session === undefined || !isConversationUrl(location.href)) return null;
    if (rolloverClickedAt !== null && Date.now() - rolloverClickedAt < ROLLOVER_COOLDOWN_MS) {
      return { ok: false, rollover: true, error: 'rolling over to a new conversation' };
    }
    const button = findLimitButton();
    if (!button || button.disabled) return null;
    // The background must know before the click: the navigation to the new-chat route is then expected.
    let answer = null;
    try {
      answer = await chrome.runtime.sendMessage({ type: 'badcat:rollover', session: session });
    } catch (e) { /* background unavailable: leave the alert alone */ }
    if (!answer || answer.ok !== true || session !== activeSession) return null;
    rolloverClickedAt = Date.now();
    button.click();
    return { ok: false, rollover: true, error: 'rolling over to a new conversation' };
  }

  // Events whose submission could not be confirmed (the page moved on after Enter): never sent again from here.
  const uncertain = new Set();
  const USER_MESSAGE = '[data-user-message-bubble="true"]'; // the user's message bubble; its text is compared exactly
  const countUserMessages = (text) => {
    let n = 0;
    for (const el of document.querySelectorAll(USER_MESSAGE)) if ((el.textContent || '').trim() === text) n += 1;
    return n;
  };

  const submitted = new Set(); // survives service-worker restart while this tab stays open
  async function handoff(pr, session, event, expectedProject) {
    // A freshly reloaded page may still be rebinding to its session.
    if (session !== undefined && activeSession === null) await waitFor(() => activeSession !== null, 3000);
    const active = () => session === undefined || session === activeSession;
    if (!active()) return { ok: false, error: 'stale session' };
    const key = event ? eventKey(event) : null;
    if (key && submitted.has(key)) return { ok: true };
    if (key && uncertain.has(key)) return UNCERTAIN;
    const conversation = isConversationUrl(location.href);
    if (!conversation && !(session !== undefined && isNewChatUrl(location.href))) {
      return { ok: false, error: 'not a conversation page' };
    }
    const startUrl = location.href;
    const changed = { ok: false, error: 'the page changed during the handoff; not sent' };
    const text = reviewMessage(pr);
    const limit = await tryRollover(session); // an already-limited conversation cannot take the command
    if (limit) return limit;
    if (!active()) return { ok: false, error: 'session stopped' };
    const composer = await waitFor(() => document.querySelector(COMPOSER), 5000);
    if (!active()) return { ok: false, error: 'session stopped' };
    if (!composer) return (await tryRollover(session)) || { ok: false, error: 'composer not found' };
    // Every asynchronous step below may let the user (or ChatGPT) move to another chat: the decision to
    // type or send is never based on what was true at the start, only on the live page and composer.
    const moved = () => location.href !== startUrl || document.querySelector(COMPOSER) !== composer;
    if (moved()) return changed;

    if (!conversation) {
      await sleep(NEW_CHAT_SETTLE_MS);
      if (!active()) return { ok: false, error: 'session stopped' };
      if (moved()) return changed;
    }

    // Never discard what is already there: append after exactly one space, unless this very command
    // is already pending (a retry or duplicate event), in which case just submit again.
    const existing = readComposer(composer);
    // A restored draft in a new chat is only ever this very command; anything else belongs to the user.
    if (!conversation && existing !== '' && existing !== text) {
      return { ok: false, error: 'the composer holds an unrelated draft' };
    }
    // After a rollover from a project the new chat must verifiably be in that same project, checked
    // before anything is typed; otherwise nothing is sent and the composer is left as it is.
    let verifiedName = null;
    if (expectedProject && conversation && projectOf(location.href) !== expectedProject) {
      return { ok: false, error: 'the conversation is not in the original project' };
    }
    if (!conversation && expectedProject) {
      verifiedName = await verifyProject(expectedProject);
      if (!active()) return { ok: false, error: 'session stopped' };
      if (verifiedName === null) return { ok: false, error: 'cannot verify the new chat is in the original project' };
      // The picker took a while: the composer may have been re-mounted or a draft restored/typed meanwhile.
      if (moved()) return changed;
      if (readComposer(composer) !== existing) {
        return { ok: false, error: 'the composer changed during verification; not sent' };
      }
    }
    if (moved()) return changed;
    if (!containsCommand(existing, text)) {
      const full = appendToComposer(composer, existing, text);
      if (readComposer(composer) !== full) return { ok: false, error: 'could not fill the composer' };
    }

    if (!active()) return { ok: false, error: 'session stopped' };
    if (moved()) return changed;
    if (expectedProject && conversation && projectOf(location.href) !== expectedProject) {
      return { ok: false, error: 'the conversation is not in the original project' };
    }
    if (verifiedName !== null && projectLabel() !== verifiedName) {
      return { ok: false, error: 'the selected project changed; not sent' }; // the command stays pending in the composer
    }
    const before = countUserMessages(text);
    // Enter and the send button share one outcome routine: 'sent' (composer emptied in place, or the exact
    // command appeared as a new user message), 'uncertain' (the page moved on without that proof) or 'pending'.
    const submit = async (trigger) => {
      trigger();
      await waitFor(() => readComposer(composer) === '' || moved(), 3000);
      if (!moved()) return readComposer(composer) === '' ? 'sent' : 'pending';
      // ChatGPT left the new chat and re-mounted the composer: the old node proves nothing. Only the exact
      // command appearing as a new user message (in a conversation of the expected project) confirms the send.
      const landed = () => countUserMessages(text) > before && (location.href === startUrl ||
        (isConversationUrl(location.href) && (!expectedProject || projectOf(location.href) === expectedProject)));
      return (await waitFor(landed, 5000)) ? 'sent' : 'uncertain';
    };
    let outcome = await submit(() => pressEnter(composer));
    if (outcome === 'pending') {
      const button = findSendButton(composer);
      // A moved page means the send may already have gone out: never click again there.
      if (active() && !moved() && button && !button.disabled) outcome = await submit(() => button.click());
    }
    if (outcome === 'uncertain') {
      if (key) uncertain.add(key);
      return UNCERTAIN; // it may have gone out: never resend automatically
    }
    if (outcome === 'sent') {
      if (key) {
        submitted.add(key);
        if (submitted.size > 500) submitted.delete(submitted.values().next().value);
      }
      return { ok: true };
    }
    // The send failed: the limit alert may have appeared in response; one bounded recheck.
    return (await tryRollover(session)) || { ok: false, error: 'message was not submitted' };
  }

  chrome.runtime.onMessage.addListener((message, sender, sendResponse) => {
    if (sender.id !== chrome.runtime.id || !message || typeof message !== 'object') return false;
    if (message.type === 'badcat:ping') {
      sendResponse({ ok: true });
      return false;
    }
    if (message.type === 'badcat:activate') {
      activeSession = message.session;
      sendResponse({ ok: true });
      return false;
    }
    if (message.type === 'badcat:cancel') {
      if (activeSession === message.session) activeSession = null;
      sendResponse({ ok: true });
      return false;
    }
    if (message.type === 'badcat:handoff') {
      const event = message.event ? parseEvent(message.event, message.event.repo) : null;
      if (message.session !== undefined && (!event || event.pr !== message.pr)) {
        sendResponse({ ok: false, error: 'invalid event' });
        return false;
      }
      const task = queue.then(() => handoff(message.pr, message.session, event,
        typeof message.project === 'string' ? message.project : ''));
      queue = task.catch(() => {});
      task.then(sendResponse, (e) => sendResponse({ ok: false, error: String(e.message) }));
      return true;
    }
    return false;
  });

  // After a full page load (e.g. the rollover navigation) rebind to the session of this very tab.
  try {
    chrome.runtime.sendMessage({ type: 'badcat:hello' }).then((answer) => {
      if (answer && answer.ok === true && typeof answer.session === 'string' && activeSession === null) {
        activeSession = answer.session;
      }
    }, () => {});
  } catch (e) { /* extension context unavailable */ }
})();

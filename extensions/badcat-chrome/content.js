// Runs only on https://chatgpt.com/*. One narrow action: put the fixed handoff text into the
// composer and submit it. It reads nothing but the composer's own text. A handoff that could not be
// submitted (e.g. ChatGPT still answering) is left in the composer; the next handoff is appended to it.
(function () {
  'use strict';
  const { isConversationUrl, reviewMessage, parseEvent, eventKey } = self.BadCatShared;

  // ChatGPT's composer is a ProseMirror contenteditable. Match on semantic attributes only (no class
  // names or localized aria-labels); the send button is the submit button in the composer's own form (it sits outside [data-composer-input]).
  const COMPOSER = '[contenteditable="true"][data-composer-markdown]';
  const COMPOSER_FORM = 'form'; // wraps both the [data-composer-input] block and the footer with the send button
  const SEND_BUTTON = 'button[type="submit"]';
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
  const submitted = new Set(); // survives service-worker restart while this tab stays open
  async function handoff(pr, session, event) {
    const active = () => session === undefined || session === activeSession;
    if (!active()) return { ok: false, error: 'stale session' };
    const key = event ? eventKey(event) : null;
    if (key && submitted.has(key)) return { ok: true };
    if (!isConversationUrl(location.href)) return { ok: false, error: 'not a conversation page' };
    const text = reviewMessage(pr);
    const composer = await waitFor(() => document.querySelector(COMPOSER), 5000);
    if (!active()) return { ok: false, error: 'session stopped' };
    if (!composer) return { ok: false, error: 'composer not found' };

    // Never discard what is already there: append after exactly one space, unless this very command
    // is already pending (a retry or duplicate event), in which case just submit again.
    const existing = readComposer(composer);
    if (!containsCommand(existing, text)) {
      const full = appendToComposer(composer, existing, text);
      if (readComposer(composer) !== full) return { ok: false, error: 'could not fill the composer' };
    }

    if (!active()) return { ok: false, error: 'session stopped' };
    pressEnter(composer);
    let sent = await waitFor(() => readComposer(composer) === '', 3000);
    if (!sent) {
      const button = findSendButton(composer);
      if (active() && button && !button.disabled) {
        button.click();
        sent = await waitFor(() => readComposer(composer) === '', 3000);
      }
    }
    if (sent) {
      if (key) {
        submitted.add(key);
        if (submitted.size > 500) submitted.delete(submitted.values().next().value);
      }
      return { ok: true };
    }
    return { ok: false, error: 'message was not submitted' };
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
      const task = queue.then(() => handoff(message.pr, message.session, event));
      queue = task.catch(() => {});
      task.then(sendResponse, (e) => sendResponse({ ok: false, error: String(e.message) }));
      return true;
    }
    return false;
  });
})();

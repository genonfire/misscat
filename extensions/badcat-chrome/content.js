// Runs only on https://chatgpt.com/*. One narrow action: put the fixed handoff text into the
// composer and submit it. It reads nothing but the composer's own text. A handoff that could not be
// submitted (e.g. ChatGPT still answering) is left in the composer; the next handoff is appended to it.
(function () {
  'use strict';
  const { isConversationUrl, reviewMessage } = self.BadCatShared;

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

  async function handoff(pr) {
    if (!isConversationUrl(location.href)) return { ok: false, error: 'not a conversation page' };
    const text = reviewMessage(pr);
    const composer = await waitFor(() => document.querySelector(COMPOSER), 5000);
    if (!composer) return { ok: false, error: 'composer not found' };

    // Never discard what is already there: append after exactly one space, unless this very command
    // is already pending (a retry or duplicate event), in which case just submit again.
    const existing = readComposer(composer);
    if (!containsCommand(existing, text)) {
      const full = appendToComposer(composer, existing, text);
      if (readComposer(composer) !== full) return { ok: false, error: 'could not fill the composer' };
    }

    pressEnter(composer);
    let sent = await waitFor(() => readComposer(composer) === '', 3000);
    if (!sent) {
      const button = findSendButton(composer);
      if (button && !button.disabled) {
        button.click();
        sent = await waitFor(() => readComposer(composer) === '', 3000);
      }
    }
    if (sent) return { ok: true };
    return { ok: false, error: 'message was not submitted' };
  }

  chrome.runtime.onMessage.addListener((message, sender, sendResponse) => {
    if (sender.id !== chrome.runtime.id || !message || typeof message !== 'object') return false;
    if (message.type === 'badcat:ping') {
      sendResponse({ ok: true });
      return false;
    }
    if (message.type === 'badcat:handoff') {
      handoff(message.pr).then(sendResponse, (e) => sendResponse({ ok: false, error: String(e.message) }));
      return true;
    }
    return false;
  });
})();

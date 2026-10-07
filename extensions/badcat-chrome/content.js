// Runs only on https://chatgpt.com/*. One narrow action: put the fixed handoff text into the
// composer and submit it. It reads nothing but the composer's own text.
(function () {
  'use strict';
  const { isConversationUrl, reviewMessage } = self.BadCatShared;

  const COMPOSER = '#prompt-textarea';
  const SEND_BUTTON = 'button[data-testid="send-button"]';
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

  const readComposer = (el) => ((el.value !== undefined ? el.value : el.textContent) || '').trim();

  function setComposer(el, text) {
    el.focus();
    if (el.tagName === 'TEXTAREA' || el.tagName === 'INPUT') {
      const proto = el.tagName === 'TEXTAREA' ? HTMLTextAreaElement : HTMLInputElement;
      Object.getOwnPropertyDescriptor(proto.prototype, 'value').set.call(el, text);
      el.dispatchEvent(new Event('input', { bubbles: true }));
      return;
    }
    // ProseMirror contenteditable: replace whatever is there so a retry never duplicates the text.
    const selection = window.getSelection();
    selection.selectAllChildren(el);
    document.execCommand('insertText', false, text);
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

    setComposer(composer, text);
    if (readComposer(composer) !== text) return { ok: false, error: 'could not fill the composer' };

    pressEnter(composer);
    let sent = await waitFor(() => readComposer(composer) === '', 3000);
    if (!sent) {
      const button = document.querySelector(SEND_BUTTON);
      if (button && !button.disabled) {
        button.click();
        sent = await waitFor(() => readComposer(composer) === '', 3000);
      }
    }
    if (sent) return { ok: true };
    if (readComposer(composer) === text) setComposer(composer, ''); // do not leave a stray draft
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

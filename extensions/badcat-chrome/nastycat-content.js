// NastyCat, Claude page side. Reads PR links from the conversation and submits the stored prompt once.
// Never overwrites the user's draft and never retries a click: anything unclear is reported as uncertain.
(function (root) {
  'use strict';
  if (root.NastyCatContent) return; // injected twice (manifest + on-demand): keep the first instance
  const N = root.NastyCatShared;
  const EDITOR = '[data-testid="code-prompt-input"]';
  const SEND = '[data-testid="code-prompt-send"]';
  const timing = { settleMs: 1500, buttonWaitMs: 10000, confirmMs: 60000, pollMs: 100 };
  const cancelled = new Set(); // deliveries the worker revoked (Watching turned OFF)
  // Only PR links inside the agent's own messages count as PRs this session produced: not the user's messages,
  // the composer or any other part of the page (verified live: agent PR links sit in assistant-message turns).
  const PRODUCED = '[data-testid="assistant-message"]';
  const attempted = new Set(); // delivery ids already clicked in this page: a repeat is never submitted again

  const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));
  const here = () => N.parseClaudeSession(root.location.href);
  const textOf = (el) => (el.textContent || '').replace(/​/g, '').trim();
  const sendButton = () => root.document.querySelector(SEND);
  const sendReady = (b) => b !== null && !b.disabled && b.getAttribute('aria-disabled') !== 'true';

  async function waitFor(test, limitMs) {
    const end = Date.now() + limitMs;
    for (;;) {
      const value = test();
      if (value) return value;
      if (Date.now() >= end) return null;
      await sleep(timing.pollMs);
    }
  }

  function scan() {
    const hrefs = Array.from(root.document.querySelectorAll('a[href]')).filter((a) => a.closest(PRODUCED)).map((a) => a.href);
    return { ok: true, conversationId: here(), links: hrefs.filter((h) => N.parsePrUrl(h) !== null) };
  }

  // Remove only the text this script inserted (the editor was empty before).
  function clearInserted(editor, prompt) {
    if (textOf(editor) !== prompt.trim()) return;
    editor.focus();
    const selection = root.getSelection && root.getSelection();
    if (selection && root.document.createRange) {
      const range = root.document.createRange();
      range.selectNodeContents(editor);
      selection.removeAllRanges();
      selection.addRange(range);
    }
    root.document.execCommand('delete', false);
  }

  // Number of user-message-like leaf elements (outside the composer) whose text is exactly the prompt, and
  // whether the newest of them still sits under a "Pending" marker.
  function transcript(editor, prompt) {
    const want = prompt.trim();
    const hits = Array.from(root.document.querySelectorAll('*')).filter((el) =>
      el.children.length === 0 && !editor.contains(el) && textOf(el) === want);
    const pending = (el) => {
      for (let node = el, i = 0; node && i < 4; node = node.parentElement, i++) {
        if (/\bPending\b/.test(node.textContent || '')) return true;
      }
      return false;
    };
    return { count: hits.length, pending: hits.length > 0 && pending(hits[hits.length - 1]) };
  }

  async function deliver(message) {
    const fail = (reason) => ({ ok: false, phase: 'not_sent', reason: reason });
    if (message.conversationId !== here()) return fail('wrong-conversation');
    if (cancelled.has(message.deliveryId)) return fail('cancelled');
    if (typeof message.deliveryId !== 'string' || attempted.has(message.deliveryId)) {
      return { ok: false, phase: 'uncertain', reason: 'duplicate-delivery' };
    }
    const prompt = N.normalizePrompt(message.prompt);
    if (prompt === null) return fail('bad-prompt');
    const editor = root.document.querySelector(EDITOR);
    if (!editor) return fail('no-editor');
    if (textOf(editor) !== '') return fail('busy-editor'); // a foreign draft: never overwritten
    const before = transcript(editor, prompt).count;
    editor.focus();
    if (!root.document.execCommand('insertText', false, prompt) || textOf(editor) !== prompt.trim()) {
      clearInserted(editor, prompt);
      return fail('insert-failed');
    }
    await sleep(timing.settleMs);
    const button = await waitFor(() => { const b = sendButton(); return sendReady(b) ? b : null; }, timing.buttonWaitMs);
    if (!button || cancelled.has(message.deliveryId) || message.conversationId !== here() || textOf(editor) !== prompt.trim()) {
      clearInserted(editor, prompt);
      return fail(!button ? 'no-send-button' : cancelled.has(message.deliveryId) ? 'cancelled' : 'changed-before-send');
    }
    attempted.add(message.deliveryId); // from here on nothing is ever resubmitted
    button.click();
    // A consumed composer only proves the click. Confirmed means the submitted user message appears in the
    // transcript (one more than before) and is no longer marked "Pending"; otherwise the attempt is uncertain.
    const done = await waitFor(() => {
      if (textOf(editor) !== '' || sendReady(sendButton())) return false;
      const now = transcript(editor, prompt);
      return now.count > before && !now.pending;
    }, timing.confirmMs);
    return done ? { ok: true, phase: 'confirmed' } : { ok: false, phase: 'uncertain', reason: 'unconfirmed' };
  }

  if (root.chrome && root.chrome.runtime && root.chrome.runtime.onMessage) {
    root.chrome.runtime.onMessage.addListener((message, sender, sendResponse) => {
      if (sender.id !== root.chrome.runtime.id || !message || typeof message.type !== 'string' ||
          !message.type.startsWith('nastycat:')) return false;
      const run = async () => {
        if (message.type === 'nastycat:ping') return { ok: true };
        if (message.type === 'nastycat:scan') return scan();
        if (message.type === 'nastycat:cancel') {
          if (typeof message.deliveryId === 'string') cancelled.add(message.deliveryId);
          return { ok: true };
        }
        if (message.type === 'nastycat:deliver') return deliver(message);
        return { ok: false };
      };
      run().then(sendResponse, () => sendResponse({ ok: false, phase: 'uncertain', reason: 'exception' }));
      return true;
    });
  }

  root.NastyCatContent = { timing: timing, scan: scan, deliver: deliver };
})(typeof self !== 'undefined' ? self : this);

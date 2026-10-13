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
  const NEW_CHAT_LABELS = ['새 채팅에서 계속하기', '새 대화 시작', '새 채팅 시작', 'Start new chat', 'Start a new chat'];
  const NEW_CHAT_SETTLE_MS = 800; // ChatGPT restores the unsent draft shortly after the composer appears
  // The new chat's project selector (semantic attribute only) and the menu items it lists.
  const PROJECT_BUTTON = '[data-composer-navigation-target="workspace-project"]';
  const MENUS = '[role="dialog"]'; // the project picker opens as a dialog of div[data-value] entries
  const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

  // ChatGPT keeps hidden composers of earlier chats in the DOM (with their own, disabled send button): only the
  // one that is actually rendered may be filled or submitted. Without layout information it counts as visible.
  const isVisible = (el) => el.offsetWidth === undefined || Boolean(el.offsetWidth || el.offsetHeight);
  function findComposer() {
    for (const el of document.querySelectorAll(COMPOSER)) if (isVisible(el)) return el;
    return null;
  }

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
  // value between /g/ and /c/ in its URL, compared exactly). One look: {name} when verified, {mismatch} when the
  // name is listed twice or maps to another ID (never retried, never sent), {notReady} when the selector or its
  // menu has not rendered yet. The menu is opened, read and closed again.
  // The URL segment may be "<id>-<name-slug>" while the picker lists just "<id>": equal, or the picker's ID
  // is the slug's leading part followed by a dash (never a mere prefix of a longer ID).
  const idMatchesSlug = (value, slug) => value !== '' && (value === slug || slug.startsWith(value + '-'));

  async function verifyProjectOnce(slug) {
    const button = document.querySelector(PROJECT_BUTTON);
    const label = projectLabel();
    if (!button || !label) return { notReady: true };
    button.click();
    const items = await waitFor(() => { const found = menuItems(); return found.length ? found : null; }, 2000);
    let result = { notReady: true };
    if (items) {
      const same = items.filter((item) => labelNames(label, (item.textContent || '').trim()));
      if (same.length > 1) result = { mismatch: true };
      else if (same.length === 1) {
        const value = same[0].getAttribute('data-value') || '';
        result = idMatchesSlug(value, slug) ? { name: label } : { mismatch: true, found: value };
      }
    }
    if (button.getAttribute('aria-expanded') === 'true') button.click();
    await waitFor(() => menuItems().length === 0, 1000);
    return result;
  }

  // Right after a rollover the selector and its menu may still be rendering: look a few more times, but only
  // for "not ready"; a definite mismatch ends the check at once.
  const PROJECT_TRIES = 5;
  async function verifyProject(slug) {
    let result = { notReady: true };
    for (let i = 0; i < PROJECT_TRIES; i += 1) {
      if (i) await sleep(500);
      result = await verifyProjectOnce(slug);
      if (result.name || result.mismatch) break;
    }
    return result;
  }

  const readComposer = (el) => (el.textContent || '').trim();
  const normalize = (value) => value.replace(/\s+/g, ' ').trim(); // ProseMirror may re-wrap whitespace

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

  // execCommand('insertText') needs the document to be focused; with the ChatGPT window in the background nothing
  // is inserted. A paste event carrying the text works without focus (ProseMirror reads event.clipboardData).
  function pasteInto(el, text) {
    const data = new DataTransfer();
    data.setData('text/plain', text);
    el.dispatchEvent(new ClipboardEvent('paste', { clipboardData: data, bubbles: true, cancelable: true }));
  }

  // The reasoning-effort selector of the composer (observed: a visible button with aria-haspopup="menu" whose text
  // ends with the current level, e.g. "추론 수준Medium"). Its menu holds a slider row
  // (data-reasoning-slider, aria-keyshortcuts="ArrowLeft ArrowRight", levels 0..2) next to an unrelated row that
  // only shows the level and opens the model list: keys go to the slider row, never to that one.
  const EFFORT_TRIGGER = 'button[aria-haspopup="menu"]';
  const EFFORT_SLIDER = '[data-reasoning-slider="true"]';
  const LEVEL_RE = /(Low|Medium|High)\s*$/i;
  const keyEvent = (type, key, code, keyCode) =>
    new KeyboardEvent(type, { key: key, code: code, keyCode: keyCode, which: keyCode, bubbles: true, cancelable: true });

  // Medium -> High before the send; High stays, anything else is left alone. Never blocks the send: the result is
  // only a note. Returns the note ('' when there is no such selector).
  async function raiseEffort(composer) {
    const form = composer.closest(COMPOSER_FORM);
    const trigger = form && [...form.querySelectorAll(EFFORT_TRIGGER)].find((b) => isVisible(b) && LEVEL_RE.test((b.textContent || '').trim()));
    if (!trigger) return '';
    const level = () => { const m = LEVEL_RE.exec((trigger.textContent || '').trim()); return m ? m[1].toLowerCase() : ''; };
    if (level() === 'high') return 'reasoning already High';
    if (level() !== 'medium') return 'reasoning is ' + level() + '; left unchanged';
    const row = () => [...document.querySelectorAll(EFFORT_SLIDER)].find(isVisible);
    const atMax = () => {
      const slider = row() && row().querySelector('[role="slider"]');
      return Boolean(slider) && slider.getAttribute('aria-valuenow') === slider.getAttribute('aria-valuemax');
    };
    const close = async () => {
      const open = row();
      if (open) open.dispatchEvent(keyEvent('keydown', 'Escape', 'Escape', 27));
      await waitFor(() => !row(), 1000);
    };
    // Menus of this kind open on pointerdown, not on a plain click.
    const press = (type) => trigger.dispatchEvent(new PointerEvent(type, { bubbles: true, cancelable: true, button: 0, ctrlKey: false, pointerType: 'mouse' }));
    press('pointerdown');
    press('pointerup');
    let item = await waitFor(row, 1500);
    if (!item) {
      trigger.click();
      item = await waitFor(row, 1500);
    }
    if (!item) return 'could not open the reasoning menu; left at Medium';
    if (typeof item.focus === 'function') item.focus();
    item.dispatchEvent(keyEvent('keydown', 'ArrowRight', 'ArrowRight', 39));
    item.dispatchEvent(keyEvent('keyup', 'ArrowRight', 'ArrowRight', 39));
    const raised = await waitFor(() => atMax() || level() === 'high', 2000);
    await close();
    if (!raised) return 'could not raise the reasoning to High; left at Medium';
    return await waitFor(() => level() === 'high', 1500) ? 'reasoning set to High' : 'reasoning change to High not confirmed';
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
  // `explicit`: the one-time test started by Watching ON. The command is whatever MissCat command ChatGPT restored
  // in the new chat (the caller has already matched it exactly); nothing is typed and no limit alert is clicked.
  async function handoff(pr, session, event, expectedProject, explicit) {
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
    const rolloverOnce = explicit ? async () => null : tryRollover;
    const limit = await rolloverOnce(session); // an already-limited conversation cannot take the command
    if (limit) return limit;
    if (!active()) return { ok: false, error: 'session stopped' };
    let composer = await waitFor(() => findComposer(), 5000);
    if (!active()) return { ok: false, error: 'session stopped' };
    if (!composer) return (await rolloverOnce(session)) || { ok: false, error: 'composer not found' };
    // Every asynchronous step below may let the user (or ChatGPT) move to another chat: the decision to
    // type or send is never based on what was true at the start, only on the live page and composer.
    const moved = () => location.href !== startUrl || findComposer() !== composer;
    if (moved()) return changed;

    if (!conversation) {
      await sleep(NEW_CHAT_SETTLE_MS);
      if (!active()) return { ok: false, error: 'session stopped' };
      if (moved()) return changed;
    }

    // Never discard what is already there: append after exactly one space, unless this very command
    // is already pending (a retry or duplicate event), in which case just submit again.
    let existing = readComposer(composer);
    // A restored draft in a new chat is only ever this very command (whitespace aside); anything else
    // belongs to the user and is left untouched.
    const ownDraft = (value) => value === '' || normalize(value) === text;
    if (!conversation && !ownDraft(existing)) {
      return { ok: false, error: 'the composer holds an unrelated draft' };
    }
    // After a rollover from a project the new chat must verifiably be in that same project, checked
    // before anything is typed; otherwise nothing is sent and the composer is left as it is.
    let verifiedName = null;
    if (expectedProject && conversation && projectOf(location.href) !== expectedProject) {
      return { ok: false, error: 'the conversation is not in the original project' };
    }
    if (!conversation && expectedProject) {
      const verified = await verifyProject(expectedProject);
      if (!active()) return { ok: false, error: 'session stopped' };
      if (verified.mismatch) {
        const seen = verified.found ? ' (picker ID ' + verified.found + ', conversation ' + expectedProject + ')' : ' (duplicate project name)';
        return { ok: false, error: 'the new chat is not in the original project; not sent' + seen };
      }
      if (!verified.name) return { ok: false, error: 'cannot verify the new chat is in the original project' };
      verifiedName = verified.name;
      if (location.href !== startUrl) return changed;
      // The picker took a while: ChatGPT may have re-mounted the composer (the old node is gone) or restored a
      // draft meanwhile. A new node of the same new chat is adopted if it holds nothing but this very command.
      const live = findComposer();
      if (!live) return changed;
      if (live !== composer) {
        const now = readComposer(live);
        if (!ownDraft(now)) return { ok: false, error: 'the composer holds an unrelated draft' };
        composer = live;
        existing = now;
      }
      if (readComposer(composer) !== existing) {
        return { ok: false, error: 'the composer changed during verification; not sent' };
      }
    }
    if (moved()) return changed;
    if (explicit && normalize(existing) !== text) return { ok: false, error: 'the restored command is no longer in the composer; not sent' };
    if (!containsCommand(normalize(existing), text)) {
      const full = existing ? existing + ' ' + text : text;
      appendToComposer(composer, existing, text);
      // ProseMirror applies the insertion asynchronously and may re-wrap it: wait for the composer to settle
      // on what was typed. Text that never shows up and text that shows up with something else are different.
      await waitFor(() => normalize(readComposer(composer)) === normalize(full), 1500);
      if (!active()) return { ok: false, error: 'session stopped' };
      if (moved()) return changed;
      // Nothing arrived at all in an empty composer (e.g. the window has no focus): insert by paste instead.
      // Never with existing text: the paste position would not be ours to choose.
      if (existing === '' && normalize(readComposer(composer)) === '') {
        pasteInto(composer, text);
        await waitFor(() => normalize(readComposer(composer)) === normalize(full), 1500);
        if (!active()) return { ok: false, error: 'session stopped' };
        if (moved()) return changed;
      }
      const now = normalize(readComposer(composer));
      if (now !== normalize(full)) {
        return { ok: false, error: now.includes(text) ? 'the composer changed while filling; not sent' : 'could not fill the composer' };
      }
    }

    if (!active()) return { ok: false, error: 'session stopped' };
    if (moved()) return changed;
    let note = '';
    try {
      note = await raiseEffort(composer);
    } catch (e) {
      note = 'could not change the reasoning level';
    }
    if (note) console.log('badcat:', note);
    if (!active()) return { ok: false, error: 'session stopped' };
    if (moved()) return changed;
    if (expectedProject && conversation && projectOf(location.href) !== expectedProject) {
      return { ok: false, error: 'the conversation is not in the original project' };
    }
    if (verifiedName !== null && projectLabel() !== verifiedName) {
      return { ok: false, error: 'the selected project changed; not sent' }; // the command stays pending in the composer
    }
    // A new chat sends only the command itself; in a conversation it must at least be a piece of the composer.
    const now = normalize(readComposer(composer));
    if (conversation ? !containsCommand(now, text) : now !== text) {
      return { ok: false, error: 'the composer changed before sending; not sent' };
    }
    // The selection may change (to a same-named project of another ID) at any moment before the send: the picker
    // is read again as the very last step before each click / Enter, and a mismatch sends nothing.
    const projectStillRight = async () => {
      if (verifiedName === null) return null;
      const again = await verifyProject(expectedProject);
      if (!active()) return { ok: false, error: 'session stopped' };
      if (!again.name || again.name !== verifiedName || projectLabel() !== verifiedName) {
        return { ok: false, error: 'the selected project changed; not sent' };
      }
      if (moved()) return changed;
      if (normalize(readComposer(composer)) !== text) return { ok: false, error: 'the composer changed before sending; not sent' };
      return null;
    };
    // The real send button is preferred: a synthetic Enter is not a trusted key press. It may need a moment to
    // become enabled after the text arrived; a button that stays disabled (e.g. ChatGPT is answering) means no send.
    const isEnabled = (b) => b && !b.disabled && b.getAttribute('aria-disabled') !== 'true';
    let button = findSendButton(composer);
    if (button) {
      button = await waitFor(() => { const b = findSendButton(composer); return isEnabled(b) ? b : null; }, 2000);
      if (!active()) return { ok: false, error: 'session stopped' };
      if (moved()) return changed;
      if (!button) return (await rolloverOnce(session)) || { ok: false, error: 'the send button is disabled; not sent' };
    }
    // The ON test must prove the real UI path: no send button means no synthetic-Enter substitute.
    if (explicit && !button) return { ok: false, error: 'send button not found; not sent' };
    if (session !== undefined) {
      // Durable "may be submitted" marker first: a channel broken by the navigation after sending must not
      // lead to an automatic resend. No marker, no send.
      let marked = null;
      try {
        marked = await chrome.runtime.sendMessage({ type: 'badcat:attempt', session: session, explicit: explicit === true });
      } catch (e) { /* background unavailable */ }
      if (!marked || marked.ok !== true) return { ok: false, error: 'could not record the attempt; not sent' };
      if (!active()) return { ok: false, error: 'session stopped' };
      if (moved()) return changed;
    }
    const stop = await projectStillRight();
    if (stop) return stop;
    if (button && !isEnabled(button)) return { ok: false, error: 'the send button is disabled; not sent' };
    const before = countUserMessages(text);
    // The button and Enter share one outcome routine: 'sent' (composer emptied in place, or the exact
    // command appeared as a new user message), 'uncertain' (the page moved on without that proof) or 'pending'.
    const submit = async (trigger) => {
      trigger();
      await waitFor(() => readComposer(composer) === '' || moved(), 3000);
      if (!moved()) return readComposer(composer) === '' ? 'sent' : 'pending';
      // ChatGPT left the new chat and re-mounted the composer: the old node proves nothing. Only the exact
      // command appearing as a new user message (in a conversation of the expected project) confirms the send.
      const landed = () => countUserMessages(text) > before && (location.href === startUrl ||
        (isConversationUrl(location.href) && (!expectedProject || projectOf(location.href) === expectedProject)));
      return (await waitFor(landed, 10000)) ? 'sent' : 'uncertain';
    };
    let outcome = await submit(button ? () => button.click() : () => pressEnter(composer));
    // The button was accepted but nothing happened: Enter as the fallback, never after the page moved on.
    if (explicit && outcome === 'pending') return { ok: false, error: 'the send button click had no effect; the command is still in the composer' };
    if (outcome === 'pending' && button && active() && !moved()) {
      const recheck = await projectStillRight();
      if (recheck) return recheck;
      outcome = await submit(() => pressEnter(composer));
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
      return note ? { ok: true, note: note } : { ok: true };
    }
    // The send failed: the limit alert may have appeared in response; one bounded recheck.
    return (await rolloverOnce(session)) || { ok: false, error: 'message was not submitted' };
  }

  // --- Watching ON test: one explicit rollover and submit, authorized by the user's click, without a +1. ---
  const RESTORED_RE = /^현재 작업 끝났으면 PR #([1-9][0-9]*) 리뷰해\.$/; // the exact MissCat command, nothing else
  let explicitRunning = false;
  const reportExplicit = (session, result) =>
    console.log('badcat ON test:', result.none ? 'no limit alert' : (result.ok ? 'ok' : 'failed') + ' ' + (result.message || '')) ||
    chrome.runtime.sendMessage({ type: 'badcat:explicit-result', session: session, ...result }).catch(() => {});

  // Stage 2: in the new chat, submit the restored command with the real send button.
  async function continueExplicit(session, project) {
    const fail = (stage, error) => reportExplicit(session, { ok: false, message: stage + ': ' + error });
    const reachedNewChat = await waitFor(() => isNewChatUrl(location.href) && findComposer(), 15000);
    if (!reachedNewChat) return fail('navigation', 'did not reach a new chat');
    await sleep(NEW_CHAT_SETTLE_MS);
    const draftOf = () => { const c = findComposer(); return c ? normalize(readComposer(c)) : ''; };
    const draft = await waitFor(() => RESTORED_RE.test(draftOf()) ? draftOf() : null, 8000);
    if (!draft) {
      return fail('restore', draftOf() === '' ? 'ChatGPT restored no command; nothing sent'
        : 'the composer holds something other than one MissCat command; nothing sent');
    }
    const pr = Number(RESTORED_RE.exec(draft)[1]);
    if (!Number.isSafeInteger(pr)) return fail('restore', 'invalid PR number; nothing sent');
    const task = queue.then(() => handoff(pr, session, null, project, true));
    queue = task.catch(() => {});
    const result = await task.catch((e) => ({ ok: false, error: String(e && e.message) }));
    if (result.ok) {
      return reportExplicit(session, { ok: true, message: 'Submitted the restored command for PR #' + pr + ' (test only, not a delivery).' + (result.note ? ' ' + result.note + '.' : '') });
    }
    return fail('submit', result.error || 'not submitted');
  }

  // Stage 1: the limit alert of the current conversation, clicked at most once.
  async function startExplicit(session) {
    if (explicitRunning) return;
    explicitRunning = true;
    if (isNewChatUrl(location.href)) {
      // Started on a new chat that may already hold the restored, unsent command. Its original conversation (and
      // project ID) is unknown, so at least a selected project must be visible.
      if (projectLabel() === '') return reportExplicit(session, { ok: false, message: 'restore: no project is selected in this new chat; nothing sent' });
      return continueExplicit(session, '');
    }
    if (!isConversationUrl(location.href)) return reportExplicit(session, { none: true });
    const button = findLimitButton();
    if (!button || button.disabled) return reportExplicit(session, { none: true });
    const project = projectOf(location.href) || '';
    let answer = null;
    try {
      answer = await chrome.runtime.sendMessage({ type: 'badcat:rollover', session: session, explicit: true });
    } catch (e) { /* background unavailable: leave the alert alone */ }
    if (!answer || answer.ok !== true || session !== activeSession) {
      return reportExplicit(session, { ok: false, message: 'rollover: not authorized; the alert was left alone' });
    }
    button.click();
    await continueExplicit(session, project);
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
    if (message.type === 'badcat:explicit') {
      const ok = message.session === activeSession;
      sendResponse({ ok: ok });
      if (ok) startExplicit(message.session);
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
        // The page was reloaded by the ON test's own rollover: carry on with the submit step.
        if (answer.explicit && typeof answer.explicit.project === 'string' && !explicitRunning) {
          explicitRunning = true;
          continueExplicit(answer.session, answer.explicit.project);
        }
      }
    }, () => {});
  } catch (e) { /* extension context unavailable */ }
})();

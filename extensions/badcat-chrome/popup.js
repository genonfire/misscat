// The popup is only a remote control: the service worker owns the session.
(function () {
  'use strict';
  const { normalizeRepo } = self.BadCatShared;
  const SESSION_KEY = 'badcat.session';
  const REPO_KEY = 'repo';

  const repoInput = document.getElementById('repo');
  const toggle = document.getElementById('toggle');
  const statusText = document.getElementById('status-text');
  const errorEl = document.getElementById('error');
  const noteEl = document.getElementById('note');
  let active = false;
  let session = null;
  let tabId = null; // the tab this popup was opened on

  function showError(text) {
    errorEl.textContent = text || '';
    errorEl.hidden = !text;
  }

  // The session belongs to the tab that started it: from any other tab the control stays disabled.
  function updateToggle() {
    toggle.disabled = active && !(session && Number.isInteger(tabId) && session.tabId === tabId);
  }

  function render(next) {
    session = next;
    active = Boolean(session && session.active);
    document.body.classList.toggle('on', active);
    statusText.textContent = active ? 'Watching' : 'Sleeping';
    toggle.textContent = active ? 'Nap time, Meow!' : "Catch 'em all, Meow!";
    repoInput.readOnly = active;
    if (active) repoInput.value = session.repo;
    if (session && session.error) showError(session.error);
    // The ON test result stays visible (a later retry error must not hide why the test failed).
    noteEl.textContent = session && session.test ? (session.test.ok ? '' : 'ON test failed: ') + session.test.message : '';
    noteEl.hidden = !noteEl.textContent;
    updateToggle();
  }

  async function ask(message) {
    try {
      return await chrome.runtime.sendMessage(message);
    } catch (e) {
      return { ok: false, error: 'MissCat background is not responding.' };
    }
  }

  async function onToggle() {
    if (toggle.disabled) return; // another tab owns the session, or a request is already running
    showError('');
    toggle.disabled = true;
    try {
      if (active) {
        const result = await ask({ type: 'badcat:stop', tabId: tabId });
        if (!result.ok) showError(result.error);
        return;
      }
      const repo = normalizeRepo(repoInput.value);
      if (repo === null) {
        showError('Enter the repository as owner/repo.');
        return;
      }
      repoInput.value = repo;
      await chrome.storage.local.set({ [REPO_KEY]: repo });
      const result = await ask({ type: 'badcat:start', repo: repo, tabId: tabId });
      if (!result.ok) showError(result.error);
    } finally {
      updateToggle();
    }
  }

  // For bug reports: what the worker saw around a stop (URL shapes and states only, no ids or queries).
  async function copyLog() {
    const result = await ask({ type: 'badcat:debug-log' });
    if (!result.ok) {
      showError(result.error);
      return;
    }
    try {
      await navigator.clipboard.writeText(JSON.stringify(result.log, null, 1));
      noteEl.textContent = 'Debug log copied (' + result.log.length + ' entries).';
      noteEl.hidden = false;
    } catch (e) {
      showError('Cannot copy the debug log.');
    }
  }

  async function init() {
    const [tab] = await chrome.tabs.query({ active: true, currentWindow: true });
    // Routed by the active tab's URL: a Claude page gets NastyCat; everything else is the unchanged MissCat popup.
    if (tab && self.NastyCatShared && self.NastyCatShared.isClaudeUrl(tab.url)) {
      return self.NastyCatPopup.init(tab, { input: repoInput, toggle, statusText, errorEl, noteEl });
    }
    const stored = await chrome.storage.local.get(REPO_KEY);
    if (typeof stored[REPO_KEY] === 'string') repoInput.value = stored[REPO_KEY];
    tabId = tab && Number.isInteger(tab.id) ? tab.id : null;
    const status = await ask({ type: 'badcat:status' });
    render(status.ok ? status.session : null);
    if (!status.ok) showError(status.error);

    toggle.addEventListener('click', onToggle);
    document.getElementById('copy-log').addEventListener('click', copyLog);
    repoInput.addEventListener('keydown', (e) => {
      if (e.key === 'Enter' && !active && !toggle.disabled) onToggle();
    });
    repoInput.addEventListener('input', () => {
      if (!active) chrome.storage.local.set({ [REPO_KEY]: repoInput.value.trim() });
    });
    chrome.storage.onChanged.addListener((changes, area) => {
      if (area === 'session' && changes[SESSION_KEY]) render(changes[SESSION_KEY].newValue);
    });
  }

  init();
})();

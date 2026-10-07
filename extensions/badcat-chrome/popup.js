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
    updateToggle();
  }

  async function ask(message) {
    try {
      return await chrome.runtime.sendMessage(message);
    } catch (e) {
      return { ok: false, error: 'BadCat background is not responding.' };
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

  async function init() {
    const stored = await chrome.storage.local.get(REPO_KEY);
    if (typeof stored[REPO_KEY] === 'string') repoInput.value = stored[REPO_KEY];
    const [tab] = await chrome.tabs.query({ active: true, currentWindow: true });
    tabId = tab && Number.isInteger(tab.id) ? tab.id : null;
    const status = await ask({ type: 'badcat:status' });
    render(status.ok ? status.session : null);
    if (!status.ok) showError(status.error);

    toggle.addEventListener('click', onToggle);
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

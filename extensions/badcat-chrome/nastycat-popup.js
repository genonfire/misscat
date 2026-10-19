// NastyCat mode of the popup (a Claude Code conversation tab). Same layout as MissCat; only the
// heading, input label, button text and accent differ. The service worker owns the session.
(function () {
  'use strict';
  const N = self.NastyCatShared;
  const SESSIONS_KEY = 'nastycat.sessions.v1';

  function init(tab, els) {
    const { input, toggle, statusText, errorEl, noteEl } = els;
    const tabId = tab && Number.isInteger(tab.id) ? tab.id : null;
    let active = false;
    document.body.classList.add('nasty');
    document.title = 'NastyCat';
    document.querySelector('.title').textContent = 'NastyCat';
    document.querySelector('label[for="repo"]').textContent = 'Prompt';
    input.placeholder = 'Prompt sent to this session';
    input.maxLength = N.MAX_PROMPT;

    function showError(text) {
      errorEl.textContent = text || '';
      errorEl.hidden = !text;
    }

    function render(session) {
      active = Boolean(session && session.active);
      document.body.classList.toggle('on', active);
      statusText.textContent = active ? 'Watching' : 'Sleeping';
      toggle.textContent = active ? 'Nap time, Meow!' : 'No napping, Meow!';
      input.readOnly = active; // the armed prompt is frozen
      if (active && typeof session.prompt === 'string') input.value = session.prompt;
      showError(session && session.error);
      noteEl.textContent = active && session.note ? session.note : '';
      noteEl.hidden = !noteEl.textContent;
    }

    async function ask(message) {
      try {
        return await chrome.runtime.sendMessage(message);
      } catch (e) {
        return { ok: false, error: 'NastyCat background is not responding.' };
      }
    }

    async function onToggle() {
      if (toggle.disabled) return;
      showError('');
      toggle.disabled = true;
      try {
        if (active) {
          const result = await ask({ type: 'nastycat:stop', tabId: tabId });
          if (!result.ok) showError(result.error);
          else render(null);
          return;
        }
        if (N.normalizePrompt(input.value) === null) {
          showError('Enter the prompt to send.');
          return;
        }
        const result = await ask({ type: 'nastycat:start', prompt: input.value, tabId: tabId });
        if (!result.ok) showError(result.error);
      } finally {
        toggle.disabled = false;
      }
    }

    async function copyLog() {
      const result = await ask({ type: 'nastycat:debug-log', tabId: tabId });
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

    return (async () => {
      const status = await ask({ type: 'nastycat:status', tabId: tabId });
      if (status.ok) {
        input.value = status.prompt || '';
        render(status.session);
      } else {
        showError(status.error);
      }
      toggle.addEventListener('click', onToggle);
      document.getElementById('copy-log').addEventListener('click', copyLog);
      input.addEventListener('keydown', (e) => {
        if (e.key === 'Enter' && !active && !toggle.disabled) onToggle();
      });
      input.addEventListener('input', () => {
        if (!active) chrome.storage.local.set({ 'nastycat.prompt': input.value });
      });
      chrome.storage.onChanged.addListener((changes, area) => {
        if (area !== 'session' || !changes[SESSIONS_KEY]) return;
        const all = changes[SESSIONS_KEY].newValue || {};
        const s = all[tabId];
        render(s ? { active: s.active, prompt: s.prompt, error: s.error, note: s.note } : null);
      });
    })();
  }

  self.NastyCatPopup = { init: init };
})();

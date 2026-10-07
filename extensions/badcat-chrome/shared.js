// Pure helpers shared by the popup, the service worker and the content script.
// Loaded as a classic script everywhere (and via require() in the Node tests): no build step.
(function (root) {
  'use strict';

  const HOST_NAME = 'com.genonfire.badcat_host';
  const OWNER_RE = /^[A-Za-z0-9-]+$/;
  const NAME_RE = /^[A-Za-z0-9_.-]+$/;
  const HEAD_RE = /^[0-9a-f]{40}$/i;
  // A ChatGPT conversation: /c/<id>, also under a project or GPT prefix (/g/<slug>/c/<id>).
  const CONVERSATION_PATH_RE = /\/c\/[A-Za-z0-9-]+\/?$/;

  // "owner/repo" or null. Mirrors badcat-host's validation; the host validates again.
  function normalizeRepo(value) {
    if (typeof value !== 'string') return null;
    const repo = value.trim();
    if (repo.length > 200 || repo.split('/').length !== 2) return null;
    const [owner, name] = repo.split('/');
    if (owner.startsWith('-') || name === '.' || name === '..') return null;
    if (!OWNER_RE.test(owner) || !NAME_RE.test(name)) return null;
    return repo;
  }

  function isConversationUrl(url) {
    let parsed;
    try {
      parsed = new URL(url);
    } catch (e) {
      return false;
    }
    return parsed.protocol === 'https:' && parsed.hostname === 'chatgpt.com' &&
      CONVERSATION_PATH_RE.test(parsed.pathname);
  }

  // The only host message that triggers a handoff: a "+1" for the watched repo.
  // Returns {repo, pr, head} or null for anything else (errors, +2, malformed, other repo).
  function parseEvent(message, watchedRepo) {
    if (message === null || typeof message !== 'object' || Array.isArray(message)) return null;
    if (message.status !== '+1') return null;
    const repo = normalizeRepo(message.repo);
    if (repo === null || repo.toLowerCase() !== String(watchedRepo).toLowerCase()) return null;
    const pr = message.pr;
    if (typeof pr !== 'number' || !Number.isSafeInteger(pr) || pr < 1) return null;
    if (typeof message.head !== 'string' || !HEAD_RE.test(message.head)) return null;
    return { repo: repo, pr: pr, head: message.head.toLowerCase() };
  }

  function eventKey(event) {
    return event.repo.toLowerCase() + '#' + event.pr + '@' + event.head;
  }

  // The fixed handoff text, built only from a validated integer.
  function reviewMessage(pr) {
    if (typeof pr !== 'number' || !Number.isSafeInteger(pr) || pr < 1) {
      throw new Error('invalid PR number');
    }
    // Phrased so ChatGPT finishes any task in progress before starting the review.
    return '현재 작업 끝났으면 PR#' + pr + ' 리뷰해.';
  }

  const api = {
    HOST_NAME: HOST_NAME,
    normalizeRepo: normalizeRepo,
    isConversationUrl: isConversationUrl,
    parseEvent: parseEvent,
    eventKey: eventKey,
    reviewMessage: reviewMessage,
  };
  if (typeof module !== 'undefined' && module.exports) module.exports = api;
  else root.BadCatShared = api;
})(typeof self !== 'undefined' ? self : this);

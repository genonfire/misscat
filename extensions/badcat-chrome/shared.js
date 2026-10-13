// Pure helpers shared by the popup, the service worker and the content script.
// Loaded as a classic script everywhere (and via require() in the Node tests): no build step.
(function (root) {
  'use strict';

  const HOST_NAME = 'com.genonfire.badcat_host';
  const OWNER_RE = /^[A-Za-z0-9-]+$/;
  const NAME_RE = /^[A-Za-z0-9_.-]+$/;
  const HEAD_RE = /^[0-9a-f]{40}$/;
  // A ChatGPT conversation: /c/<id>, also under a project or GPT prefix (/g/<slug>/c/<id>).
  // ChatGPT briefly uses a temporary id with other characters (e.g. WEB:<uuid>, possibly percent-encoded or
  // longer) before the real one appears, so accept any non-empty final segment rather than a character list.
  const CONVERSATION_PATH_RE = /\/c\/[^/\s]+\/?$/;

  // "owner/repo" or null. Mirrors badcat-host's validation; the host validates again.
  function normalizeRepo(value) {
    if (typeof value !== 'string') return null;
    const repo = value.trim();
    if (repo.length > 200 || repo.split('/').length !== 2) return null;
    const [owner, name] = repo.split('/');
    if (owner.startsWith('-') || name === '.' || name === '..') return null;
    if ((owner.match(OWNER_RE) || [])[0] !== owner || (name.match(NAME_RE) || [])[0] !== name) return null;
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

  // Any page of https://chatgpt.com: while watching, the tab may sit anywhere on the site without stopping.
  function isChatGptUrl(url) {
    try {
      const parsed = new URL(url);
      return parsed.protocol === 'https:' && parsed.hostname === 'chatgpt.com';
    } catch (e) {
      return false;
    }
  }

  // A diagnostic-only description of a URL for error messages: scheme, host and a path whose long segments are
  // reduced to their length and whether they look like a plain id. No query, fragment or conversation id.
  function urlShape(url) {
    if (typeof url !== 'string' || url === '') return '(empty)';
    try {
      const parsed = new URL(url);
      const path = parsed.pathname.split('/').map(function (seg) {
        if (seg.length <= 4) return seg;
        return '<' + seg.length + (/^[A-Za-z0-9-]+$/.test(seg) ? ':id' : ':special') + '>';
      }).join('/');
      return parsed.protocol + '//' + parsed.hostname + path;
    } catch (e) {
      return '(unparsable)';
    }
  }

  // The new-chat landing route ChatGPT shows after "새 대화 시작": a valid state only mid-rollover.
  function isNewChatUrl(url) {
    let parsed;
    try {
      parsed = new URL(url);
    } catch (e) {
      return false;
    }
    return parsed.protocol === 'https:' && parsed.hostname === 'chatgpt.com' && parsed.pathname === '/';
  }

  // The project slug of /g/<slug>/c/<id>, or null for a plain /c/<id> conversation.
  function projectOf(url) {
    try {
      const m = new URL(url).pathname.match(/^\/g\/([A-Za-z0-9_-]+)\/c\//);
      return m ? m[1] : null;
    } catch (e) {
      return null;
    }
  }

  const BARE_PROJECT_RE = /^g-p-[0-9a-f]{32}$/;
  // Whether a conversation URL belongs to the expected project. ChatGPT may show the project as the bare ID
  // (g-p-<32 hex>) or as <id>-<name-slug>; they match when equal or when the bare ID leads the slugged one.
  function inProject(url, expected) {
    const actual = projectOf(url);
    if (actual === null || !expected) return false;
    if (actual === expected) return true;
    const [bare, slugged] = actual.length < expected.length ? [actual, expected] : [expected, actual];
    return BARE_PROJECT_RE.test(bare) && slugged.startsWith(bare + '-');
  }

  // The only host message that triggers a handoff: a "+1" for the watched repo.
  // Returns the validated event contract for forwarding and re-validation by content.js.
  // Normalization preserves status and IDs; errors, +2, malformed/other-repo events return null.
  function parseEvent(message, watchedRepo) {
    if (message === null || typeof message !== 'object' || Array.isArray(message)) return null;
    if (message.status !== '+1') return null;
    const repo = normalizeRepo(message.repo);
    if (repo === null || repo !== message.repo || repo.toLowerCase() !== String(watchedRepo).toLowerCase()) return null;
    const pr = message.pr;
    if (typeof pr !== 'number' || !Number.isSafeInteger(pr) || pr < 1) return null;
    if (typeof message.head !== 'string' || message.head.length !== 40 || !HEAD_RE.test(message.head)) return null;
    if (!Number.isSafeInteger(message.review_id) || message.review_id < 1) return null;
    if (typeof message.delivery_id !== 'string' || message.delivery_id.length !== 32 || !/^[0-9a-f]{32}$/.test(message.delivery_id)) return null;
    const event = { repo: repo.toLowerCase(), pr: pr, head: message.head, status: message.status,
      review_id: message.review_id, delivery_id: message.delivery_id };
    if (message.legacy_review_id !== undefined) {
      if (!Number.isSafeInteger(message.legacy_review_id) || message.legacy_review_id < 0) return null;
      event.legacy_review_id = message.legacy_review_id;
    }
    return event;
  }

  function eventKey(event) {
    return event.repo.toLowerCase() + '#' + event.pr + '@' + event.head + ':' + event.review_id;
  }

  // The fixed handoff text, built only from a validated integer.
  function reviewMessage(pr) {
    if (typeof pr !== 'number' || !Number.isSafeInteger(pr) || pr < 1) {
      throw new Error('invalid PR number');
    }
    // Phrased so ChatGPT finishes any task in progress before starting the review.
    return '현재 작업 끝났으면 PR #' + pr + ' 리뷰해.';
  }

  const api = {
    HOST_NAME: HOST_NAME,
    normalizeRepo: normalizeRepo,
    isConversationUrl: isConversationUrl,
    isChatGptUrl: isChatGptUrl,
    isNewChatUrl: isNewChatUrl,
    urlShape: urlShape,
    projectOf: projectOf,
    inProject: inProject,
    parseEvent: parseEvent,
    eventKey: eventKey,
    reviewMessage: reviewMessage,
  };
  if (typeof module !== 'undefined' && module.exports) module.exports = api;
  else root.BadCatShared = api;
})(typeof self !== 'undefined' ? self : this);

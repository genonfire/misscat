// NastyCat: pure helpers and the polling state machine. No DOM, no chrome.* APIs.
// Loaded as a classic script (popup, service worker, content script) and via require() in the Node tests.
(function (root) {
  'use strict';

  const HOST_NAME = 'com.genonfire.nastycat_host';
  const MAX_PROMPT = 2000;
  // Beyond this many distinct PR links the session stops visibly (see nastycat.js) instead of silently dropping the tail.
  const MAX_LINKS = 20000;
  const OWNER_RE = /^[A-Za-z0-9-]+$/;
  const NAME_RE = /^[A-Za-z0-9_.-]+$/;
  const SESSION_PATH_RE = /^\/code\/([A-Za-z0-9_-]{1,128})\/?$/;
  const PR_PATH_RE = /^\/([A-Za-z0-9-]+)\/([A-Za-z0-9_.-]+)\/pull\/([1-9][0-9]{0,14})\/?$/;
  const STARTUP = '__startup__';

  function parse(url) {
    try {
      return new URL(url);
    } catch (e) {
      return null;
    }
  }

  function isClaudeUrl(url) {
    const u = parse(url);
    return u !== null && u.protocol === 'https:' && u.hostname === 'claude.ai';
  }

  // The persistent identity of a Claude Code (Remote Control) conversation: the id in /code/<id>, or null.
  function parseClaudeSession(url) {
    const u = parse(url);
    if (u === null || u.protocol !== 'https:' || u.hostname !== 'claude.ai') return null;
    const m = u.pathname.match(SESSION_PATH_RE);
    return m ? m[1] : null;
  }

  // {repo, pr} for an exact https://github.com/<owner>/<repo>/pull/<n> link, else null. Query and fragment are ignored.
  function parsePrUrl(href) {
    const u = parse(href);
    if (u === null || u.protocol !== 'https:' || u.hostname !== 'github.com' || u.port !== '' ||
        u.username !== '' || u.password !== '') return null;
    const m = u.pathname.match(PR_PATH_RE);
    if (!m || m[1].startsWith('-') || m[2] === '.' || m[2] === '..' || !OWNER_RE.test(m[1]) || !NAME_RE.test(m[2])) return null;
    const pr = Number(m[3]);
    if (!Number.isSafeInteger(pr)) return null;
    return { repo: m[1] + '/' + m[2], pr: pr };
  }

  function prKey(repo, pr) {
    return String(repo).toLowerCase() + '#' + pr;
  }

  // Validated, deduplicated PR links (first spelling wins). Old links are never dropped to make room for new ones:
  // settled PRs are skipped later by keysToCheck, so cost stays bounded without losing discovery.
  function collectLinks(hrefs) {
    const seen = new Set();
    const out = [];
    if (!Array.isArray(hrefs)) return out;
    for (const href of hrefs) {
      if (typeof href !== 'string') continue;
      const link = parsePrUrl(href);
      if (link === null) continue;
      const key = prKey(link.repo, link.pr);
      if (seen.has(key)) continue;
      seen.add(key);
      out.push(link);
    }
    return out;
  }

  // The prompt is sent verbatim; only emptiness and size are checked. Returns the same string or null.
  function normalizePrompt(value) {
    if (typeof value !== 'string' || value.trim() === '' || value.length > MAX_PROMPT) return null;
    return value;
  }

  // Validates the host's reply to a `check` request. Returns {key: {merged, merged_at}} for answered PRs.
  function parseHostResult(message, id, asked) {
    if (message === null || typeof message !== 'object' || message.type !== 'result' || message.id !== id ||
        !Array.isArray(message.prs)) return null;
    const wanted = new Set(asked.map((l) => prKey(l.repo, l.pr)));
    const out = {};
    for (const item of message.prs) {
      if (item === null || typeof item !== 'object' || typeof item.repo !== 'string' ||
          !Number.isSafeInteger(item.pr) || typeof item.merged !== 'boolean') continue;
      const key = prKey(item.repo, item.pr);
      if (!wanted.has(key)) continue;
      let mergedAt = null;
      if (item.merged) {
        if (typeof item.merged_at !== 'string' || Number.isNaN(Date.parse(item.merged_at))) continue;
        mergedAt = item.merged_at;
      }
      out[key] = { merged: item.merged, merged_at: mergedAt };
    }
    return out;
  }

  function newSession(tabId, conversationId, prompt, armId, now) {
    return {
      active: true, tabId: tabId, conversationId: conversationId, prompt: prompt, armId: armId,
      startedAt: now, phase: 'baseline', startup: 'none', prs: {}, delivery: null, error: null, note: '',
    };
  }

  // Which tracked PRs still need a GitHub answer: everything during the baseline, then undone ones and new links.
  function keysToCheck(session, links) {
    return links.filter((l) => {
      if (session.phase === 'baseline') return true;
      const known = session.prs[prKey(l.repo, l.pr)];
      return !known || !known.done;
    });
  }

  // One poll step. Pure: returns {session, send} where send = {id, keys, startup} when the stored prompt is due.
  // `results` maps prKey -> {merged, merged_at} for PRs GitHub answered; missing means unknown (never guessed).
  function decide(session, links, results, now, newId) {
    const s = JSON.parse(JSON.stringify(session));
    if (!s.active || s.delivery) return { session: s, send: null };
    const merged = (key) => results[key] && results[key].merged;
    if (s.phase === 'baseline') {
      // Fail closed: the baseline must be complete before deciding to send anything.
      if (links.some((l) => !results[prKey(l.repo, l.pr)])) {
        s.note = 'Waiting for the GitHub baseline check.';
        return { session: s, send: null };
      }
      for (const l of links) {
        const key = prKey(l.repo, l.pr);
        s.prs[key] = { repo: l.repo, pr: l.pr, merged: merged(key), done: merged(key) };
      }
      s.phase = 'watching';
      s.startup = links.every((l) => merged(prKey(l.repo, l.pr))) ? 'pending' : 'none';
    } else {
      for (const l of links) {
        const key = prKey(l.repo, l.pr);
        const answer = results[key];
        if (!answer) continue; // unknown: look again next poll (a new link is registered only once answered)
        const known = s.prs[key];
        if (!known) {
          // A PR first seen after ON: eligible if open, or if it merged after ON (merged_at guards stale history).
          const after = answer.merged && Date.parse(answer.merged_at) >= s.startedAt;
          s.prs[key] = { repo: l.repo, pr: l.pr, merged: answer.merged, done: answer.merged && !after };
        } else if (!known.done && answer.merged) {
          known.merged = true;
        }
      }
    }
    const due = Object.keys(s.prs).filter((k) => s.prs[k].merged && !s.prs[k].done).sort();
    const startup = s.startup === 'pending';
    const open = Object.keys(s.prs).filter((k) => !s.prs[k].merged).length;
    s.note = open ? 'Watching ' + open + ' open PR' + (open === 1 ? '' : 's') + '.' : 'Watching for new PRs.';
    if (!startup && due.length === 0) return { session: s, send: null };
    const keys = startup ? [STARTUP].concat(due) : due;
    s.delivery = { id: newId, phase: 'sending', keys: keys };
    return { session: s, send: { id: newId, keys: keys, startup: startup } };
  }

  // Applies the outcome of a delivery attempt: 'confirmed' | 'not_sent' | 'uncertain'.
  function settle(session, deliveryId, outcome) {
    const s = JSON.parse(JSON.stringify(session));
    if (!s.delivery || s.delivery.id !== deliveryId) return s;
    if (outcome === 'confirmed') {
      for (const key of s.delivery.keys) {
        if (key === STARTUP) s.startup = 'done';
        else if (s.prs[key]) s.prs[key].done = true;
      }
      s.delivery = null;
      s.error = null;
    } else if (outcome === 'not_sent') {
      s.delivery = null; // nothing reached Claude: the same events stay due and are retried next poll
    } else {
      s.delivery.phase = 'uncertain';
      s.error = 'Delivery could not be confirmed. Stop and re-arm to continue; nothing is resent automatically.';
    }
    return s;
  }

  const api = {
    HOST_NAME: HOST_NAME, MAX_LINKS: MAX_LINKS, STARTUP: STARTUP, MAX_PROMPT: MAX_PROMPT,
    isClaudeUrl: isClaudeUrl, parseClaudeSession: parseClaudeSession, parsePrUrl: parsePrUrl, prKey: prKey,
    collectLinks: collectLinks, normalizePrompt: normalizePrompt, parseHostResult: parseHostResult,
    newSession: newSession, keysToCheck: keysToCheck, decide: decide, settle: settle,
  };
  if (typeof module !== 'undefined' && module.exports) module.exports = api;
  else root.NastyCatShared = api;
})(typeof self !== 'undefined' ? self : this);

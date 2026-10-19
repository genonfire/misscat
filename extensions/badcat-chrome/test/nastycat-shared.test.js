const test = require('node:test');
const assert = require('node:assert/strict');
const N = require('../nastycat-shared.js');

const PRURL = (n) => 'https://github.com/o/r/pull/' + n;
const S0 = Date.parse('2026-06-01T00:00:00Z');
const link = (pr, repo = 'o/r') => ({ repo, pr });
const answer = (merged, at = null) => ({ merged, merged_at: merged ? at || '2026-06-01T00:00:10Z' : null });
const fresh = () => N.newSession(7, 'session_abc', '#860 A 진행해', 'arm1', S0);
const key = (pr) => N.prKey('o/r', pr);
const step = (s, links, results, now = S0 + 1, id = 'd' + Math.random()) => N.decide(s, links, results, now, id);

test('parses only exact GitHub PR links and Claude session URLs', () => {
  assert.deepEqual(N.parsePrUrl('https://github.com/neverworkalone/typewriter/pull/954'), { repo: 'neverworkalone/typewriter', pr: 954 });
  assert.deepEqual(N.parsePrUrl('https://github.com/o/r/pull/5/?x=1#c'), { repo: 'o/r', pr: 5 });
  for (const bad of ['http://github.com/o/r/pull/1', 'https://evil.com/o/r/pull/1', 'https://github.com.evil.com/o/r/pull/1',
    'https://github.com/o/r/pull/0', 'https://github.com/o/r/pull/01', 'https://github.com/o/r/pull/1/files',
    'https://github.com/o/r/issues/1', 'https://user@github.com/o/r/pull/1', 'https://github.com/-o/r/pull/1',
    'https://github.com/o/../pull/1', 'https://github.com/o/r/pull/99999999999999999999', 'junk', null]) {
    assert.equal(N.parsePrUrl(bad), null, String(bad));
  }
  assert.equal(N.parseClaudeSession('https://claude.ai/code/session_01AbC'), 'session_01AbC');
  for (const bad of ['https://claude.ai/', 'https://claude.ai/chat/abc', 'https://claude.ai/code', 'https://claude.ai/code/a/b',
    'http://claude.ai/code/abc', 'https://evil.com/code/abc', 'https://chatgpt.com/c/abc']) {
    assert.equal(N.parseClaudeSession(bad), null, bad);
  }
  assert.equal(N.isClaudeUrl('https://claude.ai/anything'), true);
  assert.equal(N.isClaudeUrl('https://chatgpt.com/'), false);
});

test('collectLinks dedupes (case-insensitively), keeps every PR and drops junk', () => {
  const links = N.collectLinks(['https://github.com/o/r/pull/954', 'https://github.com/O/R/pull/954', 'https://github.com/o/r/pull/957',
    'https://github.com/o/r/pull/954#x', 'nope', 5]);
  assert.deepEqual(links.map((l) => l.pr), [954, 957]);
});

test('the prompt is validated but never altered', () => {
  assert.equal(N.normalizePrompt('  #860 A 진행해 '), '  #860 A 진행해 ');
  assert.equal(N.normalizePrompt('   '), null);
  assert.equal(N.normalizePrompt('x'.repeat(N.MAX_PROMPT + 1)), null);
  assert.equal(N.normalizePrompt(5), null);
});

test('host reply parsing accepts only what was asked and requires merged_at for merges', () => {
  const asked = [link(1), link(2)];
  const msg = { type: 'result', id: 'a', prs: [
    { repo: 'O/R', pr: 1, merged: true, merged_at: '2026-01-01T00:00:00Z' },
    { repo: 'o/r', pr: 2, merged: true, merged_at: null },
    { repo: 'o/r', pr: 3, merged: false, merged_at: null }] };
  assert.deepEqual(Object.keys(N.parseHostResult(msg, 'a', asked)), [key(1)]);
  assert.equal(N.parseHostResult(msg, 'other', asked), null);
  assert.equal(N.parseHostResult({ error: 'x' }, 'a', asked), null);
});

test('startup: no PRs sends the prompt once; confirmation ends the kick-off', () => {
  let out = step(fresh(), [], {});
  assert.deepEqual(out.send.keys, [N.STARTUP]);
  const blocked = step(out.session, [], {});
  assert.equal(blocked.send, null); // a delivery is in flight: no simultaneous duplicate
  const done = N.settle(out.session, out.send.id, 'confirmed');
  assert.equal(done.startup, 'done');
  assert.equal(step(done, [], {}).send, null);
});

test('startup: all discovered PRs already merged is history plus one kick-off, never per-PR wakeups', () => {
  const results = { [key(1)]: answer(true), [key(2)]: answer(true) };
  const out = step(fresh(), [link(1), link(2)], results);
  assert.deepEqual(out.send.keys, [N.STARTUP]);
  const done = N.settle(out.session, out.send.id, 'confirmed');
  assert.equal(step(done, [link(1), link(2)], results).send, null);
});

test('startup: an open PR means no immediate send; its merge wakes once', () => {
  let out = step(fresh(), [link(1), link(2)], { [key(1)]: answer(true), [key(2)]: answer(false) });
  assert.equal(out.send, null);
  assert.equal(out.session.startup, 'none');
  out = step(out.session, [link(1), link(2)], { [key(2)]: answer(true) });
  assert.deepEqual(out.send.keys, [key(2)]);
  const done = N.settle(out.session, out.send.id, 'confirmed');
  assert.equal(step(done, [link(1), link(2)], {}).send, null);
});

test('an incomplete baseline never sends (fail closed) and is retried', () => {
  const out = step(fresh(), [link(1), link(2)], { [key(1)]: answer(true) });
  assert.equal(out.send, null);
  assert.equal(out.session.phase, 'baseline');
});

test('closed without merge does not trigger', () => {
  let out = step(fresh(), [link(1)], { [key(1)]: answer(false) });
  out = step(out.session, [link(1)], { [key(1)]: answer(false) });
  assert.equal(out.send, null);
});

test('merges out of PR-number order: a higher blocker PR first, then the lower main PR, each wake once', () => {
  let out = step(fresh(), [link(860), link(861)], { [key(860)]: answer(false), [key(861)]: answer(false) });
  const merged861 = step(out.session, [link(860), link(861)], { [key(860)]: answer(false), [key(861)]: answer(true) });
  assert.deepEqual(merged861.send.keys, [key(861)]);
  let s = N.settle(merged861.session, merged861.send.id, 'confirmed');
  const merged860 = step(s, [link(860), link(861)], { [key(860)]: answer(true) });
  assert.deepEqual(merged860.send.keys, [key(860)]);
});

test('several merges in one poll produce one send and are marked together', () => {
  let out = step(fresh(), [link(1), link(2)], { [key(1)]: answer(false), [key(2)]: answer(false) });
  out = step(out.session, [link(1), link(2)], { [key(1)]: answer(true), [key(2)]: answer(true) });
  assert.deepEqual(out.send.keys, [key(1), key(2)]);
  const done = N.settle(out.session, out.send.id, 'confirmed');
  assert.equal(done.prs[key(1)].done && done.prs[key(2)].done, true);
  assert.equal(step(done, [link(1), link(2)], {}).send, null);
});

test('a new PR created and merged between polls is eligible via merged_at; old history is not', () => {
  const s = step(fresh(), [link(1)], { [key(1)]: answer(false) }).session;
  const stale = step(s, [link(1), link(2)], { [key(2)]: answer(true, '2026-05-31T00:00:00Z') }, S0 + 5);
  assert.equal(stale.send, null);
  const fresher = step(s, [link(1), link(3)], { [key(3)]: answer(true, new Date(S0 + 3).toISOString()) }, S0 + 5);
  assert.deepEqual(fresher.send.keys, [key(3)]);
});

test('a new open PR after ON is watched and wakes when it merges later', () => {
  let s = step(fresh(), [link(1)], { [key(1)]: answer(true) });
  s = N.settle(s.session, s.send.id, 'confirmed');
  s = step(s, [link(1), link(2)], { [key(2)]: answer(false) }).session;
  const out = step(s, [link(1), link(2)], { [key(2)]: answer(true) });
  assert.deepEqual(out.send.keys, [key(2)]);
});

test('unknown answers never register a new link', () => {
  const s = step(fresh(), [link(1)], { [key(1)]: answer(false) }).session;
  const out = step(s, [link(1), link(2)], {});
  assert.equal(out.session.prs[key(2)], undefined);
});

test('not_sent leaves the events due; uncertain blocks everything and surfaces an error', () => {
  const first = step(fresh(), [], {});
  const retry = N.settle(first.session, first.send.id, 'not_sent');
  assert.equal(retry.delivery, null);
  assert.ok(step(retry, [], {}).send);
  const unsure = N.settle(first.session, first.send.id, 'uncertain');
  assert.match(unsure.error, /could not be confirmed/);
  assert.equal(step(unsure, [], {}).send, null);
  assert.equal(N.settle(first.session, 'other-id', 'confirmed').delivery.id, first.send.id); // stale ids are ignored
});

test('two sessions are independent', () => {
  const a = N.newSession(1, 'sa', '#860 A 진행해', 'x', S0);
  const b = N.newSession(2, 'sb', '#860 B 진행해', 'y', S0);
  const out = step(a, [], {});
  assert.equal(out.session.prompt, '#860 A 진행해');
  assert.equal(b.delivery, null);
  assert.equal(b.prompt, '#860 B 진행해');
});

test('a long conversation never loses discovery of a new PR behind 200+ historical links', () => {
  const hrefs = [];
  for (let i = 1; i <= 260; i++) hrefs.push(PRURL(i));
  const links = N.collectLinks(hrefs);
  assert.equal(links.length, 260);
  const old = {};
  for (const l of links.slice(0, 259)) old[N.prKey(l.repo, l.pr)] = answer(true, '2020-01-01T00:00:00Z');
  let s = step(fresh(), links.slice(0, 259), old).session;
  s = N.settle(s, s.delivery.id, 'confirmed');
  assert.deepEqual(N.keysToCheck(s, links).map((l) => l.pr), [260]); // only the new one is asked
  const out = step(s, links, { [key(260)]: answer(true, new Date(S0 + 9).toISOString()) }, S0 + 20);
  assert.deepEqual(out.send.keys, [key(260)]);
});

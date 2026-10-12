// Run with: node --test extensions/badcat-chrome/test/   (no install, no dependencies)
const test = require('node:test');
const assert = require('node:assert/strict');
const S = require('../shared.js');

const HEAD = 'a'.repeat(40);

test('normalizeRepo accepts owner/repo and rejects everything else', () => {
  assert.equal(S.normalizeRepo(' neverworkalone/typewriter '), 'neverworkalone/typewriter');
  for (const bad of ['', 'a', 'a/b/c', '-a/b', 'a/..', 'a/.', 'a b/c', 'a/b\nc', 'a_b/c', '/', 'a/', null, 5, {}]) {
    assert.equal(S.normalizeRepo(bad), null, JSON.stringify(bad));
  }
});

test('isConversationUrl only accepts https://chatgpt.com conversations', () => {
  assert.ok(S.isConversationUrl('https://chatgpt.com/c/68a1-b2'));
  assert.ok(S.isConversationUrl('https://chatgpt.com/g/g-p-123-proj/c/68a1-b2'));
  for (const bad of ['https://chatgpt.com/', 'https://chatgpt.com/c/', 'http://chatgpt.com/c/1',
    'https://evil.com/c/1', 'https://chatgpt.com.evil.com/c/1', 'https://example.com/?u=chatgpt.com/c/1',
    'not a url', undefined]) {
    assert.equal(S.isConversationUrl(bad), false, String(bad));
  }
});

test('parseEvent accepts only a well-formed +1 for the watched repo', () => {
  const ok = { repo: 'Neverworkalone/typewriter', pr: 123, head: HEAD, status: '+1', review_id: 100, delivery_id: 'c'.repeat(32) };
  assert.deepEqual(S.parseEvent(ok, 'neverworkalone/typewriter'),
    { repo: 'neverworkalone/typewriter', pr: 123, head: HEAD, status: '+1', review_id: 100, delivery_id: 'c'.repeat(32) });
  const bad = [
    { ...ok, status: '+2' }, { ...ok, status: '-1' }, { ...ok, status: undefined },
    { ...ok, repo: 'other/repo' }, { ...ok, repo: 'a/b/c' },
    { ...ok, pr: '123' }, { ...ok, pr: 0 }, { ...ok, pr: -1 }, { ...ok, pr: 1.5 }, { ...ok, pr: 2 ** 60 },
    { ...ok, head: 'abc' }, { ...ok, head: HEAD + 'a' }, { ...ok, head: 'g'.repeat(40) },
    ...[undefined, null, 0, -1, 1.2, '100', true, 2 ** 60].map(review_id => ({ ...ok, review_id })),
    { ...ok, delivery_id: undefined }, { ...ok, head: HEAD.toUpperCase() },
    { ...ok, head: HEAD + '\n' }, { ...ok, delivery_id: 'c'.repeat(32) + '\n' },
    { ...ok, repo: ' Neverworkalone/typewriter ' },
    { ...ok, legacy_review_id: "100" }, { ...ok, legacy_review_id: -1 },
    { error: 'boom' }, [], null, 'x', 7, undefined,
  ];
  for (const message of bad) {
    assert.equal(S.parseEvent(message, 'neverworkalone/typewriter'), null, JSON.stringify(message));
  }
});

test('normalized events can be validated again across script boundaries', () => {
  const raw = { repo: 'O/R', pr: 558, head: HEAD, status: '+1',
    review_id: 5481963549, delivery_id: 'c'.repeat(32), legacy_review_id: 100 };
  const normalized = S.parseEvent(raw, 'o/r');
  assert.deepEqual(S.parseEvent(normalized, 'o/r'), normalized);
});

test('reviewMessage is fixed text built from a validated integer', () => {
  assert.equal(S.reviewMessage(123), '현재 작업 끝났으면 PR #123 리뷰해.');
  for (const bad of ['1; rm -rf', '12', 0, -3, 1.2, NaN, null]) {
    assert.throws(() => S.reviewMessage(bad), /invalid PR number/);
  }
});

test('eventKey separates PR and HEAD', () => {
  const a = { repo: 'o/r', pr: 1, head: 'a'.repeat(40), review_id: 1 };
  assert.notEqual(S.eventKey(a), S.eventKey({ ...a, review_id: 2 }));
  assert.notEqual(S.eventKey(a), S.eventKey({ ...a, head: 'b'.repeat(40) }));
  assert.notEqual(S.eventKey(a), S.eventKey({ ...a, pr: 2 }));
  assert.equal(S.eventKey(a), S.eventKey({ ...a, repo: 'O/R' }));
});

test('isNewChatUrl accepts only the chatgpt.com root; projectOf reads the project slug', () => {
  assert.ok(S.isNewChatUrl('https://chatgpt.com/'));
  for (const bad of ['https://chatgpt.com/c/1', 'https://chatgpt.com/g/p/c/1', 'http://chatgpt.com/', 'https://evil.com/',
    'https://chatgpt.com.evil.com/', 'nope', undefined]) {
    assert.equal(S.isNewChatUrl(bad), false, String(bad));
  }
  assert.equal(S.projectOf('https://chatgpt.com/g/g-p-123-proj/c/68a1'), 'g-p-123-proj');
  assert.equal(S.projectOf('https://chatgpt.com/c/68a1'), null);
  assert.equal(S.projectOf('nope'), null);
});

test('projectHomeSlug reads only /g/<slug>/project on https://chatgpt.com', () => {
  assert.equal(S.projectHomeSlug('https://chatgpt.com/g/g-p-1-proj/project'), 'g-p-1-proj');
  assert.equal(S.projectHomeSlug('https://chatgpt.com/g/g-p-1-proj/project/'), 'g-p-1-proj');
  for (const bad of ['https://chatgpt.com/g/g-p-1-proj/c/1', 'https://chatgpt.com/', 'https://chatgpt.com/g/x/project/y',
    'https://evil.com/g/x/project', 'http://chatgpt.com/g/x/project', 'nope', undefined]) {
    assert.equal(S.projectHomeSlug(bad), null, String(bad));
  }
});

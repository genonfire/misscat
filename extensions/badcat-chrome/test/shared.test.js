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
  const ok = { repo: 'Neverworkalone/typewriter', pr: 123, head: HEAD.toUpperCase(), status: '+1' };
  assert.deepEqual(S.parseEvent(ok, 'neverworkalone/typewriter'),
    { repo: 'Neverworkalone/typewriter', pr: 123, head: HEAD });
  const bad = [
    { ...ok, status: '+2' }, { ...ok, status: '-1' }, { ...ok, status: undefined },
    { ...ok, repo: 'other/repo' }, { ...ok, repo: 'a/b/c' },
    { ...ok, pr: '123' }, { ...ok, pr: 0 }, { ...ok, pr: -1 }, { ...ok, pr: 1.5 }, { ...ok, pr: 2 ** 60 },
    { ...ok, head: 'abc' }, { ...ok, head: HEAD + 'a' }, { ...ok, head: 'g'.repeat(40) },
    { error: 'boom' }, [], null, 'x', 7, undefined,
  ];
  for (const message of bad) {
    assert.equal(S.parseEvent(message, 'neverworkalone/typewriter'), null, JSON.stringify(message));
  }
});

test('reviewMessage is fixed text built from a validated integer', () => {
  assert.equal(S.reviewMessage(123), 'PR #123 리뷰해');
  for (const bad of ['1; rm -rf', '12', 0, -3, 1.2, NaN, null]) {
    assert.throws(() => S.reviewMessage(bad), /invalid PR number/);
  }
});

test('eventKey separates PR and HEAD', () => {
  const a = { repo: 'o/r', pr: 1, head: 'a'.repeat(40) };
  assert.notEqual(S.eventKey(a), S.eventKey({ ...a, head: 'b'.repeat(40) }));
  assert.notEqual(S.eventKey(a), S.eventKey({ ...a, pr: 2 }));
  assert.equal(S.eventKey(a), S.eventKey({ ...a, repo: 'O/R' }));
});

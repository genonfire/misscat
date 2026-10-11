"""Same-HEAD re-review when the PR author answers a blocking -1."""
import dataclasses
import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

import misscat as m

REPO = "genonfire/typewriter"
HEAD = "a" * 40
AUTHOR = "alice"
T0 = datetime(2026, 10, 1, 12, 0, 0, tzinfo=timezone.utc)


def ts(minutes):
    return (T0 + timedelta(minutes=minutes)).isoformat().replace("+00:00", "Z")


def marker(verdict, head=HEAD, evidence="because"):
    return f"{verdict}\nHEAD: {head}\n{evidence}"


def review(id, minutes, verdict="-1", head=HEAD, login="reviewer", state="COMMENTED", body=None):
    return m.GhReview(id, login, state, marker(verdict, head) if body is None else body, head, ts(minutes))


def comment(id, minutes, login=AUTHOR):
    return m.GhComment(id, login, ts(minutes), f"https://example/c/{id}")


class FakeDiscussion:
    def __init__(self, reviews=(), comments=(), head=HEAD):
        self.review_list, self.comment_list, self.head_sha = list(reviews), list(comments), head
        self.review_calls = self.comment_calls = 0
        self.fail = False

    def reviews(self, number):
        self.review_calls += 1
        if self.fail:
            raise m.GhError("boom")
        return list(self.review_list)

    def comments(self, number):
        self.comment_calls += 1
        if self.fail:
            raise m.GhError("boom")
        return list(self.comment_list)

    def head(self, number):
        if self.fail:
            raise m.GhError("boom")
        return self.head_sha


class ReplyBase(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        for name, value in (("CONFIG_DIR", root / "config"), ("LOCK_ROOT", root / "locks")):
            patcher = mock.patch.object(m, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.cfg = m.load_settings(None)
        self.state = m.State(m.state_path(REPO))
        self.key = m.Key(7, HEAD, "luna")
        self.state.add(self.key)
        # reviewed_at is "now"; pin it next to the fake timeline
        self.state._records = [m.ReviewRecord(7, HEAD, "luna", ts(5))]
        self.pr = m.PR(7, HEAD, False, "https://example/pr/7", "t", author=AUTHOR, updated_at="u1")
        self.calls = []
        self.sleeps = []

    def watcher(self, discussion, prs=None, reviewer=None, profile="luna"):
        prs = [self.pr] if prs is None else prs
        reviewer = reviewer or (lambda s, r, pr, ws: self.calls.append(pr) or True)
        return m.Watcher(REPO, profile, self.cfg, self.state, lambda repo: prs, reviewer,
                         sleep=self.sleeps.append, discussion=discussion)

    def blocked(self, **kw):
        return FakeDiscussion([review(1, 4)], [comment(10, 6)], **kw)


class TriggerTests(ReplyBase):
    def test_author_comment_after_blocking_review_triggers_exactly_once(self):
        d = self.blocked()
        w = self.watcher(d)
        w.cycle()  # review is not submitted by the fake reviewer: verification fails
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(self.calls[0].reply.comment_ids, (10,))
        self.assertIn("https://example/c/10", m.build_prompt(self.cfg, REPO, self.calls[0]))

    def run_verified(self, d, w):
        def reviewer(s, r, pr, ws):
            self.calls.append(pr)
            d.review_list.append(review(2, 8))  # the reviewer submits a new -1
            return True
        w.reviewer = reviewer
        w.cycle()

    def test_verified_run_records_comment_and_does_not_repeat(self):
        d = self.blocked()
        w = self.watcher(d)
        self.run_verified(d, w)
        self.assertEqual(len(self.calls), 1)
        self.assertIn(m.ReplyKey(7, HEAD, "luna", 10), self.state.handled())
        self.pr = m.PR(7, HEAD, False, "u", "t", author=AUTHOR, updated_at="u2")
        w2 = self.watcher(d, [self.pr])
        w2.cycle()
        w2.cycle()
        self.assertEqual(len(self.calls), 1)

    def test_two_successive_replies_give_two_reviews_same_sha(self):
        d = self.blocked()
        self.run_verified(d, self.watcher(d))  # reply 10 answered by -1 (review 2, +8)
        d.comment_list.append(comment(11, 13))  # a new reply to that new -1
        w = self.watcher(d, [m.PR(7, HEAD, False, "u", "t", author=AUTHOR, updated_at="u3")])

        def reviewer(s, r, pr, ws):
            self.calls.append(pr)
            d.review_list.append(review(3, 15))
            return True
        w.reviewer = reviewer
        w.cycle()
        self.assertEqual([c.reply.comment_ids for c in self.calls], [(10,), (11,)])
        self.assertEqual({k.comment_id for k in self.state.handled()}, {10, 11})

    def test_same_login_reviewer_review_does_not_self_trigger(self):
        d = FakeDiscussion([review(1, 4, login=AUTHOR)], [comment(10, 3)])
        self.watcher(d).cycle()
        self.assertEqual(self.calls, [])

    def test_no_trigger_cases(self):
        cases = {
            "non-author": FakeDiscussion([review(1, 4)], [comment(10, 6, login="bob")]),
            "comment before -1": FakeDiscussion([review(1, 4)], [comment(10, 3)]),
            "no blocking review": FakeDiscussion([review(1, 4, "+1")], [comment(10, 6)]),
            "pass after -1": FakeDiscussion([review(1, 4), review(2, 5, "+1")], [comment(10, 6)]),
            "old head -1": FakeDiscussion([review(1, 4, head="b" * 40)], [comment(10, 6)]),
            "no evidence": FakeDiscussion([review(1, 4, body=f"-1\nHEAD: {HEAD}\n  ")], [comment(10, 6)]),
            "not a marker": FakeDiscussion([review(1, 4, body="-1 : FAIL")], [comment(10, 6)]),
            "pending review": FakeDiscussion([review(1, 4, state="PENDING")], [comment(10, 6)]),
            "newer -1 after reply": FakeDiscussion([review(1, 4), review(2, 7)], [comment(10, 6)]),
        }
        for name, d in cases.items():
            with self.subTest(name):
                self.watcher(d).cycle()
                self.assertEqual(self.calls, [], name)

    def test_later_pass_prevents_trigger_from_earlier_comments(self):
        d = FakeDiscussion([review(1, 4), review(2, 8, "+2")], [comment(10, 6)])
        self.watcher(d).cycle()
        self.assertEqual(self.calls, [])

    def test_other_profiles_blocking_review_is_not_reopened(self):
        # luna reviewed at +5; a later stage's -1 at +30 is not luna's to re-review
        d = FakeDiscussion([review(1, 30)], [comment(10, 31)])
        self.watcher(d).cycle()
        self.assertEqual(self.calls, [])

    def test_unreviewed_head_gets_normal_review_without_discussion_lookup(self):
        d = self.blocked()
        pr = m.PR(7, "c" * 40, False, "u", "t", author=AUTHOR, updated_at="u1")
        self.watcher(d, [pr]).cycle()
        self.assertEqual(len(self.calls), 1)
        self.assertIsNone(self.calls[0].reply)
        self.assertEqual(d.review_calls, 0)

    def test_no_review_label_and_draft_filtering_still_apply(self):
        d = self.blocked()
        self.cfg = dataclasses.replace(self.cfg, include_drafts=False)
        for pr in (m.PR(7, HEAD, False, "u", "t", no_review=True, author=AUTHOR, updated_at="u1"),
                   m.PR(7, HEAD, True, "u", "t", author=AUTHOR, updated_at="u1")):
            self.watcher(d, [pr]).cycle()
        self.assertEqual(self.calls, [])
        self.assertEqual(d.review_calls, 0)

    def test_urgent_reply_goes_first(self):
        self.state.add(m.Key(8, "d" * 40, "luna"))
        d = self.blocked()
        w = self.watcher(d)
        w.cats = 2
        prs = [m.PR(7, HEAD, False, "u", "t", author=AUTHOR, updated_at="u1", urgent=True),
               m.PR(9, "e" * 40, False, "u", "t")]
        self.assertEqual([p.number for p in w._next_batch(prs)], [7, 9])
        w.cats = 1
        self.assertEqual([p.number for p in w._next_batch(prs)], [7])


class VerificationTests(ReplyBase):
    def test_exit_zero_without_review_is_not_recorded_and_retries(self):
        d = self.blocked()
        w = self.watcher(d)
        w.cycle()
        self.assertEqual(self.state.handled(), set())
        self.assertEqual(self.sleeps, [m.FAILURE_WAIT])
        w.cycle()  # same updatedAt, but never cached as "no reply due"
        self.assertEqual(len(self.calls), 2)

    def test_verification_api_failure_and_head_race_are_failures(self):
        d = self.blocked()
        w = self.watcher(d)

        def flaky(s, r, pr, ws):
            d.review_list.append(review(2, 8))
            d.fail = True
            return True
        w.reviewer = flaky
        with self.assertLogs("misscat", "ERROR"):
            w.cycle()
        self.assertEqual(self.state.handled(), set())
        d.fail = False
        d.review_list.pop()
        d.head_sha = "f" * 40

        def race(s, r, pr, ws):
            d.review_list.append(review(3, 9))
            return True
        w.reviewer = race
        with self.assertLogs("misscat", "WARNING"):
            w.cycle()
        self.assertEqual(self.state.handled(), set())

    def test_reviewer_failure_is_not_recorded(self):
        d = self.blocked()
        self.watcher(d, reviewer=lambda *a: False).cycle()
        self.assertEqual(self.state.handled(), set())

    def test_review_of_another_head_does_not_count(self):
        d = self.blocked()
        w = self.watcher(d)

        def wrong(s, r, pr, ws):
            d.review_list.append(review(2, 8, head="b" * 40))
            return True
        w.reviewer = wrong
        with self.assertLogs("misscat", "ERROR"):
            w.cycle()
        self.assertEqual(self.state.handled(), set())


class PollingTests(ReplyBase):
    def test_lookup_only_when_updated_at_changes(self):
        d = FakeDiscussion([review(1, 4)], [])
        w = self.watcher(d)
        w.cycle()
        w.cycle()
        self.assertEqual((d.review_calls, d.comment_calls), (1, 1))
        w2 = self.watcher(d, [m.PR(7, HEAD, False, "u", "t", author=AUTHOR, updated_at="u2")])
        w2._checked = dict(w._checked)
        w2.cycle()
        self.assertEqual(d.review_calls, 2)

    def test_comments_not_fetched_unless_blocked(self):
        d = FakeDiscussion([review(1, 4, "+1")], [comment(10, 6)])
        self.watcher(d).cycle()
        self.assertEqual((d.review_calls, d.comment_calls), (1, 0))

    def test_lookup_failure_is_retried_next_cycle(self):
        d = self.blocked()
        d.fail = True
        w = self.watcher(d)
        with self.assertLogs("misscat", "WARNING"):
            w.cycle()
        d.fail = False
        w.cycle()
        self.assertEqual(len(self.calls), 1)

    def test_unknown_author_or_disabled_discussion_never_looks(self):
        d = self.blocked()
        pr = m.PR(7, HEAD, False, "u", "t", updated_at="u1")
        self.watcher(d, [pr]).cycle()
        self.watcher(None).cycle()
        self.assertEqual((self.calls, d.review_calls), ([], 0))


class StateTests(ReplyBase):
    def test_replies_persist_and_old_v2_files_load(self):
        self.state.add_replies([m.ReplyKey(7, HEAD, "luna", 10)])
        again = m.State(m.state_path(REPO))
        self.assertEqual(again.handled(), {m.ReplyKey(7, HEAD, "luna", 10)})
        self.assertIsNotNone(again.handled_time(self.key))
        data = json.loads(m.state_path(REPO).read_text())
        data.pop("replies")
        m.state_path(REPO).write_text(json.dumps(data))
        legacy = m.State(m.state_path(REPO))
        self.assertEqual(legacy.handled(), set())
        self.assertEqual(legacy.reviewed(), {self.key})  # history is kept, nothing re-reviewed

    def test_malformed_replies_fail_safely(self):
        path = m.state_path(REPO)
        data = json.loads(path.read_text())
        data["replies"] = [{"pr": 7, "head": HEAD, "profile": None, "comment_id": "x",
                            "handled_at": ts(1)}]
        path.write_text(json.dumps(data))
        with self.assertRaises(m.StateError):
            m.State(path)

    def test_removing_a_review_keeps_replies(self):
        self.state.add_replies([m.ReplyKey(7, HEAD, "luna", 10)])
        self.state._records = [m.ReviewRecord(7, HEAD, "luna", ts(5))]
        self.assertTrue(self.state.remove_latest(self.key))
        self.assertEqual(len(m.State(m.state_path(REPO)).handled()), 1)


class GhTests(unittest.TestCase):
    def test_listing_requests_author_and_updated_at(self):
        out = json.dumps([{"number": 1, "headRefOid": "a", "isDraft": False, "url": "u",
                           "title": "t", "labels": [], "author": {"login": "Alice"},
                           "updatedAt": "2026-10-01T00:00:00Z"}])
        proc = mock.Mock(returncode=0, stdout=out, stderr="")
        with mock.patch.object(m.subprocess, "run", return_value=proc) as run:
            prs = m.gh_open_prs(REPO)
        self.assertIn("author,updatedAt", run.call_args[0][0][-1])
        self.assertEqual((prs[0].author, prs[0].updated_at), ("Alice", "2026-10-01T00:00:00Z"))

    def test_discussion_parses_reviews_comments_and_paginates(self):
        gh = m.GhDiscussion(REPO)
        pages = [[{"id": i, "user": {"login": "u"}, "created_at": ts(0), "html_url": "h"}
                  for i in range(100)], [{"id": 100, "user": None, "created_at": ts(0)}]]
        with mock.patch.object(gh, "_api", side_effect=pages) as api:
            comments = gh.comments(5)
        self.assertEqual(len(comments), 101)
        self.assertEqual(api.call_args_list[0][0][0], f"repos/{REPO}/issues/5/comments?per_page=100&page=1")
        with mock.patch.object(gh, "_api", return_value=[{"id": 1, "user": {"login": "r"},
                "state": "COMMENTED", "body": None, "commit_id": HEAD, "submitted_at": ts(1)}]):
            self.assertEqual(gh.reviews(5)[0].body, "")
        with mock.patch.object(gh, "_api", return_value=[{"bad": 1}]):
            with self.assertRaises(m.GhError):
                gh.reviews(5)


if __name__ == "__main__":
    unittest.main()

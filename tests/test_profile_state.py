"""Completion is per (PR, HEAD), whichever profile ran; verified review ids are never dropped."""
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import misscat as m
import test_replies as tr

REPO = tr.REPO
HEAD = tr.HEAD
OTHER = "c" * 40


def rows(*items):
    return [{"pr": 7, "head": HEAD, "profile": p, "reviewed_at": t, **({"review_ids": ids} if ids else {})}
            for p, t, ids in items]


class StateFileBase(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        for name, value in (("CONFIG_DIR", root / "config"), ("LOCK_ROOT", root / "locks")):
            patcher = mock.patch.object(m, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.path = m.state_path(REPO)

    def write(self, reviewed, replies=None):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        data = {"version": 2, "reviewed": reviewed}
        if replies:
            data["replies"] = replies
        self.path.write_text(json.dumps(data))

    def stored(self):
        return json.loads(self.path.read_text())


class LegacyRowsTests(StateFileBase):
    def mixed(self):
        self.write(
            rows(("luna", "2026-10-01T10:00:00Z", [3]), ("mscat/opencode", "2026-10-01T11:00:00Z", [4])),
            [{"pr": 7, "head": HEAD, "profile": "luna", "comment_id": 10,
              "handled_at": "2026-10-01T12:00:00Z", "review_id": 5}])

    def test_different_profile_rows_of_one_head_load_as_one_review(self):
        self.mixed()
        state = m.State(self.path)
        self.assertEqual(state.reviewed(), {m.Key(7, HEAD)})
        self.assertEqual(len(state.records()), 2)
        self.assertEqual(state.review_ids(m.Key(7, HEAD)), {3, 4, 5})
        self.assertEqual(state.handled(), {m.ReplyKey(7, HEAD, 10)})

    def test_rows_survive_rewrite(self):
        self.mixed()
        state = m.State(self.path)
        state.add_pending(m.PendingRun(7, HEAD, "sol", "rid"))
        data = self.stored()
        self.assertEqual({r["profile"] for r in data["reviewed"]}, {"luna", "mscat/opencode"})
        self.assertEqual(len(data["replies"]), 1)
        self.assertEqual(data["version"], 2)

    def test_identical_duplicate_row_is_corruption(self):
        self.write(rows(("luna", "2026-10-01T10:00:00Z", [3]), ("luna", "2026-10-01T11:00:00Z", [4])))
        with self.assertRaises(m.StateError):
            m.State(self.path)

    def test_identical_duplicate_reply_is_corruption(self):
        reply = {"pr": 7, "head": HEAD, "profile": "luna", "comment_id": 10,
                 "handled_at": "2026-10-01T12:00:00Z", "review_id": 5}
        self.write(rows(("luna", "2026-10-01T10:00:00Z", None)), [reply, dict(reply)])
        with self.assertRaises(m.StateError):
            m.State(self.path)
        self.write(rows(("luna", "2026-10-01T10:00:00Z", None)), [reply, {**reply, "profile": "sol"}])
        self.assertEqual(len(m.State(self.path).handled()), 1)


class KeepIdsTests(StateFileBase):
    def setUp(self):
        super().setUp()
        self.write(rows(("luna", "2026-10-01T10:00:00Z", [1])))
        self.state = m.State(self.path)
        self.key = m.Key(7, HEAD)

    def test_other_profile_gets_its_own_row_and_luna_is_untouched(self):
        self.state.add(self.key, "sol", (2,))
        self.assertEqual(self.state.reviewed(), {self.key})
        for state in (self.state, m.State(self.path)):
            by = {r.profile: r for r in state.records()}
            self.assertEqual(by["luna"].review_ids, (1,))
            self.assertEqual(by["luna"].reviewed_at, "2026-10-01T10:00:00Z")
            self.assertEqual(by["sol"].review_ids, (2,))
            self.assertEqual(state.review_ids(self.key), {1, 2})

    def test_same_profile_merges_without_a_new_row(self):
        self.state.add(self.key, "luna", (2,))
        reloaded = m.State(self.path)
        self.assertEqual(len(reloaded.records()), 1)
        self.assertEqual(reloaded.records()[0].review_ids, (1, 2))
        self.assertEqual(reloaded.records()[0].reviewed_at, "2026-10-01T10:00:00Z")

    def test_no_new_ids_writes_nothing(self):
        self.state.add_replies([m.ReplyKey(7, HEAD, 10)], "luna", 1)
        with mock.patch.object(self.state, "_write") as write:
            self.state.add(self.key, "sol", (1,))
            self.state.add(self.key, "sol")
            self.state.add_replies([m.ReplyKey(7, HEAD, 10)], "sol", 0)
        write.assert_not_called()

    def test_handled_comment_is_not_recorded_again_but_id_is_kept(self):
        self.state.add_replies([m.ReplyKey(7, HEAD, 10)], "luna", 2)
        self.state.add_replies([m.ReplyKey(7, HEAD, 10)], "sol", 3)
        reloaded = m.State(self.path)
        self.assertEqual(len(self.stored()["replies"]), 1)
        self.assertEqual(reloaded.review_ids(self.key), {1, 2, 3})
        with mock.patch.object(reloaded, "_write") as write:
            reloaded.add_replies([m.ReplyKey(7, HEAD, 10)], "sol", 3)
        write.assert_not_called()


class LateEvidenceTests(StateFileBase):
    def test_late_evidence_does_not_reorder_latest_head(self):
        self.write([
            {"pr": 7, "head": "A", "profile": "luna", "reviewed_at": "2026-10-01T09:00:00Z", "review_ids": [1]},
            {"pr": 7, "head": "B", "profile": "luna", "reviewed_at": "2026-10-01T10:00:00Z"},
        ])
        state = m.State(self.path)
        state.add(m.Key(7, "A"), "sol", (2,))  # late recovery of A's pending, "now" > 10:00
        for st in (state, m.State(self.path)):
            self.assertEqual(st.review_ids(m.Key(7, "A")), {1, 2})
            self.assertTrue(st.is_latest(m.Key(7, "B")))
            self.assertFalse(st.is_latest(m.Key(7, "A")))
            self.assertFalse(st.remove_latest(m.Key(7, "A")))
        by = {r.profile: r for r in state.records() if r.head == "A"}
        self.assertEqual(by["sol"].reviewed_at, "2026-10-01T09:00:00Z")

    def test_deleted_head_is_not_resurrected_by_pending_recovery(self):
        reply = {"pr": 7, "head": HEAD, "profile": "luna", "comment_id": 10,
                 "handled_at": "2026-10-01T12:00:00Z", "review_id": 5}
        self.write(rows(("luna", "2026-10-01T10:00:00Z", [1])), [reply])
        state = m.State(self.path)
        self.assertTrue(state.remove_latest(m.Key(7, HEAD)))
        state.add_replies([m.ReplyKey(7, HEAD, 10)], "sol", 6)
        self.assertEqual(state.reviewed(), set())
        self.assertEqual(state.records(), [])
        self.assertIn(6, state.review_ids(m.Key(7, HEAD)))
        # the next normal review of the HEAD records it once, and the file stays loadable
        state.add(m.Key(7, HEAD), "luna", (7,))
        reloaded = m.State(self.path)
        self.assertEqual(reloaded.reviewed(), {m.Key(7, HEAD)})
        self.assertEqual(reloaded.review_ids(m.Key(7, HEAD)), {5, 6, 7})
        self.assertEqual(len(reloaded.records()), 1)

    def test_deleted_head_same_profile_reply_row_cannot_hold_another_id(self):
        reply = {"pr": 7, "head": HEAD, "profile": "luna", "comment_id": 10,
                 "handled_at": "2026-10-01T12:00:00Z", "review_id": 5}
        self.write(rows(("luna", "2026-10-01T10:00:00Z", None)), [reply])
        state = m.State(self.path)
        state.remove_latest(m.Key(7, HEAD))
        with self.assertLogs("misscat", "WARNING"):
            state.add_replies([m.ReplyKey(7, HEAD, 10)], "luna", 6)
        self.assertEqual(state.reviewed(), set())
        m.State(self.path)  # still loads


class StateManagerTests(StateFileBase):
    def test_remove_latest_clears_all_profiles_and_keeps_replies(self):
        self.write(
            rows(("luna", "2026-10-01T10:00:00Z", [1]), ("sol", "2026-10-01T11:00:00Z", [2])),
            [{"pr": 7, "head": HEAD, "profile": "luna", "comment_id": 10,
              "handled_at": "2026-10-01T12:00:00Z", "review_id": 5}])
        state = m.State(self.path)
        self.assertTrue(state.is_latest(m.Key(7, HEAD)))
        self.assertTrue(state.remove_latest(m.Key(7, HEAD)))
        self.assertEqual(state.records(), [])
        self.assertEqual(state.reviewed(), set())
        self.assertEqual(len(m.State(self.path).handled()), 1)

    def test_latest_is_per_pr_not_per_profile(self):
        self.write([
            {"pr": 7, "head": "old", "profile": "luna", "reviewed_at": "2026-10-01T10:00:00Z"},
            {"pr": 7, "head": "new", "profile": "sol", "reviewed_at": "2026-10-01T11:00:00Z"},
        ])
        state = m.State(self.path)
        self.assertFalse(state.is_latest(m.Key(7, "old")))
        self.assertFalse(state.remove_latest(m.Key(7, "old")))
        self.assertTrue(state.is_latest(m.Key(7, "new")))

    def test_ui_confirmation_names_rows_and_profiles(self):
        self.write(rows(("luna", "2026-10-01T10:00:00Z", None), ("sol", "2026-10-01T11:00:00Z", None),
                        (None, "2026-10-01T09:00:00Z", None)))
        state = m.State(self.path)
        captured = {}

        class FakeApp:
            def __init__(self, layout, key_bindings, **kw):
                captured["layout"], captured["keys"] = layout, key_bindings

            def run(self):
                pass

        with mock.patch("prompt_toolkit.application.Application", FakeApp):
            m.run_state_ui(state, REPO)
        control = captured["layout"].container.content
        text = lambda: "".join(t for _, t in control.text())
        handler = lambda key: next(b for b in captured["keys"].bindings if getattr(b.keys[0], "value", b.keys[0]) == key).handler
        handler("delete")(mock.Mock())
        self.assertIn("removes 3 record(s) (profiles: default, luna, sol) and makes this HEAD eligible again? [y/N]",
                      text())
        handler("y")(mock.Mock())
        self.assertIn("Deleted 3 record(s) for PR #7 / " + HEAD[:10], text())
        self.assertEqual(state.records(), [])


class ProfileSwitchWatcherTests(tr.ReplyBase):
    def test_author_reply_to_luna_is_reviewed_by_opencode_then_not_by_sol(self):
        d = self.blocked()
        self.run_verified(d, self.watcher(d, profile="opencode"))
        self.assertEqual(len(self.calls), 1)
        self.assertIn(m.ReplyKey(7, HEAD, 10), self.state.handled())
        self.assertEqual({r.profile for r in self.state._replies}, {"opencode"})
        self.watcher(d, [m.PR(7, HEAD, False, "u", "t", author=tr.AUTHOR, updated_at="u9")],
                     profile="sol").cycle()
        self.assertEqual(len(self.calls), 1)

    def run_verified(self, d, w):
        def reviewer(s, r, pr, ws):
            self.calls.append(pr)
            d.review_list.append(tr.review(2, 8, run=pr.run_id))
            return True
        w.reviewer = reviewer
        w.cycle()

    def test_legacy_reply_row_of_another_profile_counts_as_handled(self):
        self.state.add_replies([m.ReplyKey(7, HEAD, 10)], "luna", 1)
        self.watcher(self.blocked(), profile="opencode").cycle()
        self.assertEqual(self.calls, [])

    def test_pending_of_another_profile_is_settled(self):
        pr = m.PR(7, OTHER, False, "u", "t")
        self.state.add_pending(m.PendingRun(7, OTHER, "luna", "rid"))
        d = tr.FakeDiscussion([tr.review(4, 8, "+1", head=OTHER, run="rid")])
        self.watcher(d, [pr], profile="sol").cycle()
        self.assertIn(m.Key(7, OTHER), self.state.reviewed())
        self.assertEqual(self.state.pending(), [])
        self.assertEqual(self.calls, [])

    def test_other_profile_pending_on_completed_head_keeps_new_id(self):
        self.state.add_pending(m.PendingRun(7, HEAD, "sol", "rid"))
        d = tr.FakeDiscussion([tr.review(5, 9, "+1", run="rid")])
        self.watcher(d, profile="luna").cycle()
        reloaded = m.State(m.state_path(REPO))
        self.assertEqual(reloaded.review_ids(self.key), {1, 5})
        by = {r.profile: r for r in reloaded.records()}
        self.assertEqual(by["luna"].review_ids, (1,))
        self.assertEqual(by["luna"].reviewed_at, tr.ts(5))
        self.assertEqual(by["sol"].review_ids, (5,))
        self.assertEqual(reloaded.pending(), [])

    def test_handled_comment_still_keeps_newly_verified_review_id(self):
        self.state.add_replies([m.ReplyKey(7, HEAD, 10)], "luna", 2)
        self.state.add_pending(m.PendingRun(7, HEAD, "opencode", "rid", (10,)))
        d = tr.FakeDiscussion([tr.review(1, 4), tr.review(3, 9, run="rid")], [tr.comment(10, 6)])
        self.watcher(d, profile="sol").cycle()
        reloaded = m.State(m.state_path(REPO))
        self.assertEqual(len(reloaded._replies), 1)
        self.assertIn(3, reloaded.review_ids(self.key))
        self.assertEqual(self.calls, [])


if __name__ == "__main__":
    unittest.main()

"""BadCat: review protocol, merge gate, watcher, state, lock, packaging."""
import contextlib
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest import mock

import misscat
from badcat import cli
from badcat.github import GhClient, OpenPR
from badcat.protocol import (BLOCKED, INVALID, PLUS1, READY, WAITING, Review, evaluate,
                             parse_header)

A, B = "a" * 40, "b" * 40
ME = frozenset({"me"})


def body(marker, sha, evidence="looks fine"):
    return f"{marker}\nHEAD: {sha}\n\n{evidence}"


_ids = iter(range(1, 10_000))


def rev(marker, sha, commit=None, state="COMMENTED", login="me", at="2026-01-01T00:00:00Z", text=None):
    return Review(next(_ids), login, state, text if text is not None else body(marker, sha),
                  commit or sha, at)


def seq(*reviews):
    """Give reviews increasing submission times in the order given."""
    return [r.__class__(r.id, r.login, r.state, r.body, r.commit_id, f"2026-01-01T00:00:{i:02d}Z")
            for i, r in enumerate(reviews)]


class ProtocolTests(unittest.TestCase):
    def stage(self, reviews, head=A, trusted=ME):
        return evaluate(reviews, head, trusted).stage

    def test_pr287_history(self):
        self.assertEqual(self.stage(seq(rev("+1", A), rev("-1", A))), BLOCKED)
        old = seq(rev("+1", A), rev("-1", A))
        self.assertEqual(self.stage(old, head=B), WAITING)  # new HEAD clears old passes
        self.assertEqual(self.stage(seq(rev("-1", A), rev("+1", B), rev("+2", B)), head=B), READY)

    def test_pr288_invalid_first_line(self):
        r = rev(None, A, text=f"-1 : FAIL (blocker found)\nHEAD: {A}\n\nevidence")
        self.assertEqual(self.stage([r]), INVALID)
        self.assertEqual(self.stage(seq(rev("+1", A), r, rev("+2", A))), INVALID)

    def test_header_strictness_never_authorizes(self):
        bad = [
            "\n+1\nHEAD: %s\n\nx" % A,
            " +1\nHEAD: %s\n\nx" % A,
            "**+1**\nHEAD: %s\n\nx" % A,
            "+1\n\nHEAD: %s\n\nx" % A,
            "+1\nHEAD: %s\n\nx" % A.upper(),
            "+1\nHEAD: %s\n\nx" % A[:7],
            "+1\nHEAD: %s\n" % A,
            "+1\nHEAD: %s\n  \n" % A,
            "Review\n+1\nHEAD: %s\n\nx" % A,
            "+1\nHEAD: %s" % A,
        ]
        for text in bad:
            with self.subTest(text=text):
                self.assertNotEqual(self.stage(seq(rev(None, A, text=text), rev("+2", A))), READY)
        self.assertIsNone(parse_header("+3\nHEAD: %s\n\nx" % A))
        self.assertEqual(parse_header("+1\r\nHEAD: %s\r\n\r\nx" % A)[0], "+1")

    def test_sha_and_state_mismatches(self):
        self.assertNotEqual(self.stage(seq(rev("+1", A, commit=B), rev("+2", A, commit=B))), READY)
        self.assertEqual(self.stage(seq(rev("+1", B, commit=A))), INVALID)  # contradictory
        self.assertEqual(self.stage(seq(rev("+1", A, state="APPROVED"), rev("+2", A, state="APPROVED"))), WAITING)
        self.assertEqual(self.stage([rev("+1", A, at="")]), WAITING)  # not submitted

    def test_ordering_and_conflicts(self):
        self.assertEqual(self.stage(seq(rev("+2", A))), INVALID)
        self.assertEqual(self.stage(seq(rev("+2", A), rev("+1", A))), PLUS1)
        self.assertEqual(self.stage(seq(rev("+1", A))), PLUS1)
        self.assertEqual(self.stage(seq(rev("+1", A), rev("+2", A), rev("-1", A))), BLOCKED)
        self.assertEqual(self.stage(seq(rev("+1", A), rev("+2", A))), READY)

    def test_non_pass_reviews_after_pass_hold_the_head(self):
        passes = [rev("+1", A), rev("+2", A)]
        for state, text in (("COMMENTED", "## 1차 리뷰 — 변경 요청"), ("CHANGES_REQUESTED", "## 1차 리뷰 — 변경 요청"),
                            ("COMMENTED", ""), ("CHANGES_REQUESTED", body("+2", A))):
            with self.subTest(state=state, text=text[:8]):
                self.assertEqual(self.stage(seq(*passes, rev(None, A, state=state, text=text))), INVALID)
        # before the first +1, on an old HEAD, from APPROVE, or from an untrusted user: no hold
        self.assertEqual(self.stage(seq(rev(None, A, text="hi"), *passes)), READY)
        self.assertEqual(self.stage(seq(*passes, rev(None, B, state="CHANGES_REQUESTED", text="x"))), READY)
        self.assertEqual(self.stage(seq(*passes, rev(None, A, state="APPROVED", text="x"))), READY)
        self.assertEqual(self.stage(seq(*passes, rev(None, A, login="eve", text="x"))), READY)

    def test_tie_break_by_id(self):
        same = "2026-01-01T00:00:00Z"
        r1 = Review(1, "me", "COMMENTED", body("+1", A), A, same)
        r2 = Review(2, "me", "COMMENTED", body("+2", A), A, same)
        self.assertEqual(evaluate([r2, r1], A, ME).stage, READY)

    def test_untrusted_reviewer(self):
        reviews = seq(rev("+1", A, login="eve"), rev("+2", A, login="eve"))
        v = evaluate(reviews, A, ME)
        self.assertEqual(v.stage, WAITING)
        self.assertIn("eve", v.detail)
        self.assertEqual(evaluate(reviews, A, frozenset()).stage, WAITING)  # fail closed
        self.assertEqual(evaluate(reviews, A, frozenset({"eve"})).stage, READY)
        shared = seq(rev("+1", A, login="Me"), rev("+2", A, login="me"))  # same account is fine
        self.assertEqual(evaluate(shared, A, ME).stage, READY)


def run(name, head=A, conclusion="success", status="completed", app="github-actions", id=1):
    return {"name": name, "head_sha": head, "status": status, "conclusion": conclusion,
            "id": id, "app": {"slug": app}}


class FakeClient:
    def __init__(self, head=A, draft=False, reviews=None):
        self.head, self.draft = head, draft
        self.review_list = reviews if reviews is not None else seq(rev("+1", A), rev("+2", A))
        self.raw = {"state": "open", "merged": False, "mergeable": True, "mergeable_state": "clean",
                    "draft": draft, "head": {"sha": head}}
        self.runs = [run("Validate and test Typewriter", head)]
        self.workflows = []
        self.status = {"state": "pending", "total_count": 0, "sha": head}
        self.calls, self.fail = [], {}
        self.merge_error = None
        self.open = True

    def _maybe_fail(self, name):
        if name in self.fail:
            raise misscat.GhError(self.fail[name])

    def open_prs(self):
        self._maybe_fail("open_prs")
        return [OpenPR(7, self.head, self.draft, "u", "t")] if self.open else []

    def pr(self, n):
        self._maybe_fail("pr")
        return self.raw

    def reviews(self, n):
        self._maybe_fail("reviews")
        return self.review_list

    def check_runs(self, sha):
        return self.runs

    def workflow_runs(self, sha):
        self._maybe_fail("workflow_runs")
        return self.workflows

    def combined_status(self, sha):
        return self.status

    def merge(self, n, sha):
        self.calls.append(("merge", n, sha))
        if self.merge_error:
            raise misscat.GhError(self.merge_error)
        self.raw["merged"] = True
        self.open = False


class WatcherTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = Path(tmp.name)

    def watcher(self, client, merge=False):
        return cli.Watcher("o/r", client, cli.NotifyState(self.dir / "s.json"), ME, merge,
                           sleep=lambda s: None)

    def run_cycle(self, w):
        with self.assertLogs("badcat", level="DEBUG") as cm:
            logging_marker()
            delay = w.cycle()
        return delay, [m for m in cm.output if "dummy" not in m]

    def test_notify_only_never_writes_even_when_eligible(self):
        c = FakeClient()
        w = self.watcher(c)
        _, out = self.run_cycle(w)
        self.assertEqual(c.calls, [])
        self.assertTrue(any("ready to merge (notify only)" in m for m in out))

    def test_merge_mode_squash_with_head_guard_once(self):
        c = FakeClient()
        w = self.watcher(c, merge=True)
        _, out = self.run_cycle(w)
        self.assertEqual(c.calls, [("merge", 7, A)])
        self.assertTrue(any("merged (squash" in m for m in out))
        self.assertEqual(w.state.prs, {})

    def test_draft_and_non_draft_same_path_and_rejection_reported_once(self):
        for draft in (False, True):
            with self.subTest(draft=draft):
                c = FakeClient(draft=draft)
                c.raw["mergeable_state"] = "draft" if draft else "clean"
                c.merge_error = "Pull Request is still a draft (HTTP 405)"
                w = self.watcher(c, merge=True)
                _, out = self.run_cycle(w)
                self.assertTrue(any("merge rejected" in m for m in out))
                with self.assertLogs("badcat", level="DEBUG"):
                    logging_marker()
                    w.cycle()
                    w.cycle()
                self.assertEqual(len(c.calls), 1)  # no repeated requests while unchanged
                self.assertEqual(c.raw["draft"], draft)  # never toggled
                c.raw["draft"] = False  # state change -> one more attempt
                c.raw["mergeable_state"] = "has_hooks"
                with self.assertLogs("badcat", level="DEBUG"):
                    logging_marker()
                    w.cycle()
                self.assertEqual(len(c.calls), 2)

    def test_non_ready_never_merges(self):
        cases = {
            "plus2_only": seq(rev("+2", A)),
            "plus1_only": seq(rev("+1", A)),
            "blocked": seq(rev("+1", A), rev("+2", A), rev("-1", A)),
            "old_head": seq(rev("+1", B), rev("+2", B)),
            "untrusted": seq(rev("+1", A, login="eve"), rev("+2", A, login="eve")),
            "invalid": seq(rev("+1", A), rev(None, A, text="-1 : FAIL\nHEAD: %s\n\nx" % A), rev("+2", A)),
        }
        for name, reviews in cases.items():
            with self.subTest(name):
                c = FakeClient(reviews=reviews)
                self.run_cycle(self.watcher(c, merge=True))
                self.assertEqual(c.calls, [])

    def test_ci_and_mergeability_gates_fail_closed(self):
        def run(mutate):
            c = FakeClient()
            mutate(c)
            self.run_cycle(self.watcher(c, merge=True))
            return c.calls

        pending = lambda c: c.runs.__setitem__(0, {**c.runs[0], "status": "in_progress", "conclusion": None})
        failed = lambda c: c.runs.__setitem__(0, {**c.runs[0], "conclusion": "failure"})
        stale = lambda c: c.runs.__setitem__(0, {**c.runs[0], "head_sha": B})
        skipped = lambda c: c.runs.__setitem__(0, {**c.runs[0], "conclusion": "skipped"})
        missing = lambda c: c.runs.clear()
        bad_status = lambda c: c.status.update(state="failure", total_count=1)
        unknown = lambda c: c.raw.update(mergeable=None)
        conflict = lambda c: c.raw.update(mergeable=False, mergeable_state="dirty")
        blocked = lambda c: c.raw.update(mergeable_state="blocked")
        moved = lambda c: c.raw.update(head={"sha": B})
        for name, fn in dict(pending=pending, failed=failed, stale=stale, skipped=skipped,
                             missing=missing, bad_status=bad_status, unknown=unknown,
                             conflict=conflict, blocked=blocked, moved=moved).items():
            with self.subTest(name):
                self.assertEqual(run(fn), [])

    def test_head_race_merge_rejection_is_logged_and_watching_continues(self):
        c = FakeClient()
        c.merge_error = "Head branch was modified (HTTP 409)"
        w = self.watcher(c, merge=True)
        _, out = self.run_cycle(w)
        self.assertTrue(any("merge rejected" in m for m in out))
        self.assertIn("7", w.state.prs)

    def test_timeout_after_merge_is_not_a_failure_or_retry(self):
        c = FakeClient()
        c.merge_error = "timed out"
        orig = c.merge
        def merge_then_fail(n, sha):
            c.raw["merged"] = True
            orig(n, sha)
        c.merge = merge_then_fail
        w = self.watcher(c, merge=True)
        _, out = self.run_cycle(w)
        self.assertEqual(len(c.calls), 1)
        self.assertTrue(any("merged" in m for m in out))
        self.assertNotIn("7", w.state.prs)

    def test_first_sight_records_history_then_announces_new_reviews_by_first_line(self):
        first = seq(rev("+1", A))
        c = FakeClient(reviews=first)
        w = self.watcher(c)
        _, out1 = self.run_cycle(w)
        self.assertTrue(any("new (aaaaaaa)" in m for m in out1))
        self.assertFalse(any("+1" in m for m in out1))  # history is not replayed
        c.review_list = first + [
            Review(900, "me", "COMMENTED", body("+2", A), A, "2026-01-02T00:00:00Z")]
        _, out2 = self.run_cycle(w)
        self.assertEqual([m for m in out2 if "dummy" not in m and "ready" not in m],
                         ["INFO:badcat:PR #7: +2"])
        _, out3 = self.run_cycle(w)
        self.assertFalse(any("PR #7: +2" in m for m in out3))  # once only

    def test_nonconforming_reviews_print_literal_first_line_without_labels(self):
        c = FakeClient(reviews=[])
        w = self.watcher(c)
        self.run_cycle(w)
        extra = [
            Review(1, "me", "COMMENTED", f"-1 : FAIL (blocker found)\nHEAD: {A}\n\nx", A, "2026-01-01T00:00:01Z"),
            Review(2, "me", "CHANGES_REQUESTED", "## 1차 리뷰 — 변경 요청\n\n상세", A, "2026-01-01T00:00:02Z"),
            Review(3, "me", "COMMENTED", "", A, "2026-01-01T00:00:03Z"),
            Review(4, "me", "COMMENTED", "+1\r\nHEAD: x\r\n", A, "2026-01-01T00:00:04Z"),
        ]
        c.review_list = extra
        _, out = self.run_cycle(w)
        lines = [m for m in out if "dummy" not in m]
        self.assertEqual(lines, [
            "INFO:badcat:PR #7: -1 : FAIL (blocker found)",
            "INFO:badcat:PR #7: ## 1차 리뷰 — 변경 요청",
            "INFO:badcat:PR #7: ",
            "INFO:badcat:PR #7: +1",
        ])
        self.assertFalse(any("INVALID" in m.upper() for m in lines))
        self.assertEqual(c.calls, [])

    def test_reviews_after_the_passes_hold_the_merge_but_print_their_first_line(self):
        later = {
            "COMMENTED": ("COMMENTED", "## 1차 리뷰 — 변경 요청\n\n상세"),
            "CHANGES_REQUESTED": ("CHANGES_REQUESTED", "## 1차 리뷰 — 변경 요청\n\n상세"),
            "empty": ("COMMENTED", ""),
            "marker_as_changes_requested": ("CHANGES_REQUESTED", body("+2", A)),
        }
        for name, (state, text) in later.items():
            with self.subTest(name):
                passes = seq(rev("+1", A), rev("+2", A))
                last = Review(950, "me", state, text, A, "2026-01-03T00:00:00Z")
                c = FakeClient(reviews=passes)
                w = self.watcher(c, merge=True)
                c.open = True
                # first sight records history; the late review is then announced and holds the PR
                with self.assertLogs("badcat", level="DEBUG"):
                    logging_marker()
                    w.state.set(7, {"head": A, "reviews": [r.id for r in passes], "note": "", "gate": "ok"})
                c.review_list = passes + [last]
                _, out = self.run_cycle(w)
                self.assertEqual(c.calls, [])
                first = text.split("\n")[0]
                self.assertIn(f"INFO:badcat:PR #7: {first}", out)
                self.assertFalse(any("INVALID" in m.upper() for m in out))

    def test_approval_and_earlier_comment_do_not_hold(self):
        c = FakeClient(reviews=seq(rev(None, A, text="context for reviewers"), rev("+1", A),
                                   rev("+2", A), rev(None, A, state="APPROVED", text="ok")))
        self.run_cycle(self.watcher(c, merge=True))
        self.assertEqual(len(c.calls), 1)

    def test_merge_needs_no_required_check_option(self):
        cli.Watcher("o/r", FakeClient(), cli.NotifyState(self.dir / "x.json"), ME, merge=True)
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            cli.main(["o/r", "--merge", "--required-check", "x"])

    def blocked_by(self, mutate):
        c = FakeClient()
        mutate(c)
        _, out = self.run_cycle(self.watcher(c, merge=True))
        self.assertEqual(c.calls, [])
        return out

    def test_any_actions_check_name_with_success_is_eligible(self):
        for name in ("Validate and test Typewriter", "unittest (3.9)", "build"):
            with self.subTest(name):
                c = FakeClient()
                c.runs = [run(name), run("unittest (3.12)", id=2)]
                self.run_cycle(self.watcher(c, merge=True))
                self.assertEqual(c.calls, [("merge", 7, A)])

    def test_no_or_unproven_ci_never_merges(self):
        self.blocked_by(lambda c: c.runs.clear())
        self.blocked_by(lambda c: c.runs.__setitem__(slice(None), [run("test", conclusion="skipped")]))
        # a green check from another app, or a green commit status, is not proof CI executed
        self.blocked_by(lambda c: c.runs.__setitem__(slice(None), [run("lint", app="other-ci")]))
        self.blocked_by(lambda c: (c.runs.clear(), c.status.update(state="success", total_count=1)))

    def test_multiple_checks_all_must_pass(self):
        for bad in (dict(conclusion="failure"), dict(conclusion="cancelled"),
                    dict(conclusion="timed_out"), dict(conclusion="action_required"),
                    dict(status="in_progress", conclusion=None), dict(head=B)):
            with self.subTest(bad):
                self.blocked_by(lambda c: c.runs.append(run("other", id=2, **bad)))
        c = FakeClient()
        c.runs.append(run("lint", id=2, conclusion="skipped"))
        c.runs.append(run("ext", id=3, app="other-ci"))
        self.run_cycle(self.watcher(c, merge=True))
        self.assertEqual(len(c.calls), 1)

    def test_workflow_runs_gate_even_without_check_runs_for_them(self):
        for bad in (dict(conclusion="action_required"), dict(status="queued", conclusion=None),
                    dict(conclusion="failure"), dict(head=B)):
            with self.subTest(bad):
                self.blocked_by(lambda c: c.workflows.append(run("CI", id=9, **bad)))
        c = FakeClient()
        c.workflows.append(run("CI", id=9))
        self.run_cycle(self.watcher(c, merge=True))
        self.assertEqual(len(c.calls), 1)

    def test_ci_read_error_blocks_and_rerun_recovers(self):
        c = FakeClient()
        c.fail["workflow_runs"] = "HTTP 502"
        w = self.watcher(c, merge=True)
        delay, out = self.run_cycle(w)
        self.assertEqual((c.calls, w.errors), ([], 1))
        del c.fail["workflow_runs"]
        c.runs[0] = run("test", conclusion="failure")
        self.run_cycle(w)
        self.assertEqual(c.calls, [])
        c.runs[0] = run("test")  # re-run succeeded on the same HEAD
        self.run_cycle(w)
        self.assertEqual(c.calls, [("merge", 7, A)])

    def test_ci_is_reread_immediately_before_merge(self):
        c = FakeClient()
        w = self.watcher(c, merge=True)
        calls = iter([[run("test")], [run("test", conclusion="failure")]])
        c.check_runs = lambda sha: next(calls)
        self.run_cycle(w)
        self.assertEqual(c.calls, [])

    def test_branch_protection_refusal_is_reported_once(self):
        c = FakeClient()
        c.merge_error = "Required status check is expected (HTTP 405)"
        w = self.watcher(c, merge=True)
        _, out = self.run_cycle(w)
        self.assertTrue(any("merge rejected" in m for m in out))
        with self.assertLogs("badcat", level="DEBUG"):
            logging_marker()
            w.cycle()
        self.assertEqual(len(c.calls), 1)

    def test_changes_requested_never_counts_toward_merge(self):
        c = FakeClient(reviews=seq(rev("+1", A), rev("+2", A, state="CHANGES_REQUESTED")))
        self.run_cycle(self.watcher(c, merge=True))
        self.assertEqual(c.calls, [])

    def test_new_head_reported_and_old_reviews_not_replayed(self):
        c = FakeClient(reviews=seq(rev("+1", A)))
        w = self.watcher(c)
        self.run_cycle(w)
        c.head, c.raw["head"] = B, {"sha": B}
        _, out = self.run_cycle(w)
        self.assertTrue(any("new HEAD (bbbbbbb)" in m for m in out))
        self.assertFalse(any("PR #7: +1" in m for m in out))

    def test_restart_does_not_repeat_notifications(self):
        first = seq(rev("+1", A))
        c = FakeClient(reviews=first)
        self.run_cycle(self.watcher(c))
        c.review_list = first + [Review(901, "me", "COMMENTED", body("+2", A), A, "2026-01-02T00:00:00Z")]
        self.run_cycle(self.watcher(c))
        _, out = self.run_cycle(self.watcher(c))  # a third process: nothing new
        self.assertFalse(any("PR #7: +" in m or "new" in m for m in out))

    def test_api_errors_backoff_dedupe_and_recover(self):
        c = FakeClient()
        c.fail["open_prs"] = "HTTP 403 forbidden"
        w = self.watcher(c)
        with self.assertLogs("badcat", level="WARNING") as cm:
            d1 = w.cycle()
            d2 = w.cycle()
        self.assertEqual(len(cm.output), 1)  # same error reported once
        self.assertGreater(d2, d1 - 1)
        self.assertEqual(c.calls, [])
        del c.fail["open_prs"]
        _, _ = self.run_cycle(w)
        self.assertEqual(w.errors, 0)
        c.fail["reviews"] = "timed out"
        with self.assertLogs("badcat", level="WARNING"):
            self.assertGreater(w.cycle(), cli.POLL_INTERVAL - 1)
        self.assertEqual(c.calls, [])

    def test_closed_or_merged_elsewhere_is_idempotent(self):
        c = FakeClient(reviews=seq(rev("+1", A)))
        w = self.watcher(c)
        self.run_cycle(w)
        c.open, c.raw["merged"] = False, True
        _, out = self.run_cycle(w)
        self.assertTrue(any("PR #7: merged" in m for m in out))
        self.assertEqual(w.state.prs, {})

    def test_corrupt_or_incompatible_state_is_ignored(self):
        for text in ("{not json", json.dumps({"version": 99, "prs": {}}), "[]"):
            p = self.dir / "s.json"
            p.write_text(text)
            with self.assertLogs("badcat", level="WARNING"):
                self.assertEqual(cli.NotifyState(p).prs, {})


def logging_marker():
    import logging
    logging.getLogger("badcat").info("dummy")


class LockAndIdentityTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        self.root = root
        for mod, name, value in ((cli, "LOCK_ROOT", root / "badlocks"), (cli, "CONFIG_DIR", root / "badconf"),
                                 (misscat, "LOCK_ROOT", root / "misslocks"),
                                 (misscat, "CONFIG_DIR", root / "missconf")):
            p = mock.patch.object(mod, name, value)
            p.start()
            self.addCleanup(p.stop)

    def test_two_badcats_conflict_but_misscat_runs_alongside(self):
        with cli.merger_lock("o/r"):
            with self.assertRaises(cli.BadCatError):
                with cli.merger_lock("o/r"):
                    pass
            with misscat.repository_lock("o/r"):  # concurrent MissCat on the same repo
                pass
            with cli.merger_lock("o/other"):
                pass
        with cli.merger_lock("o/r"):  # released after exit, file may remain
            pass

    def test_state_and_lock_paths_are_separate_from_misscat(self):
        self.assertNotEqual(cli.state_path("o/r").parent, misscat.state_path("o/r").parent)
        with cli.merger_lock("o/r"):
            cli.NotifyState(cli.state_path("o/r")).set(1, {"head": A})
        self.assertFalse((self.root / "missconf").exists())
        self.assertFalse((self.root / "misslocks").exists())

    def test_cli_repo_errors_exit_2_without_side_effects(self):
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(cli.main(["bad repo"]), 2)
        self.assertFalse((self.root / "badlocks").exists())

    def test_main_default_is_notify_only_and_uses_viewer_as_trusted(self):
        seen = {}
        class W:
            def __init__(self, repo, client, state, trusted, merge):
                seen.update(trusted=trusted, merge=merge)
            def run_forever(self):
                pass
        with mock.patch.object(cli, "Watcher", W), \
             mock.patch.object(cli.shutil, "which", return_value="/usr/bin/gh"), \
             mock.patch.object(cli.GhClient, "viewer", return_value="Luna"):
            self.assertEqual(cli.main(["o/r"]), 0)
            self.assertEqual(seen, {"trusted": frozenset({"luna"}), "merge": False})
            self.assertEqual(cli.main(["o/r", "--merge", "--trusted-reviewer", "A"]), 0)
            self.assertEqual(seen, {"trusted": frozenset({"a"}), "merge": True})


class GhClientTests(unittest.TestCase):
    def test_merge_request_is_squash_with_sha_guard(self):
        client = GhClient("o/r")
        with mock.patch.object(client, "_api") as api:
            client.merge(3, A)
        api.assert_called_once_with("repos/o/r/pulls/3/merge", "-X", "PUT",
                                    "-f", "merge_method=squash", "-f", f"sha={A}")

    def test_timeout_and_nonzero_become_gherror(self):
        client = GhClient("o/r")
        with mock.patch.object(sys.modules["badcat.github"].subprocess, "run",
                               side_effect=sys.modules["badcat.github"].subprocess.TimeoutExpired("gh", 1)):
            with self.assertRaises(misscat.GhError):
                client.pr(1)

    def test_pagination_fails_closed_on_truncation(self):
        client = GhClient("o/r")
        with mock.patch.object(client, "_api", return_value=[{}] * 100), \
             mock.patch("badcat.github.MAX_PAGES", 2):
            with self.assertRaises(misscat.GhError):
                client._pages("x")


class PackagingTests(unittest.TestCase):
    def test_wheel_contains_badcat_and_entry_points(self):
        root = Path(__file__).resolve().parents[1]
        with tempfile.TemporaryDirectory() as out:
            proc = subprocess.run([sys.executable, "-m", "pip", "wheel", "--no-deps", "--no-build-isolation",
                                   "-q", "-w", out, str(root)], capture_output=True, text=True)
            if proc.returncode != 0:
                self.skipTest(f"cannot build wheel offline: {proc.stderr[-200:]}")
            wheel = next(Path(out).glob("misscat-*.whl"))
            with zipfile.ZipFile(wheel) as z:
                names = z.namelist()
                self.assertIn("badcat/cli.py", names)
                self.assertIn("badcat/protocol.py", names)
                eps = z.read(next(n for n in names if n.endswith("entry_points.txt"))).decode()
            self.assertIn("badcat = badcat.cli:main", eps)
            self.assertIn("misscat = misscat:main", eps)


if __name__ == "__main__":
    unittest.main()

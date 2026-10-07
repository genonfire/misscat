"""Backlog burst reviews: batches, the batch barrier, and persistent cat worktrees."""
import json
import os
import subprocess
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

import misscat as m

REPO = "genonfire/typewriter"


def git(*args, cwd=None):
    env = {"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_NAME": "t",
           "GIT_COMMITTER_EMAIL": "t@t", "PATH": os.environ["PATH"],
           "HOME": os.environ["HOME"]}
    return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True,
                          env=env).stdout.strip()


class FakeWorkspaces:
    def __init__(self, fail=()):
        self.fail, self.synced, self.prepared = set(fail), 0, []

    def sync(self):
        self.synced += 1

    def prepare(self, pr, cat):
        if pr.number in self.fail:
            raise m.WorkspaceError("boom")
        self.prepared.append((pr.number, cat))
        return Path(f"/cat-{cat}")


class WatcherBase(unittest.TestCase):
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
        self.prs = [m.PR(n, f"sha{n}", False, "u", "t") for n in (101, 102, 103, 104)]

    def watcher(self, reviewer, cats, prs=None, workspaces=None, sleeps=None):
        prs = self.prs if prs is None else prs
        sleeps = [] if sleeps is None else sleeps
        return m.Watcher(REPO, "luna", self.cfg, self.state, lambda repo: prs, reviewer,
                         sleep=sleeps.append, cats=cats, workspaces=workspaces)

    def reviewed(self):
        return {k.pr for k in self.state.reviewed()}


class BatchTests(WatcherBase):
    def test_cat_1_reviews_one_pr_per_cycle_inline(self):
        threads = []
        w = self.watcher(lambda s, r, pr, ws: threads.append(threading.current_thread().name) or True, 1)
        w.cycle()
        self.assertEqual(self.reviewed(), {101})
        self.assertEqual(threads, [threading.current_thread().name])

    def test_backlog_smaller_than_cats_launches_only_needed(self):
        calls = []
        w = self.watcher(lambda s, r, pr, ws: calls.append(pr.number) or True, 5, self.prs[:2])
        w.cycle()
        self.assertEqual(sorted(calls), [101, 102])

    def test_backlog_larger_than_cats_is_capped_and_concurrent(self):
        barrier = threading.Barrier(3, timeout=5)  # only passes if all 3 run at the same time
        calls = []

        def reviewer(s, r, pr, ws):
            barrier.wait()
            calls.append(pr.number)
            return True

        w = self.watcher(reviewer, 3)
        w.cycle()
        self.assertEqual(sorted(calls), [101, 102, 103])
        self.assertEqual(self.reviewed(), {101, 102, 103})

    def test_each_cat_gets_its_own_workspace(self):
        seen = {}
        ws = FakeWorkspaces()
        w = self.watcher(lambda s, r, pr, path: seen.setdefault(pr.number, path) and True, 3,
                         workspaces=ws)
        w.cycle()
        self.assertEqual(seen, {101: Path("/cat-1"), 102: Path("/cat-2"), 103: Path("/cat-3")})
        self.assertEqual(ws.synced, 1)

    def test_next_pr_waits_for_whole_batch(self):
        release = threading.Event()
        started = []
        lock = threading.Lock()

        def reviewer(s, r, pr, ws):
            with lock:
                started.append(pr.number)
            if pr.number == 102:
                release.wait(5)  # slow cat; the fast one must not pick up another PR
            return True

        w = self.watcher(reviewer, 2)
        t = threading.Thread(target=w.cycle)
        t.start()
        for _ in range(100):
            with lock:
                if len(started) == 2:
                    break
            threading.Event().wait(0.02)
        threading.Event().wait(0.2)
        self.assertEqual(sorted(started), [101, 102])
        self.assertEqual(self.reviewed(), set())  # nothing recorded before the barrier
        release.set()
        t.join(5)
        self.assertEqual(self.reviewed(), {101, 102})
        self.assertEqual(sorted(started), [101, 102])  # 103 not launched in this cycle

    def test_partial_failure_still_records_successes(self):
        w = self.watcher(lambda s, r, pr, ws: pr.number != 102, 3)
        w.cycle()
        self.assertEqual(self.reviewed(), {101, 103})
        self.assertEqual(w.failed, {m.Key(102, "sha102", "luna")})
        self.assertEqual(w.mode, m.ACTIVE)  # something succeeded: re-list right away

    def test_crash_in_one_cat_is_a_failure_only_for_that_pr(self):
        def reviewer(s, r, pr, ws):
            if pr.number == 101:
                raise RuntimeError("x")
            return True

        w = self.watcher(reviewer, 2)
        with self.assertLogs("misscat", "ERROR"):
            w.cycle()
        self.assertEqual(self.reviewed(), {102})

    def test_all_failed_waits_failure_wait(self):
        sleeps = []
        w = self.watcher(lambda *a: False, 2, sleeps=sleeps)
        w.cycle()
        self.assertEqual(sleeps, [m.FAILURE_WAIT])
        self.assertEqual(self.reviewed(), set())

    def test_failed_head_goes_last_in_next_batch(self):
        calls = []
        w = self.watcher(lambda s, r, pr, ws: calls.append(pr.number) or pr.number != 101, 2)
        w.cycle()  # 101 fails, 102 ok
        calls.clear()
        w.cycle()  # 103, 104 are ahead of the failed 101
        self.assertEqual(sorted(calls), [103, 104])

    def urgent_prs(self, *urgent):
        return [m.PR(n, f"sha{n}", False, "u", "t", n in urgent) for n in (101, 102, 103, 104)]

    def selected(self, cats, prs, w=None):
        w = w or self.watcher(lambda s, r, pr, ws: True, cats, prs)
        return [p.number for p in w._next_batch(prs)]

    def test_no_urgent_keeps_oldest_first(self):
        self.assertEqual(self.selected(2, self.urgent_prs()), [101, 102])

    def test_urgent_jumps_ahead_and_fills_remaining_with_normal(self):
        self.assertEqual(self.selected(2, self.urgent_prs(103)), [103, 101])

    def test_multiple_urgent_oldest_first(self):
        self.assertEqual(self.selected(3, self.urgent_prs(104, 102)), [102, 104, 101])

    def test_more_urgent_than_cats_only_urgent(self):
        self.assertEqual(self.selected(2, self.urgent_prs(102, 103, 104)), [102, 103])

    def test_urgent_added_mid_batch_observed_only_on_next_listing(self):
        current = [self.urgent_prs()]
        started = []

        def reviewer(s, r, pr, ws):
            current[0] = self.urgent_prs(104)  # label added while the batch is running
            started.append(pr.number)
            return True

        w = m.Watcher(REPO, "luna", self.cfg, self.state, lambda repo: current[0], reviewer,
                      sleep=lambda s: None, cats=2)
        w.cycle()
        self.assertEqual(sorted(started), [101, 102])  # running batch unchanged
        started.clear()
        w.cycle()
        self.assertEqual(sorted(started), [103, 104])
        self.assertEqual(self.reviewed(), {101, 102, 103, 104})

    def labeled_prs(self, urgent=(), no_review=()):
        return [m.PR(n, f"sha{n}", False, "u", "t", n in urgent, n in no_review)
                for n in (101, 102, 103, 104)]

    def test_no_review_pr_is_excluded(self):
        self.assertEqual(self.selected(4, self.labeled_prs(no_review=(102,))), [101, 103, 104])

    def test_no_review_beats_urgent(self):
        self.assertEqual(self.selected(2, self.labeled_prs(urgent=(103,), no_review=(103,))), [101, 102])

    def test_no_review_does_not_consume_a_cat_slot(self):
        self.assertEqual(self.selected(2, self.labeled_prs(no_review=(101,))), [102, 103])

    def test_no_review_is_not_recorded_and_label_removal_restores_eligibility(self):
        started = []
        current = [self.labeled_prs(no_review=(101, 102, 103, 104))]
        w = m.Watcher(REPO, "luna", self.cfg, self.state, lambda repo: current[0],
                      lambda s, r, pr, ws: started.append(pr.number) or True,
                      sleep=lambda s: None, cats=2)
        w.cycle()
        self.assertEqual(started, [])
        self.assertEqual(self.reviewed(), set())
        current[0] = self.labeled_prs(no_review=(102, 103, 104))  # label removed from 101
        w.cycle()
        self.assertEqual(started, [101])
        self.assertEqual(self.reviewed(), {101})

    def test_no_review_added_mid_batch_observed_only_on_next_listing(self):
        current = [self.labeled_prs()]
        started = []

        def reviewer(s, r, pr, ws):
            current[0] = self.labeled_prs(no_review=(101, 103))  # label added while running
            started.append(pr.number)
            return True

        w = m.Watcher(REPO, "luna", self.cfg, self.state, lambda repo: current[0], reviewer,
                      sleep=lambda s: None, cats=2)
        w.cycle()
        self.assertEqual(sorted(started), [101, 102])  # in-flight review not cancelled
        self.assertEqual(self.reviewed(), {101, 102})
        started.clear()
        w.cycle()
        self.assertEqual(started, [104])

    def test_failed_head_fairness_within_each_priority_class(self):
        prs = self.urgent_prs(101, 102)
        w = self.watcher(lambda s, r, pr, ws: True, 4, prs)
        w.failed = {w._key(prs[0]), w._key(prs[2])}  # urgent 101 and normal 103 failed
        self.assertEqual(self.selected(4, prs, w), [102, 101, 104, 103])

    def test_labels_parsed_from_single_gh_listing(self):
        out = json.dumps([
            {"number": 1, "headRefOid": "a", "isDraft": False, "url": "u", "title": "t",
             "labels": [{"name": "urgent", "color": "f00"}]},
            {"number": 2, "headRefOid": "b", "isDraft": False, "url": "u", "title": "t",
             "labels": [{"name": "Urgent-ish"}, {"name": "No-Review"}]},
            {"number": 3, "headRefOid": "c", "isDraft": False, "url": "u", "title": "t", "labels": []},
            {"number": 4, "headRefOid": "d", "isDraft": False, "url": "u", "title": "t",
             "labels": [{"name": "no-review"}, {"name": "urgent"}]},
        ])
        proc = mock.Mock(returncode=0, stdout=out, stderr="")
        with mock.patch.object(m.subprocess, "run", return_value=proc) as run:
            prs = m.gh_open_prs(REPO)
        self.assertEqual(run.call_count, 1)
        self.assertIn("labels", run.call_args[0][0][-1])
        self.assertEqual([p.urgent for p in prs], [True, False, False, True])
        self.assertEqual([p.no_review for p in prs], [False, False, False, True])

    def test_workspace_failure_fails_only_that_pr(self):
        ws = FakeWorkspaces(fail={102})
        w = self.watcher(lambda *a: True, 3, workspaces=ws)
        with self.assertLogs("misscat", "ERROR"):
            w.cycle()
        self.assertEqual(self.reviewed(), {101, 103})

    def test_sync_failure_fails_whole_batch(self):
        ws = FakeWorkspaces()
        ws.sync = mock.Mock(side_effect=m.WorkspaceError("offline"))
        sleeps = []
        w = self.watcher(lambda *a: self.fail("must not run"), 2, workspaces=ws, sleeps=sleeps)
        with self.assertLogs("misscat", "ERROR"):
            w.cycle()
        self.assertEqual(sleeps, [m.FAILURE_WAIT])
        self.assertEqual(self.reviewed(), set())

    def test_state_is_written_only_from_the_main_thread_after_reviewers_finish(self):
        writers, finished = [], []
        original = self.state.add

        def add(key):
            writers.append(threading.current_thread())
            self.assertEqual(len(finished), 3)
            original(key)

        self.state.add = add

        def reviewer(s, r, pr, ws):
            finished.append(pr.number)
            return True

        self.watcher(reviewer, 3).cycle()
        self.assertEqual(len(writers), 3)
        self.assertTrue(all(t is threading.main_thread() for t in writers))


class CliTests(unittest.TestCase):
    def test_cat_must_be_positive(self):
        for bad in ("0", "-1", "x"):
            with self.assertRaises(SystemExit), mock.patch("sys.stderr"):
                m.main(["o/r", f"--cat={bad}"])


class WorkspaceTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        self.origin = root / "origin.git"
        seed = root / "seed"
        git("init", "-q", "--bare", "-b", "main", str(self.origin))
        git("init", "-q", "-b", "main", str(seed))
        (seed / "a.txt").write_text("base\n")
        git("add", ".", cwd=seed)
        git("commit", "-qm", "base", cwd=seed)
        git("remote", "add", "origin", str(self.origin), cwd=seed)
        git("push", "-q", "origin", "main", cwd=seed)
        self.seed = seed
        self.prs = {}
        for number in (1, 2, 3):
            self.add_pr(number)
        for name, value in (("WORKSPACE_ROOT", root / "repos"), ("CAT_ROOT", root / "cats")):
            patcher = mock.patch.object(m, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        patcher = mock.patch.object(m, "_clone", lambda repo, path: git("clone", "-q", str(self.origin), str(path)))
        patcher.start()
        self.addCleanup(patcher.stop)

    def add_pr(self, number):
        git("checkout", "-q", "-B", f"pr{number}", "main", cwd=self.seed)
        (self.seed / f"pr{number}.txt").write_text(f"{number}\n")
        git("add", ".", cwd=self.seed)
        git("commit", "-qm", f"pr {number}", cwd=self.seed)
        sha = git("rev-parse", "HEAD", cwd=self.seed)
        git("push", "-q", str(self.origin), f"HEAD:refs/pull/{number}/head", cwd=self.seed)
        git("checkout", "-q", "main", cwd=self.seed)
        self.prs[number] = m.PR(number, sha, False, "u", "t")

    def test_creates_control_clone_and_cats_once_and_reuses_them(self):
        ws = m.Workspaces(REPO, 3)
        ws.sync()
        for n in (1, 2, 3):
            self.assertTrue((ws.cat_path(n) / ".git").exists())
        marker = ws.cat_path(2) / "marker"
        marker.write_text("kept")
        again = m.Workspaces(REPO, 3)
        again.sync()
        self.assertTrue(marker.exists())  # sync neither recreated nor cleaned the cat
        listing = git("worktree", "list", cwd=ws.control)
        self.assertEqual(listing.count("cat-"), 3)

    def test_increasing_cats_adds_only_missing_and_decreasing_leaves_extras(self):
        m.Workspaces(REPO, 2).sync()
        ws = m.Workspaces(REPO, 3)
        marker = ws.cat_path(1) / "marker"
        marker.write_text("kept")
        ws.sync()
        self.assertTrue(ws.cat_path(3).exists())
        self.assertTrue(marker.exists())
        m.Workspaces(REPO, 1).sync()
        for n in (1, 2, 3):
            self.assertTrue(ws.cat_path(n).exists())

    def test_control_stays_clean_on_default_branch(self):
        ws = m.Workspaces(REPO, 2)
        ws.sync()
        (ws.control / "junk.txt").write_text("x")
        (ws.control / "a.txt").write_text("changed")
        ws.prepare(self.prs[1], 1)
        ws.sync()
        self.assertEqual(git("symbolic-ref", "--short", "HEAD", cwd=ws.control), "main")
        self.assertEqual(git("status", "--porcelain", cwd=ws.control), "")
        self.assertEqual(git("rev-parse", "HEAD", cwd=ws.control),
                         git("rev-parse", "main", cwd=self.origin))

    def test_prepare_cleans_and_detaches_at_exact_head(self):
        ws = m.Workspaces(REPO, 2)
        ws.sync()
        path = ws.prepare(self.prs[1], 1)
        self.assertEqual(git("rev-parse", "HEAD", cwd=path), self.prs[1].head)
        (path / "leftover.txt").write_text("x")
        (path / "a.txt").write_text("dirty")
        path = ws.prepare(self.prs[2], 1)
        self.assertEqual(git("rev-parse", "HEAD", cwd=path), self.prs[2].head)
        self.assertEqual(git("status", "--porcelain", cwd=path), "")
        self.assertFalse((path / "pr1.txt").exists())
        self.assertEqual(subprocess.run(["git", "symbolic-ref", "-q", "HEAD"], cwd=path).returncode, 1)
        other = ws.prepare(self.prs[3], 2)  # two cats hold different HEADs at the same time
        self.assertEqual(git("rev-parse", "HEAD", cwd=other), self.prs[3].head)
        self.assertEqual(git("rev-parse", "HEAD", cwd=path), self.prs[2].head)

    def test_head_changed_since_listing_is_a_preparation_failure(self):
        ws = m.Workspaces(REPO, 1)
        ws.sync()
        stale = m.PR(1, "0" * 40, False, "u", "t")
        with self.assertRaises(m.WorkspaceError):
            ws.prepare(stale, 1)

    def test_broken_cat_is_recreated(self):
        ws = m.Workspaces(REPO, 1)
        ws.sync()
        (ws.cat_path(1) / ".git").unlink()
        ws.sync()
        path = ws.prepare(self.prs[1], 1)
        self.assertEqual(git("rev-parse", "HEAD", cwd=path), self.prs[1].head)


if __name__ == "__main__":
    unittest.main()

"""Independent cat workers: immediate settlement, one periodic poll, and persistent cat worktrees."""
import collections
import dataclasses
import json
import os
import queue
import subprocess
import tempfile
import threading
import time
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
        self.fail, self.synced, self.prepared, self.skips = set(fail), 0, [], []
        self.sync_error = None
        self.on_prepare = None

    def sync(self, skip=frozenset()):
        if self.sync_error:
            raise self.sync_error
        self.synced += 1
        self.skips.append(set(skip))

    def prepare(self, pr, cat):
        if pr.number in self.fail:
            raise m.WorkspaceError("boom")
        if self.on_prepare:
            self.on_prepare(pr, cat)
        self.prepared.append((pr.number, cat))
        return Path(f"/cat-{cat}")


class FakeDiscussion:
    """GitHub stand-in: a review carries the run nonce of every run that `submit`ted one."""

    def __init__(self):
        self.runs, self.calls = {}, 0

    def submit(self, pr):
        self.runs[pr.number] = (pr.head, pr.run_id)

    def reviews(self, number):
        self.calls += 1
        if number not in self.runs:
            return []
        head, run = self.runs[number]
        return [m.GhReview(number, "bot", "COMMENTED",
                           f"+1\nHEAD: {head}\n\nok\n{m.run_line(run)}", head, "2026-01-01T00:00:00Z")]

    def head(self, number):
        return self.runs[number][0]


def wait_until(cond, timeout=5.0):
    end = time.monotonic() + timeout
    while not cond():
        if time.monotonic() > end:
            raise AssertionError("timed out")
        time.sleep(0.005)


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
        self.prs = [m.PR(n, f"{n:040x}", False, "u", "t") for n in (101, 102, 103, 104)]
        self.now = 1000.0
        self.list_calls = 0
        self.gates = collections.defaultdict(threading.Event)  # PR number -> release
        self.started = collections.defaultdict(threading.Event)

    def watcher(self, reviewer, cats, prs=None, workspaces=None, **kw):
        prs = self.prs if prs is None else prs
        self.current = list(prs)

        def list_prs(repo):
            self.list_calls += 1
            return list(self.current)

        w = m.Watcher(REPO, "luna", self.cfg, self.state, list_prs, reviewer, cats=cats,
                      workspaces=workspaces, clock=lambda: self.now, **kw)
        self.addCleanup(self.release_all)
        return w

    def release_all(self):
        for gate in list(self.gates.values()):
            gate.set()

    def gated(self, ok=True):
        """A reviewer that blocks until `self.gates[pr.number]` is set."""
        def reviewer(s, r, pr, ws):
            self.started[pr.number].set()
            self.gates[pr.number].wait(5)
            return ok
        return reviewer

    def collect(self, w, n=1):
        """Record `n` finished reviews the way run_forever does, one event at a time."""
        for _ in range(n):
            w._finish(w.events.get(timeout=5))

    def reviewed(self):
        return {k.pr for k in self.state.reviewed()}


class BatchTests(WatcherBase):
    def test_cat_1_uses_one_worker_thread(self):
        threads = []
        w = self.watcher(lambda s, r, pr, ws: threads.append(threading.current_thread().name) or True, 1)
        w.cycle()
        self.assertEqual(self.reviewed(), {101})
        self.assertEqual(threads, ["cat-1"])

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

    def test_partial_failure_still_records_successes(self):
        w = self.watcher(lambda s, r, pr, ws: pr.number != 102, 3)
        w.cycle()
        self.assertEqual(self.reviewed(), {101, 103})
        self.assertEqual(w.failed, {m.Key(102, self.prs[1].head): self.now})

    def test_crash_in_one_cat_is_a_failure_only_for_that_pr(self):
        def reviewer(s, r, pr, ws):
            if pr.number == 101:
                raise RuntimeError("x")
            return True

        w = self.watcher(reviewer, 2)
        with self.assertLogs("misscat", "ERROR"):
            w.cycle()
        self.assertEqual(self.reviewed(), {102})
        self.assertEqual(set(w.failed), {m.Key(101, self.prs[0].head)})

    # ---- immediate settlement, no barrier

    def test_fast_cat_is_recorded_before_the_slow_one_and_waits_for_the_next_poll(self):
        w = self.watcher(self.gated(), 2, self.prs[:2])
        w.poll()
        wait_until(lambda: all(self.started[n].is_set() for n in (101, 102)))
        self.gates[101].set()
        with self.assertLogs("misscat", "INFO") as logs:
            self.collect(w)
        self.assertEqual(self.reviewed(), {101})  # 102 is still running
        self.assertTrue(any("PR #101: review done" in line and "[cat-1]" in line for line in logs.output))
        self.assertEqual(set(w.running), {2})
        self.assertEqual(w.idle, [1])
        self.current = self.prs  # more work appears: cat-1 only takes it at the next poll
        self.assertFalse(self.started[103].is_set())
        w.poll()
        wait_until(lambda: self.started[103].is_set())
        self.assertEqual(w.running[1].pr.number, 103)
        self.assertEqual(self.reviewed(), {101})

    def test_completion_order_may_differ_from_start_order(self):
        w = self.watcher(self.gated(), 2, self.prs[:2])
        w.poll()
        self.gates[102].set()
        self.collect(w)
        self.assertEqual(self.reviewed(), {102})
        self.gates[101].set()
        self.collect(w)
        self.assertEqual(self.reviewed(), {101, 102})
        self.assertEqual((w.running, w.idle), ({}, [1, 2]))

    def test_state_is_written_only_from_the_main_thread(self):
        writers = []
        original = self.state.add

        def add(key, *ids):
            writers.append(threading.current_thread())
            original(key, *ids)

        self.state.add = add
        ws = mock.Mock(prepare=lambda pr, cat: Path("/c"))
        ws.sync.side_effect = lambda skip=frozenset(): writers.append(threading.current_thread())
        self.watcher(lambda *a: True, 3, workspaces=ws).cycle()
        self.assertEqual(len(writers), 4)  # sync + three records
        self.assertTrue(all(t is threading.main_thread() for t in writers))

    # ---- polling

    def test_completions_do_not_poll_and_do_not_move_the_deadline(self):
        cfg = dataclasses.replace(self.cfg, idle=(60.0,) * 5, active=(60.0,) * 5)
        self.cfg = cfg
        w = self.watcher(self.gated(), 2, self.prs[:2])
        timeouts, polls = [], []

        class Stop(BaseException):
            pass

        class Queue(queue.Queue):
            n = 0

            def get(q, block=True, timeout=None):
                if not block:
                    return super().get(False)
                timeouts.append(timeout)
                q.n += 1
                if q.n == 2:
                    self.gates[101].set()
                    wait_until(lambda: not q.empty())  # cat-1 finishes during the interval
                try:
                    return super().get(False)
                except queue.Empty:
                    self.now += timeout  # the interval elapses
                    raise

        w.events = Queue()
        original = w.list_prs

        def list_prs(repo):
            polls.append((self.now, self.reviewed()))
            if len(polls) == 3:
                raise Stop
            return original(repo)

        w.list_prs = list_prs
        try:
            with self.assertRaises(Stop):
                w.run_forever()
        finally:
            self.gates[102].set()
        # first poll at once, the next one exactly 60s later; cat-1's completion in between
        # was recorded without a listing and left the deadline alone
        self.assertEqual([t for t, _ in polls[:2]], [1000.0, 1060.0])
        self.assertEqual(polls[1][1], {101})
        self.assertEqual(self.list_calls, 2)
        self.assertEqual(timeouts[:4], [0.0, 60.0, 60.0, 60.0])

    def test_one_listing_per_poll_however_many_cats_finish(self):
        w = self.watcher(lambda *a: True, 3)
        w.cycle()
        self.assertEqual(self.list_calls, 1)

    def test_idle_cats_are_assigned_from_a_single_listing(self):
        w = self.watcher(self.gated(), 3, self.prs[:1])
        w.poll()
        self.current = self.prs
        w.poll()
        self.assertEqual(self.list_calls, 2)
        self.assertEqual(sorted(r.pr.number for r in w.running.values()), [101, 102, 103])

    # ---- inflight PRs

    def test_inflight_pr_gets_no_second_run_when_its_head_changes(self):
        disc = FakeDiscussion()
        calls = []

        def reviewer(s, r, pr, ws):
            calls.append((pr.number, pr.head))
            disc.submit(pr)
            self.gates[pr.number].wait(5)
            return True

        w = self.watcher(reviewer, 2, self.prs[:1], discussion=disc)
        w.poll()
        wait_until(lambda: calls)
        live = [r.run_id for r in self.state.pending()]
        self.current = [m.PR(101, "f" * 40, False, "u", "t")]
        w.poll()
        self.assertEqual(calls, [(101, self.prs[0].head)])  # nothing assigned
        self.assertEqual([r.run_id for r in self.state.pending()], live)  # live pending preserved
        self.gates[101].set()
        self.collect(w)
        self.assertEqual(self.state.reviewed(), {m.Key(101, self.prs[0].head)})
        self.assertEqual(self.state.pending(), [])
        w.poll()  # the new HEAD is picked up at the next poll
        while w.running:
            self.collect(w)
        self.assertEqual(calls[-1], (101, "f" * 40))

    def test_inflight_pr_is_not_looked_up_for_replies(self):
        w = self.watcher(self.gated(), 2, self.prs[:1])
        w.poll()
        w.discussion = mock.Mock()
        self.state.add(m.Key(101, self.prs[0].head), "luna")  # reviewed HEAD of a running PR (re-review)
        self.current = [m.PR(101, self.prs[0].head, False, "u", "t", author="a", updated_at="u1")]
        self.assertEqual(w._next_batch(self.current, 1, {101}), [])
        w.discussion.reviews.assert_not_called()

    # ---- ACTIVE / IDLE

    def test_mode_follows_running_cats_and_restarts_the_schedule(self):
        self.cfg = dataclasses.replace(self.cfg, idle=(10.0, 20.0, 30.0), active=(60.0,) * 5)
        w = self.watcher(self.gated(), 9, self.prs[:1])
        self.assertEqual(w.poll(), 60.0)
        self.assertEqual(w.mode, m.ACTIVE)  # one of nine cats is enough
        self.assertEqual(w.poll(), 60.0)
        self.gates[101].set()
        self.collect(w)
        self.assertEqual(w.poll(), 10.0)  # nothing running: idle schedule from its start
        self.assertEqual(w.mode, m.IDLE)
        self.assertEqual(w.poll(), 20.0)

    def test_listing_failure_keeps_running_cats_and_assigns_nothing(self):
        w = self.watcher(self.gated(), 2, self.prs[:1])
        w.poll()
        w.list_prs = mock.Mock(side_effect=m.GhError("down"))
        with self.assertLogs("misscat", "WARNING"):
            w.poll()
        self.assertEqual(set(w.running), {1})

    # ---- cat assignment

    def test_free_cat_number_is_reused_and_running_cats_are_skipped(self):
        ws = FakeWorkspaces()
        w = self.watcher(self.gated(), 2, self.prs[:2], workspaces=ws)
        w.poll()
        self.gates[101].set()
        self.collect(w)
        self.current = self.prs
        w.poll()
        self.assertEqual(ws.prepared, [(101, 1), (102, 2), (103, 1)])
        self.assertEqual(ws.skips, [set(), {2}])  # cat-2 still runs: never inspected

    # ---- preparation comes before pending

    def test_prepare_failure_leaves_no_pending_and_no_settle(self):
        disc = FakeDiscussion()
        ws = FakeWorkspaces(fail={101})
        w = self.watcher(lambda *a: True, 2, self.prs[:1], workspaces=ws, discussion=disc)
        with self.assertLogs("misscat", "ERROR"):
            w.cycle()
        self.assertEqual(self.state.pending(), [])
        self.assertEqual(disc.calls, 0)  # _settle never ran
        self.assertEqual(w.failed, {m.Key(101, self.prs[0].head): self.now})
        self.assertEqual((w.running, w.idle), ({}, [1, 2]))

    def test_workspace_failure_fails_only_that_pr(self):
        ws = FakeWorkspaces(fail={102})
        w = self.watcher(lambda *a: True, 3, workspaces=ws)
        with self.assertLogs("misscat", "ERROR"):
            w.cycle()
        self.assertEqual(self.reviewed(), {101, 103})

    def test_pending_is_written_only_for_prepared_prs(self):
        ws = FakeWorkspaces(fail={102})
        disc = FakeDiscussion()
        w = self.watcher(self.gated(), 3, self.prs[:3], workspaces=ws, discussion=disc)
        with self.assertLogs("misscat", "ERROR"):
            w.poll()
        self.assertEqual(sorted(r.pr for r in self.state.pending()), [101, 103])

    # ---- FAILURE_WAIT

    def test_failed_head_cools_down_then_retries_but_others_still_run(self):
        calls = []
        w = self.watcher(lambda s, r, pr, ws: calls.append(pr.number) or pr.number != 101, 1,
                         self.prs[:2])
        w.cycle()  # 101 fails
        w.cycle()  # in cooldown: 102 goes instead
        self.assertEqual(calls, [101, 102])
        self.now += m.FAILURE_WAIT - 1
        self.current = self.prs[:1]
        w.cycle()
        self.assertEqual(calls, [101, 102])  # still cooling down
        self.now += 1
        w.cycle()
        self.assertEqual(calls, [101, 102, 101])

    def test_failed_head_goes_last_after_its_cooldown(self):
        calls = []
        w = self.watcher(lambda s, r, pr, ws: calls.append(pr.number) or pr.number != 101, 2,
                         self.prs[:3])
        w.cycle()  # 101 fails, 102 ok
        calls.clear()
        self.now += m.FAILURE_WAIT
        w.cycle()  # 103 is ahead of the failed 101
        self.assertEqual(calls, [103, 101] if calls[0] == 103 else calls)
        self.assertEqual(calls[0], 103)

    def test_failed_entries_of_vanished_heads_are_dropped(self):
        w = self.watcher(lambda *a: False, 1, self.prs[:1])
        w.cycle()
        self.assertEqual(set(w.failed), {m.Key(101, self.prs[0].head)})
        self.current = [m.PR(101, "f" * 40, False, "u", "t")]
        w.poll()
        self.assertNotIn(m.Key(101, self.prs[0].head), w.failed)

    def test_sync_failure_is_a_global_delay_without_head_cooldown(self):
        disc = FakeDiscussion()
        ws = FakeWorkspaces()
        ws.sync_error = m.WorkspaceError("offline")
        w = self.watcher(lambda *a: self.fail("must not run"), 2, workspaces=ws, discussion=disc)
        with self.assertLogs("misscat", "ERROR"):
            delay = w.cycle()
        self.assertGreaterEqual(delay, m.FAILURE_WAIT)
        self.assertEqual(w.failed, {})
        self.assertEqual(self.state.pending(), [])
        self.assertEqual(disc.calls, 0)
        ws.sync_error = None  # recovered: the same HEADs, in normal order
        w.reviewer = lambda *a: True
        w.poll()
        self.assertEqual([p for p, _ in ws.prepared], [101, 102])

    def test_sync_failure_leaves_running_cats_and_their_completion_alone(self):
        ws = FakeWorkspaces()
        w = self.watcher(self.gated(), 2, self.prs[:1], workspaces=ws)
        w.poll()
        self.current = self.prs[:3]
        ws.sync_error = m.WorkspaceError("offline")
        with self.assertLogs("misscat", "ERROR"):
            delay = w.poll()
        self.assertGreaterEqual(delay, m.FAILURE_WAIT)
        self.assertEqual(set(w.running), {1})
        self.assertEqual(w.failed, {})
        self.gates[101].set()
        calls = self.list_calls
        self.collect(w)  # recorded at once, with no extra listing
        self.assertEqual(self.reviewed(), {101})
        self.assertEqual(self.list_calls, calls)

    # ---- fatal reviewer errors

    def test_fatal_stops_assignment_and_records_the_other_running_cat(self):
        fatal = []

        def reviewer(s, r, pr, ws):
            self.started[pr.number].set()
            if pr.number == 101:
                self.started[102].wait(5)  # both cats are running before the error shows
                fatal.append("auth")
                return False
            self.gates[102].wait(5)
            return True

        w = self.watcher(reviewer, 2, self.prs[:2], fatal=lambda: fatal[0] if fatal else None)
        w.poll()
        wait_until(lambda: fatal)
        self.current = self.prs
        threading.Timer(0.1, self.gates[102].set).start()
        with self.assertRaises(m.MissCatError) as ctx, self.assertLogs("misscat", "ERROR"):
            w.run_forever()
        self.assertEqual(str(ctx.exception), "auth")
        self.assertEqual(self.reviewed(), {102})  # the running cat was recorded first
        self.assertEqual(self.list_calls, 1)  # no more assignment
        self.assertFalse(self.started[103].is_set())

    def test_fatal_raised_inside_prepare_does_not_start_that_pr(self):
        fatal = []
        disc = FakeDiscussion()

        def reviewer(s, r, pr, ws):
            disc.submit(pr)
            self.gates[pr.number].wait(5)
            return True

        ws = FakeWorkspaces()
        w = self.watcher(reviewer, 2, self.prs[:1], workspaces=ws, discussion=disc,
                         fatal=lambda: fatal[0] if fatal else None)
        w.poll()
        wait_until(lambda: 101 in disc.runs)
        added = []
        original = self.state.add_pending
        self.state.add_pending = lambda run: added.append(run.pr) or original(run)

        def on_prepare(pr, cat):  # while 102 is being prepared, cat-1 finds a fatal error
            fatal.append("auth")
            self.gates[101].set()
            wait_until(lambda: not w.events.empty())

        ws.on_prepare = on_prepare
        self.current = self.prs[:2]
        w.poll()
        self.assertEqual(added, [])  # no pending for 102
        self.assertEqual(set(w.running), set())  # nothing is running; 101 was recorded
        self.assertEqual(w.failed, {})  # not the HEAD's fault
        self.assertEqual(ws.prepared, [(101, 1), (102, 2)])  # 102's prepared worktree stays
        self.assertEqual(self.reviewed(), {101})
        with self.assertRaises(m.MissCatError), self.assertLogs("misscat", "ERROR"):
            w._check_fatal()

    def test_worker_start_failure_cleans_up_and_other_cats_go_on(self):
        disc = FakeDiscussion()
        w = self.watcher(self.gated(), 3, self.prs[:1], discussion=disc)
        w.poll()  # cat-1 runs 101
        self.current = self.prs[:3]
        real = threading.Thread

        class Boom(real):
            def start(t):
                if t.name == "cat-2":
                    raise RuntimeError("can't start new thread")
                super().start()

        with mock.patch.object(m.threading, "Thread", Boom), self.assertLogs("misscat", "ERROR"):
            w.poll()
        self.assertEqual(sorted(w.running), [1, 3])  # 103 took cat-3
        self.assertEqual(w.idle, [2])
        self.assertEqual(set(w.failed), {m.Key(102, self.prs[1].head)})
        self.assertEqual(sorted(r.pr for r in self.state.pending()), [101, 103])

    def test_verified_recovery_of_pending_clears_the_cooldown(self):
        disc = FakeDiscussion()
        w = self.watcher(lambda s, r, pr, ws: disc.submit(pr) or True, 1, self.prs[:1], discussion=disc)
        real = disc.reviews
        down = [True]

        def reviews(number):
            if down[0] and disc.runs:
                raise m.GhError("down")
            return real(number)

        disc.reviews = reviews
        with self.assertLogs("misscat", "ERROR"):
            w.cycle()
        self.assertEqual(len(self.state.pending()), 1)  # unsettled stays pending
        self.assertIn(m.Key(101, self.prs[0].head), w.failed)
        w.poll()  # still down: still unsettled, cooldown kept
        self.assertIn(m.Key(101, self.prs[0].head), w.failed)
        self.assertEqual(len(self.state.pending()), 1)
        down[0] = False
        w.poll()  # the submitted review is found: done, no cooldown left
        self.assertEqual(self.reviewed(), {101})
        self.assertEqual(w.failed, {})
        self.assertEqual(self.state.pending(), [])

    def test_fatal_drained_inside_poll_ends_without_waiting_for_the_timer(self):
        fatal = []
        ws = FakeWorkspaces()
        w = self.watcher(self.gated(), 2, self.prs[:1], workspaces=ws,
                         fatal=lambda: fatal[0] if fatal else None)
        w.poll()

        def on_prepare(pr, cat):
            fatal.append("auth")
            self.gates[101].set()
            wait_until(lambda: not w.events.empty())

        ws.on_prepare = on_prepare
        self.current = self.prs[:2]
        timeouts = []
        original = w.events.get
        w.events.get = lambda *a, **k: timeouts.append(k) or original(*a, **k)
        with self.assertRaises(m.MissCatError), self.assertLogs("misscat", "ERROR"):
            w.run_forever()
        self.assertEqual(self.reviewed(), {101})
        self.assertTrue(all(not k.get("timeout") for k in timeouts))  # never waited on the timer

    # ---- completions arriving while dispatching

    def test_completion_during_dispatch_is_recorded_before_the_next_prepare(self):
        ws = FakeWorkspaces()
        w = self.watcher(self.gated(), 3, self.prs[:1], workspaces=ws)
        w.poll()  # cat-1 runs 101
        order = []

        def on_prepare(pr, cat):
            if pr.number == 102:
                self.gates[101].set()
                wait_until(lambda: not w.events.empty())
            if pr.number == 103:
                order.append(("prepare 103", self.reviewed()))

        ws.on_prepare = on_prepare
        self.current = self.prs[:3]
        calls = self.list_calls
        with self.assertLogs("misscat", "INFO") as logs:
            w.poll()
        self.assertEqual(order, [("prepare 103", {101})])  # recorded before the second prepare
        self.assertTrue(any("PR #101: review done" in line for line in logs.output))
        self.assertEqual(self.list_calls, calls + 1)
        # the cat freed meanwhile took nothing in this dispatch (idle snapshot: cats 2 and 3)
        self.assertEqual(ws.prepared, [(101, 1), (102, 2), (103, 3)])
        self.assertEqual(w.idle, [1])

    def test_state_is_not_written_by_workers(self):
        names = []
        original = self.state.add

        def add(*a):
            names.append(threading.current_thread().name)
            original(*a)

        self.state.add = add
        self.watcher(lambda *a: True, 2).cycle()
        self.assertEqual(names, ["MainThread"] * 2)

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
        self.root = root
        self.origin = root / "genonfire" / "typewriter.git"  # a URL ending in REPO, like GitHub's
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

    def drop_pull_ref(self, number, branch=None, remote=None):
        """GitHub lost refs/pull/N/head; the PR's commit stays on `branch` of `remote`."""
        remote = remote or self.origin
        if branch:
            git("push", "-q", str(remote), f"{self.prs[number].head}:refs/heads/{branch}", cwd=self.seed)
        git("update-ref", "-d", f"refs/pull/{number}/head", cwd=self.origin)

    def metadata(self, number, ref, source=REPO, sha=None):
        return {"head": {"sha": sha or self.prs[number].head, "ref": ref,
                         "repo": {"full_name": source} if source else None}}

    def test_pull_ref_fast_path_does_not_look_up_metadata(self):
        ws = m.Workspaces(REPO, 1)
        ws.sync()
        with mock.patch.object(m.GhDiscussion, "_api", side_effect=AssertionError("no lookup")):
            path = ws.prepare(self.prs[1], 1)
        self.assertEqual(git("rev-parse", "HEAD", cwd=path), self.prs[1].head)

    def test_missing_pull_ref_falls_back_to_same_repo_branch(self):
        ws = m.Workspaces(REPO, 1)
        ws.sync()
        self.drop_pull_ref(1, "a-codex-096-02")
        with mock.patch.object(m.GhDiscussion, "_api", return_value=self.metadata(1, "a-codex-096-02")) as api, \
                self.assertLogs("misscat", "INFO"):
            path = ws.prepare(self.prs[1], 1)
        api.assert_called_once_with(f"repos/{REPO}/pulls/1")
        self.assertEqual(git("rev-parse", "HEAD", cwd=path), self.prs[1].head)
        self.assertEqual(subprocess.run(["git", "symbolic-ref", "-q", "HEAD"], cwd=path).returncode, 1)
        self.assertEqual(git("remote", cwd=ws.control), "origin")

    def test_missing_pull_ref_falls_back_to_fork_branch(self):
        fork = self.root / "forker" / "typewriter.git"
        git("init", "-q", "--bare", str(fork))
        ws = m.Workspaces(REPO, 1)
        ws.sync()
        self.drop_pull_ref(2, "feature", remote=fork)  # the branch exists only on the fork
        with mock.patch.object(m.GhDiscussion, "_api",
                               return_value=self.metadata(2, "feature", "forker/typewriter")), \
                self.assertLogs("misscat", "INFO"):
            path = ws.prepare(self.prs[2], 1)
        self.assertEqual(git("rev-parse", "HEAD", cwd=path), self.prs[2].head)
        self.assertEqual(git("remote", cwd=ws.control), "origin")  # no fork remote left behind
        self.assertEqual(git("remote", "get-url", "origin", cwd=ws.control), str(self.origin))

    def test_fallback_rejects_a_moved_head(self):
        ws = m.Workspaces(REPO, 1)
        ws.sync()
        self.drop_pull_ref(1, "b1")
        self.drop_pull_ref(2, "b2")
        cases = (self.metadata(1, "b1", sha="f" * 40),  # GitHub already reports a newer HEAD
                 self.metadata(1, "b2"))  # branch tip is not the listed HEAD
        for meta in cases:
            with mock.patch.object(m.GhDiscussion, "_api", return_value=meta), \
                    self.assertRaisesRegex(m.WorkspaceError, "changed during preparation"):
                ws.prepare(self.prs[1], 1)
        self.assertNotEqual(git("rev-parse", "HEAD", cwd=ws.cat_path(1)), self.prs[1].head)

    def test_fallback_failures_fail_closed(self):
        ws = m.Workspaces(REPO, 1)
        ws.sync()
        self.drop_pull_ref(1)  # no source branch anywhere
        cases = (
            mock.patch.object(m.GhDiscussion, "_api", return_value=self.metadata(1, "gone")),
            mock.patch.object(m.GhDiscussion, "_api", return_value=self.metadata(1, "x", source=None)),
            mock.patch.object(m.GhDiscussion, "_api",
                              return_value=self.metadata(1, "x", "nobody/typewriter")),  # no such fork
            mock.patch.object(m.GhDiscussion, "_api", side_effect=m.GhError("HTTP 403")),
            mock.patch.object(m.GhDiscussion, "_api", return_value={"head": None}),
        )
        for case in cases:
            with case, self.assertRaisesRegex(m.WorkspaceError, "source branch fallback failed"):
                ws.prepare(self.prs[1], 1)

    def test_other_fetch_failures_do_not_fall_back(self):
        ws = m.Workspaces(REPO, 1)
        ws.sync()
        git("remote", "set-url", "origin", str(self.root / "missing.git"), cwd=ws.control)
        with mock.patch.object(m.GhDiscussion, "_api", side_effect=AssertionError("no lookup")), \
                self.assertRaises(m.WorkspaceError) as caught:
            ws.prepare(self.prs[1], 1)
        self.assertNotIn("fallback", str(caught.exception))

    SECRET = "FAKE_SECRET_DO_NOT_USE"

    def credential_origin(self, ws):
        url = f"https://reviewer:{self.SECRET}@127.0.0.1:9/genonfire/typewriter.git"
        git("remote", "set-url", "origin", url, cwd=ws.control)
        return url

    def test_fork_fetch_failure_never_logs_url_credentials(self):
        ws = m.Workspaces(REPO, 1)
        ws.sync()
        self.credential_origin(ws)  # nothing listens on port 9: the fork fetch fails
        meta = self.metadata(1, "feature", "forker/typewriter")
        for _ in range(2):  # the retry on the next poll fails the same, still redacted
            with mock.patch.object(m.GhDiscussion, "_api", return_value=meta), \
                    self.assertRaisesRegex(m.WorkspaceError, "source branch fallback failed") as caught:
                ws._fetch_source(self.prs[1])
            self.assertNotIn(self.SECRET, str(caught.exception))
            self.assertNotIn("reviewer", str(caught.exception))
            self.assertIn("forker/typewriter:feature", str(caught.exception))

    def test_fork_fetch_uses_origin_credentials_without_logging_them(self):
        fork = self.root / "forker" / "typewriter.git"
        git("init", "-q", "--bare", str(fork))
        ws = m.Workspaces(REPO, 1)
        ws.sync()
        self.drop_pull_ref(2, "feature", remote=fork)
        url = self.credential_origin(ws)
        fetched = []
        real_git = m._git

        def fake_git(args, cwd):
            if args[0] == "fetch":  # stand in for the authenticated HTTPS fork
                fetched.append(args[1])
                args = ["fetch", str(fork), *args[2:]]
            return real_git(args, cwd)

        with mock.patch.object(m.GhDiscussion, "_api",
                               return_value=self.metadata(2, "feature", "forker/typewriter")), \
                mock.patch.object(m, "_git", fake_git), self.assertLogs("misscat", "INFO") as logs:
            ws._fetch_source(self.prs[2])
        self.assertEqual(fetched, [url.replace("genonfire/typewriter", "forker/typewriter")])
        self.assertEqual(git("rev-parse", "FETCH_HEAD", cwd=ws.control), self.prs[2].head)
        self.assertNotIn(self.SECRET, "\n".join(logs.output))

    def test_git_errors_redact_url_credentials(self):
        with self.assertRaises(m.WorkspaceError) as caught:
            m._git(["fetch", f"https://reviewer:{self.SECRET}@127.0.0.1:9/o/r.git", "main"], self.root)
        self.assertNotIn(self.SECRET, str(caught.exception))
        self.assertIn("https://127.0.0.1:9/o/r.git", str(caught.exception))
        self.assertEqual(m._redact("'https://tok@github.com/o/r' git@github.com:o/r.git"),
                         "'https://github.com/o/r' git@github.com:o/r.git")

    def test_sibling_url_keeps_host_and_protocol(self):
        cases = {
            "https://github.com/genonfire/typewriter.git": "https://github.com/forker/tw.git",
            "https://github.com/Genonfire/Typewriter": "https://github.com/forker/tw",
            "git@github.com:genonfire/typewriter.git": "git@github.com:forker/tw.git",
            "ssh://git@github.com/genonfire/typewriter.git/": "ssh://git@github.com/forker/tw.git",
            "https://u:tok@github.com/genonfire/typewriter.git": "https://u:tok@github.com/forker/tw.git",
            "https://github.com/xgenonfire/typewriter.git": None,
            "https://github.com/other/repo.git": None,
        }
        for origin, want in cases.items():
            self.assertEqual(m._sibling_url(origin, REPO, "forker/tw"), want, origin)

    def test_broken_cat_is_recreated(self):
        ws = m.Workspaces(REPO, 1)
        ws.sync()
        (ws.cat_path(1) / ".git").unlink()
        ws.sync()
        path = ws.prepare(self.prs[1], 1)
        self.assertEqual(git("rev-parse", "HEAD", cwd=path), self.prs[1].head)


if __name__ == "__main__":
    unittest.main()

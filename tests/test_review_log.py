"""Review start separator, elapsed-time logging, and unchanged success/failure semantics."""
import io
import logging
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import misscat as m

REPO = "genonfire/typewriter"


class FormatElapsedTests(unittest.TestCase):
    def test_formats(self):
        f = m._fmt_elapsed
        self.assertEqual(f(0), "0s")
        self.assertEqual(f(59.9), "59s")
        self.assertEqual(f(60), "1m 0s")
        self.assertEqual(f(61), "1m 1s")
        self.assertEqual(f(16 * 60 + 58), "16m 58s")
        self.assertEqual(f(-1), "0s")


class ReviewLogTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.config = Path(tmp.name) / ".config" / "misscat"
        for name, value in (("CONFIG_DIR", self.config),
                            ("LOCK_ROOT", Path(tmp.name) / "locks")):
            patcher = mock.patch.object(m, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.path = m.state_path(REPO)
        self.pr = m.PR(78, "e4e58fa1234", False, "https://github.com/example", "title")
        self.cfg = m.load_settings(None)

    def run_review(self, reviewer, clock):
        stream = io.StringIO()
        handler = logging.StreamHandler(stream)
        handler.setFormatter(logging.Formatter("%(levelname)s %(message)s"))
        old_level = m.log.level
        m.log.addHandler(handler)
        m.log.setLevel(logging.INFO)
        propagate, m.log.propagate = m.log.propagate, False
        try:
            with m.repository_lock(REPO):
                state = m.State(self.path)
                watcher = m.Watcher(REPO, "luna", self.cfg, state, lambda repo: [self.pr],
                                    reviewer, sleep=lambda delay: None)
                with mock.patch.object(m.time, "monotonic", side_effect=clock):
                    result = watcher._review(self.pr)
                return watcher, state, result, stream.getvalue()
        finally:
            m.log.removeHandler(handler)
            m.log.setLevel(old_level)
            m.log.propagate = propagate

    def test_success_logs_separator_start_and_duration_in_order(self):
        watcher, state, ok, out = self.run_review(lambda *a: True, [100.0, 1118.4])
        self.assertTrue(ok)
        self.assertEqual(out, f"\n{m.REVIEW_SEPARATOR}\n"
                              f"INFO PR #78: review start (e4e58fa)\n"
                              f"INFO PR #78: review done in 16m 58s\n")
        self.assertIn(m.Key(78, self.pr.head, "luna"), state.reviewed())

    def test_failure_logs_elapsed_and_stays_eligible(self):
        watcher, state, ok, out = self.run_review(lambda *a: False, [0.0, 42.0])
        self.assertFalse(ok)
        self.assertIn("WARNING PR #78: review failed after 42s, HEAD stays eligible", out)
        self.assertEqual(state.reviewed(), set())
        self.assertEqual(watcher.last_failed, m.Key(78, self.pr.head, "luna"))

    def test_exception_is_failure_with_elapsed(self):
        def boom(*a):
            raise RuntimeError("x")
        watcher, state, ok, out = self.run_review(boom, [0.0, 60.0])
        self.assertFalse(ok)
        self.assertIn("review failed after 1m 0s, HEAD stays eligible", out)
        self.assertEqual(state.reviewed(), set())

    def test_separator_precedes_start_on_log_stream(self):
        stream = io.StringIO()
        handler = logging.StreamHandler(stream)
        handler.setFormatter(logging.Formatter("%(message)s"))
        m.log.addHandler(handler)
        self.addCleanup(m.log.removeHandler, handler)
        m._log_review_separator()
        m.log.warning("after")
        self.assertEqual(stream.getvalue(), f"\n{m.REVIEW_SEPARATOR}\nafter\n")


if __name__ == "__main__":
    unittest.main()

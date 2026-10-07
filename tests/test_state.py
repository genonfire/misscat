"""State v2, deletion order, and single-process locking regressions."""
import json
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest import mock

import misscat


REPO = "genonfire/typewriter"


def row(head, when, profile="luna", pr=213):
    return {"pr": pr, "head": head, "profile": profile, "reviewed_at": when}


class StateManagerTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.config = Path(tmp.name) / ".config" / "misscat"
        patcher = mock.patch.object(misscat, "CONFIG_DIR", self.config)
        patcher.start()
        self.addCleanup(patcher.stop)
        locks = mock.patch.object(misscat, "LOCK_ROOT", Path(tmp.name) / ".cache" / "locks")
        locks.start()
        self.addCleanup(locks.stop)
        self.path = misscat.state_path(REPO)

    def write_state(self, version, rows):
        self.config.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps({"version": version, "reviewed": rows}))

    def test_v1_is_intentionally_discarded_on_first_access(self):
        self.write_state(1, [{"pr": 213, "head": "abc", "profile": "luna"}])
        with misscat.repository_lock(REPO):
            state = misscat.State(self.path)
            self.assertEqual(state.records(), [])
            self.assertEqual(json.loads(self.path.read_text()),
                             {"version": 2, "reviewed": []})

    def test_unknown_state_version_and_corrupt_json_are_not_discarded(self):
        self.write_state(3, [])
        with misscat.repository_lock(REPO):
            with self.assertRaises(misscat.StateError):
                misscat.State(self.path)
        self.assertEqual(json.loads(self.path.read_text())["version"], 3)
        self.path.write_text("{bad json")
        with misscat.repository_lock(REPO):
            with self.assertRaises(misscat.StateError):
                misscat.State(self.path)
        self.assertEqual(self.path.read_text(), "{bad json")

    def test_non_integer_versions_are_rejected_without_reset(self):
        for bad in (True, 1.0, "1", None):
            self.config.mkdir(parents=True, exist_ok=True)
            content = json.dumps({"version": bad, "reviewed": [row("a", "2026-10-02T09:00:00Z")]})
            self.path.write_text(content)
            with misscat.repository_lock(REPO):
                with self.assertRaises(misscat.StateError, msg=repr(bad)):
                    misscat.State(self.path)
            self.assertEqual(self.path.read_text(), content, repr(bad))

    def test_v1_reset_warns_about_token_usage(self):
        self.write_state(1, [])
        with misscat.repository_lock(REPO):
            with self.assertLogs("misscat", "WARNING") as logs:
                misscat.State(self.path)
        self.assertIn("tokens", logs.output[0])

    def test_latest_compares_instants_not_strings(self):
        # 11:00+02:00 is 09:00Z: earlier than 10:00Z although it sorts later as a string
        self.write_state(2, [
            row("early", "2026-10-02T11:00:00+02:00"),
            row("late", "2026-10-02T10:00:00+00:00"),
        ])
        with misscat.repository_lock(REPO):
            state = misscat.State(self.path)
            self.assertFalse(state.remove_latest(misscat.Key(213, "early", "luna")))
            self.assertTrue(state.remove_latest(misscat.Key(213, "late", "luna")))

    def test_lock_does_not_create_config_dir(self):
        with misscat.repository_lock(REPO):
            self.assertFalse(self.config.exists())
            self.assertTrue(any(misscat.LOCK_ROOT.iterdir()))

    def test_state_first_then_watcher_still_installs_profiles(self):
        with misscat.repository_lock(REPO):  # e.g. `misscat state owner/repo` as the first command
            misscat.State(self.path)
        self.assertFalse(self.config.exists())
        with misscat.repository_lock(REPO):  # first watcher start
            misscat.ensure_initial_profiles()
        self.assertTrue((self.config / "luna.yml").is_file())
        self.assertEqual(misscat.load_settings("luna").provider, "codex")

    def test_timestamp_not_sha_controls_latest_and_stack_deletion(self):
        self.write_state(2, [
            row("zzz", "2026-10-02T09:00:00Z"),
            row("aaa", "2026-10-02T10:00:00Z"),
            row("mmm", "2026-10-02T08:00:00Z"),
            row("bbb", "2026-10-02T12:00:00Z", profile="sol"),
        ])
        with misscat.repository_lock(REPO):
            state = misscat.State(self.path)
            self.assertFalse(state.is_latest(misscat.Key(213, "zzz", "luna")))
            self.assertTrue(state.is_latest(misscat.Key(213, "aaa", "luna")))
            self.assertFalse(state.remove_latest(misscat.Key(213, "zzz", "luna")))
            self.assertTrue(state.remove_latest(misscat.Key(213, "aaa", "luna")))
            self.assertTrue(state.is_latest(misscat.Key(213, "zzz", "luna")))
            self.assertTrue(state.remove_latest(misscat.Key(213, "zzz", "luna")))
            self.assertTrue(state.is_latest(misscat.Key(213, "mmm", "luna")))
            self.assertTrue(state.is_latest(misscat.Key(213, "bbb", "sol")))
            self.assertEqual(json.loads(self.path.read_text())["version"], 2)
            self.assertEqual(len(state.records()), 2)

    def test_display_order_is_newest_first_across_prs_and_profiles(self):
        self.write_state(2, [
            row("old25", "2026-10-01T09:00:00Z", profile="luna", pr=25),
            row("new10", "2026-10-03T09:00:00Z", profile="sol", pr=10),
            # same instant as new10, expressed with an offset: 18:00+09:00 == 09:00Z
            row("tieB", "2026-10-03T18:00:00+09:00", profile="luna", pr=10),
            row("tieA", "2026-10-03T09:00:00Z", profile="luna", pr=12),
            row("mid", "2026-10-02T23:00:00-05:00", profile="sol", pr=3),  # 10-03 04:00Z
        ])
        with misscat.repository_lock(REPO):
            state = misscat.State(self.path)
            before = self.path.read_text()
            heads = [r.head for r in misscat.display_order(state.records())]
            # ties: higher PR first, then profile, then head
            self.assertEqual(heads, ["tieA", "tieB", "new10", "mid", "old25"])
            self.assertEqual(heads, [r.head for r in misscat.display_order(
                list(reversed(state.records())))])
            self.assertEqual(self.path.read_text(), before)

    def test_deletion_targets_actual_record_after_reordering(self):
        self.write_state(2, [
            row("old25", "2026-10-01T09:00:00Z", pr=25),
            row("new10", "2026-10-03T09:00:00Z", pr=10),
        ])
        with misscat.repository_lock(REPO):
            state = misscat.State(self.path)
            shown = misscat.display_order(state.records())
            self.assertEqual(shown[0].pr, 10)
            self.assertTrue(state.remove_latest(shown[1].key()))
            self.assertEqual([r.head for r in state.records()], ["new10"])
            self.assertEqual([r["head"] for r in json.loads(self.path.read_text())["reviewed"]],
                             ["new10"])

    def test_successful_review_gets_utc_timestamp(self):
        with misscat.repository_lock(REPO):
            state = misscat.State(self.path)
            key = misscat.Key(215, "fedcba", None)
            state.add(key)
            state.add(key)  # no duplicate timestamps/records
            records = state.records()
            self.assertEqual(len(records), 1)
            timestamp = records[0].reviewed_at
            self.assertTrue(timestamp.endswith("Z"))
            self.assertIsNotNone(datetime.fromisoformat(timestamp.replace("Z", "+00:00")).tzinfo)
            self.assertEqual(json.loads(self.path.read_text())["reviewed"][0]["reviewed_at"],
                             timestamp)

    def test_lock_is_exclusive_and_released(self):
        with misscat.repository_lock(REPO):
            with self.assertRaisesRegex(misscat.StateError, "Stop the watcher"):
                with misscat.repository_lock(REPO):
                    pass
            with misscat.repository_lock("genonfire/other"):
                pass
        with misscat.repository_lock(REPO):
            pass

    def test_watcher_reads_deletion_on_next_start_and_rereviews_only_current_head(self):
        import misscat as m
        cfg = m.load_settings(None)
        pr = m.PR(213, "current", False, "https://github.com/example", "title")
        with m.repository_lock(REPO):
            state = m.State(self.path)
            state.add(m.Key(pr.number, pr.head, "luna"))
            state.add(m.Key(213, "older", "luna"))
            self.assertTrue(state.remove_latest(m.Key(213, "older", "luna")))
            # Deleting an older HEAD in storage doesn't make an already reviewed HEAD eligible
            calls = []
            watcher = m.Watcher(REPO, "luna", cfg, state,
                                lambda repo: [pr],
                                lambda settings, repo, item, workspace: calls.append(item.head) or True,
                                sleep=lambda delay: None)
            watcher.cycle()
            self.assertEqual(calls, [])
            self.assertTrue(state.remove_latest(m.Key(213, "current", "luna")))
        with m.repository_lock(REPO):
            reloaded = m.State(self.path)
            calls = []
            watcher = m.Watcher(REPO, "luna", cfg, reloaded,
                                lambda repo: [pr],
                                lambda settings, repo, item, workspace: calls.append(item.head) or True,
                                sleep=lambda delay: None)
            watcher.cycle()
            self.assertEqual(calls, ["current"])
            self.assertIn(m.Key(213, "current", "luna"), reloaded.reviewed())


if __name__ == "__main__":
    unittest.main()

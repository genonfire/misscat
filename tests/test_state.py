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

    def test_first_run_installs_profiles_despite_lock_creating_config_dir(self):
        first_run = not misscat.CONFIG_DIR.exists()
        self.assertTrue(first_run)
        with misscat.repository_lock(REPO):
            self.assertTrue(self.config.exists())  # the lock created it
            misscat.ensure_initial_profiles(first_run)
        self.assertTrue((self.config / "luna.yml").is_file())

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
                                lambda settings, repo, item: calls.append(item.head) or True,
                                sleep=lambda delay: None)
            watcher.cycle()
            self.assertEqual(calls, [])
            self.assertTrue(state.remove_latest(m.Key(213, "current", "luna")))
        with m.repository_lock(REPO):
            reloaded = m.State(self.path)
            calls = []
            watcher = m.Watcher(REPO, "luna", cfg, reloaded,
                                lambda repo: [pr],
                                lambda settings, repo, item: calls.append(item.head) or True,
                                sleep=lambda delay: None)
            watcher.cycle()
            self.assertEqual(calls, ["current"])
            self.assertIn(m.Key(213, "current", "luna"), reloaded.reviewed())


if __name__ == "__main__":
    unittest.main()

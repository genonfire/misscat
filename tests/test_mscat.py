"""mscat: OpenCode-driven reviewer reusing MissCat's watcher, State and lock."""
import json
import os
import stat
import subprocess
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest import mock

import misscat as m
import mscat

ROOT = Path(__file__).resolve().parent.parent
REPO = "genonfire/typewriter"
HEAD = "a" * 40


class ConfigTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.cfg = Path(tmp.name) / ".config" / "misscat"
        patcher = mock.patch.object(m, "CONFIG_DIR", self.cfg)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_fresh_install_has_profiles_and_opencode(self):
        mscat.ensure_config()
        names = {p.name for p in self.cfg.iterdir()}
        self.assertEqual(names, {"luna.yml", "sol.yml", "sonnet.yml", "opus.yml", "gemini.yml",
                                 "opencode.yml"})

    def test_existing_dir_gets_only_opencode(self):
        self.cfg.mkdir(parents=True)
        (self.cfg / "custom.yml").write_text("x: 1\n")
        mscat.ensure_config()
        self.assertEqual({p.name for p in self.cfg.iterdir()}, {"custom.yml", "opencode.yml"})

    def test_existing_opencode_never_overwritten(self):
        self.cfg.mkdir(parents=True)
        (self.cfg / "opencode.yml").write_text("reviewer: {model: mine/x}\n")
        mscat.ensure_config()
        self.assertEqual((self.cfg / "opencode.yml").read_text(), "reviewer: {model: mine/x}\n")
        self.assertEqual(mscat.load_settings().model, "mine/x")

    def test_invalid_user_file_is_a_config_error_and_kept(self):
        self.cfg.mkdir(parents=True)
        (self.cfg / "opencode.yml").write_text("reviewer: {provider: codex}\n")
        mscat.ensure_config()
        with self.assertRaises(m.ConfigError):
            mscat.load_settings()
        (self.cfg / "opencode.yml").write_text("- a list\n")
        with self.assertRaises(m.ConfigError):
            mscat.load_settings()
        self.assertEqual((self.cfg / "opencode.yml").read_text(), "- a list\n")

    def test_later_misscat_init_still_installs_profiles(self):
        mscat.ensure_config()
        (self.cfg / "luna.yml").unlink()
        self.assertEqual(m.run_init([]), 0)
        self.assertTrue((self.cfg / "luna.yml").exists())
        self.assertNotIn("opencode.yml", m.bundled_profiles())

    def test_template_defaults(self):
        mscat.ensure_config()
        s = mscat.load_settings()
        self.assertEqual((s.provider, s.model, s.args), ("opencode", "openai/gpt-6-luna#xhigh", ()))
        self.assertTrue(s.include_drafts)

    def test_help_and_version_have_no_side_effects(self):
        with tempfile.TemporaryDirectory() as home:
            env = {**os.environ, "HOME": home}
            for flag in ("--help", "--version"):
                subprocess.run([sys.executable, "-m", "mscat", flag], env=env,
                               cwd=ROOT / "src", capture_output=True, check=True)
            self.assertFalse((Path(home) / ".config").exists())


class RunnerTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.bin = self.root / "bin"
        self.bin.mkdir()
        self.work = self.root / "work"
        self.work.mkdir()
        self.settings = m.Settings("opencode", "openai/gpt-6-luna#xhigh", "PROMPT", (1.0,),
                                   (1.0,), True, ("--foo",))
        self.pr = m.PR(7, HEAD, False, "u", "t", run_id="r1")

    def fake(self, lines, code=0):
        script = self.bin / "opencode"
        script.write_text("#!/bin/sh\npwd > \"$FAKE_OUT\"\nprintf '%s\\n' \"$@\" >> \"$FAKE_OUT\"\n"
                          + "".join(f"echo '{line}'\n" for line in lines) + f"exit {code}\n")
        script.chmod(script.stat().st_mode | stat.S_IEXEC)
        out = self.root / "out"
        return mock.patch.dict(os.environ, {"PATH": f"{self.bin}:{os.environ['PATH']}",
                                            "FAKE_OUT": str(out)}), out

    def test_command_shape(self):
        cmd = mscat.command(self.settings, "P")
        self.assertEqual(cmd, ["opencode", "run", "--standalone", "--format", "json", "--foo",
                               "--model", "openai/gpt-6-luna#xhigh", "P"])

    def test_runs_in_workspace_with_prompt_and_logs_usage(self):
        steps = [json.dumps(e) for e in (
            {"type": "step_start"},
            {"type": "tool_use", "part": {"tool": "bash", "state": {"status": "completed",
                                                                    "input": {"command": "gh pr view"}}}},
            {"type": "step_finish", "part": {"cost": 0.01, "tokens": {"input": 10, "output": 5}}},
            {"type": "step_finish", "part": {"cost": 0.02, "tokens": {"input": 20, "output": 1}}},
        )]
        env, out = self.fake(steps)
        with env, self.assertLogs("misscat", "INFO") as logs:
            ok = mscat.OpenCodeReviewer()(self.settings, REPO, self.pr, self.work)
        self.assertTrue(ok)
        lines = out.read_text().splitlines()
        self.assertEqual(Path(lines[0]).resolve(), self.work.resolve())
        self.assertEqual(lines[1:8], ["run", "--standalone", "--format", "json", "--foo", "--model",
                                      "openai/gpt-6-luna#xhigh"])
        self.assertIn("MissCat-Run: r1", "\n".join(lines))
        self.assertNotIn("--continue", lines)
        done = [r for r in logs.output if "opencode done" in r][0]
        self.assertIn("input=30", done)
        self.assertIn("output=6", done)
        self.assertIn("cost≈$0.0300 (CLI estimate)", done)

    def test_child_pwd_is_the_worktree_not_the_callers(self):
        script = self.bin / "opencode"  # no shell: nothing repairs PWD for us
        script.write_text(f"#!{sys.executable}\nimport os\nopen(os.environ['FAKE_OUT'], 'w')"
                          ".write(os.getcwd() + '\\n' + os.environ['PWD'])\n")
        script.chmod(script.stat().st_mode | stat.S_IEXEC)
        out = self.root / "out"
        caller = self.root / "caller"
        caller.mkdir()
        env = {"PATH": f"{self.bin}:{os.environ['PATH']}", "FAKE_OUT": str(out), "PWD": str(caller)}
        with mock.patch.dict(os.environ, env):
            self.assertTrue(mscat.OpenCodeReviewer()(self.settings, REPO, self.pr, self.work))
            self.assertEqual(os.environ["PWD"], str(caller))  # parent untouched
        cwd, pwd = out.read_text().split("\n")
        self.assertEqual(Path(cwd).resolve(), self.work.resolve())
        self.assertEqual(pwd, str(self.work))

    def test_no_route_is_fatal_and_pointed_at_the_config(self):
        err = json.dumps({"type": "error", "error": {"type": "provider.no-route", "message": "x"}})
        env, _ = self.fake([err])
        reviewer = mscat.OpenCodeReviewer()
        with env, self.assertLogs("misscat", "ERROR"):
            self.assertFalse(reviewer(self.settings, REPO, self.pr, self.work))
        with self.assertRaises(m.MissCatError) as ctx:
            reviewer.check()
        self.assertIn("reviewer.model", str(ctx.exception))

    def test_missing_usage_is_not_zero(self):
        env, _ = self.fake([json.dumps({"type": "step_start"})])
        with env, self.assertLogs("misscat", "INFO") as logs:
            self.assertTrue(mscat.OpenCodeReviewer()(self.settings, REPO, self.pr, self.work))
        done = [r for r in logs.output if "opencode done" in r][0]
        self.assertNotIn("input", done)
        self.assertNotIn("cost", done)

    def test_nonzero_exit_fails_and_redacts(self):
        env, _ = self.fake(["see https://user:tok@github.com/x"], code=3)
        with env, self.assertLogs("misscat", "ERROR") as logs:
            self.assertFalse(mscat.OpenCodeReviewer()(self.settings, REPO, self.pr, self.work))
        self.assertIn("exited 3", logs.output[0])
        self.assertNotIn("tok", logs.output[0])

    def test_error_event_fails_even_with_exit_zero(self):
        err = json.dumps({"type": "error", "error": {"type": "api", "message": "boom"}})
        env, _ = self.fake([err])
        reviewer = mscat.OpenCodeReviewer()
        with env, self.assertLogs("misscat", "ERROR"):
            self.assertFalse(reviewer(self.settings, REPO, self.pr, self.work))
        reviewer.check()  # transient: no stop

    def test_fatal_error_stops_dispatch_with_advice(self):
        err = json.dumps({"type": "error", "error": {"type": "provider.auth", "message": "no key"}})
        env, _ = self.fake([err])
        reviewer = mscat.OpenCodeReviewer()
        with env, self.assertLogs("misscat", "ERROR"):
            self.assertFalse(reviewer(self.settings, REPO, self.pr, self.work))
        with self.assertRaises(m.MissCatError) as ctx:
            reviewer.check()
        self.assertIn("opencode auth login", str(ctx.exception))

    def test_tool_failure_is_a_warning(self):
        ev = json.dumps({"type": "tool_use", "part": {"tool": "bash", "state": {
            "status": "error", "error": "permission rejected", "input": {"command": "rm x"}}}})
        env, _ = self.fake([ev])
        with env, self.assertLogs("misscat.activity", "WARNING") as logs:
            self.assertTrue(mscat.OpenCodeReviewer()(self.settings, REPO, self.pr, self.work))
        self.assertIn("permission rejected", logs.output[0])

    def test_missing_executable(self):
        with mock.patch.dict(os.environ, {"PATH": str(self.bin)}), self.assertLogs("misscat", "ERROR"):
            self.assertFalse(mscat.OpenCodeReviewer()(self.settings, REPO, self.pr, self.work))


class WatcherIntegrationTest(unittest.TestCase):
    """mscat's profile identity: confirmation, recovery and coexistence with MissCat profiles."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        for name, value in (("CONFIG_DIR", root / "config"), ("LOCK_ROOT", root / "locks")):
            patcher = mock.patch.object(m, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        mscat.ensure_config()
        self.settings = mscat.load_settings()
        self.state = m.State(m.state_path(REPO))
        self.pr = m.PR(5, HEAD, False, "u", "t")

    def run_watcher(self, reviewer, reviews, profile=mscat.PROFILE):
        disc = mock.Mock()
        disc.reviews.side_effect = lambda n: reviews()
        disc.head.return_value = HEAD
        w = m.Watcher(REPO, profile, self.settings, self.state, lambda r: [self.pr], reviewer,
                      sleep=lambda s: None, discussion=disc, workspaces=mock.Mock(
                          prepare=lambda pr, cat: Path("/cat")))
        w.cycle()
        return w

    def test_valid_review_records_namespaced_profile(self):
        seen = {}

        def reviewer(settings, repo, pr, ws):
            seen["run"] = pr.run_id
            return True

        body = lambda: f"+1\nHEAD: {HEAD}\n\nevidence\nMissCat-Run: {seen['run']}"
        self.run_watcher(reviewer, lambda: [m.GhReview(9, "me", "COMMENTED", body(), HEAD, "2026-01-01T00:00:00Z")])
        self.assertEqual(self.state.reviewed(), {m.Key(5, HEAD, "mscat/opencode")})

    def test_exit_zero_without_review_is_not_recorded(self):
        self.run_watcher(lambda *a: True, lambda: [])
        self.assertEqual(self.state.reviewed(), set())
        self.assertEqual(self.state.pending(), [])

    def test_review_submitted_before_failed_exit_still_settles(self):
        seen = {}

        def reviewer(settings, repo, pr, ws):
            seen["run"] = pr.run_id
            return False

        body = lambda: f"-1\nHEAD: {HEAD}\n\nbad\nMissCat-Run: {seen['run']}"
        self.run_watcher(reviewer, lambda: [m.GhReview(9, "me", "COMMENTED", body(), HEAD, "2026-01-01T00:00:00Z")])
        self.assertEqual(len(self.state.reviewed()), 1)

    def test_wrong_head_not_recorded(self):
        seen = {}

        def reviewer(settings, repo, pr, ws):
            seen["run"] = pr.run_id
            return True

        body = lambda: f"+1\nHEAD: {'b' * 40}\n\nx\nMissCat-Run: {seen['run']}"
        self.run_watcher(reviewer, lambda: [m.GhReview(9, "me", "COMMENTED", body(), "b" * 40, "2026-01-01T00:00:00Z")])
        self.assertEqual(self.state.reviewed(), set())

    def test_misscat_profile_does_not_suppress_mscat(self):
        self.state.add(m.Key(5, HEAD, "luna"))
        w = m.Watcher(REPO, mscat.PROFILE, self.settings, self.state, lambda r: [self.pr],
                      lambda *a: True, sleep=lambda s: None)
        self.assertEqual(w._next_batch([self.pr]), [self.pr])

    def test_shared_lock_blocks_second_process(self):
        with m.repository_lock(REPO):
            with self.assertRaises(m.StateError):
                with m.repository_lock(REPO):
                    pass


class MainTest(unittest.TestCase):
    def test_fatal_error_ends_watch_with_message(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        patches = [mock.patch.object(m, "CONFIG_DIR", root / "config"),
                   mock.patch.object(m, "LOCK_ROOT", root / "locks"),
                   mock.patch.object(m, "WORKSPACE_ROOT", root / "repos"),
                   mock.patch.object(m, "CAT_ROOT", root / "cats"),
                   mock.patch("shutil.which", return_value="/x"),
                   mock.patch.object(m.Workspaces, "sync"),
                   mock.patch.object(m.Workspaces, "prepare", return_value=root),
                   mock.patch.object(mscat, "gh_open_prs", return_value=[m.PR(1, HEAD, False, "u", "t")]),
                   mock.patch.object(m.GhDiscussion, "reviews", return_value=[]),
                   mock.patch.object(m.GhDiscussion, "head", return_value=HEAD)]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)

        def fail(self_, settings, repo, pr, ws):
            self_.fatal = "opencode provider.auth: nope"
            return False

        with mock.patch.object(mscat.OpenCodeReviewer, "__call__", fail), \
                mock.patch("sys.stderr") as err, mock.patch("time.sleep") as sleep:
            self.assertEqual(mscat.main([REPO]), 2)
        sleep.assert_not_called()
        self.assertIn("provider.auth", "".join(c.args[0] for c in err.write.call_args_list))


class WheelTest(unittest.TestCase):
    def test_wheel_exposes_mscat(self):
        with tempfile.TemporaryDirectory() as tmp:
            build = subprocess.run([sys.executable, "-m", "pip", "wheel", "--no-deps", "-w", tmp, str(ROOT)],
                                   capture_output=True, text=True)
            if build.returncode and os.environ.get("MISSCAT_SKIP_WHEEL_TEST"):
                self.skipTest("wheel build explicitly skipped (MISSCAT_SKIP_WHEEL_TEST)")
            self.assertEqual(build.returncode, 0, build.stderr[-1000:])
            wheel = next(Path(tmp).glob("misscat-*.whl"))
            with zipfile.ZipFile(wheel) as z:
                names = set(z.namelist())
                entry = next(n for n in names if n.endswith("entry_points.txt"))
                self.assertIn("mscat = mscat:main", z.read(entry).decode())
            self.assertIn("mscat/opencode.yml", names)
            self.assertNotIn("misscat/profiles/opencode.yml", names)


if __name__ == "__main__":
    unittest.main()

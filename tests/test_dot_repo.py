"""Resolution of `.` to the origin remote."""
import contextlib
import io
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import misscat


class TtyBuffer(io.StringIO):
    def isatty(self):
        return True


def git(cwd, *args):
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True)


class ParseRemoteTests(unittest.TestCase):
    def test_supported_forms(self):
        for url in (
            "git@github.com:Neverworkalone/Typewriter.git",
            "git@github.com:neverworkalone/typewriter",
            "https://github.com/neverworkalone/typewriter.git",
            "https://github.com/neverworkalone/typewriter",
            "https://user:token@github.com/neverworkalone/typewriter.git",
            "ssh://git@github.com/neverworkalone/typewriter.git",
            "ssh://git@github.com/neverworkalone/typewriter",
            "https://github.com/neverworkalone/typewriter/",
        ):
            with self.subTest(url=url):
                self.assertEqual(misscat.parse_github_remote(url), "neverworkalone/typewriter")

    def test_rejected_forms_do_not_leak_url(self):
        for url in (
            "https://user:secret@gitlab.com/o/r.git",
            "git@work-alias:o/r.git",
            "https://github.com.evil.com/o/r",
            "https://github.com/onlyowner",
            "/local/path/repo.git",
            "git@github.com:o/r/extra.git",
        ):
            with self.subTest(url=url):
                with self.assertRaises(misscat.MissCatError) as ctx:
                    misscat.parse_github_remote(url)
                self.assertNotIn("secret", str(ctx.exception))
                self.assertNotIn(url, str(ctx.exception))


class ResolveDotTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name).resolve()
        self.repo = self.root / "checkout"
        self.repo.mkdir()
        git(self.repo, "init", "-q")
        git(self.repo, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "--allow-empty", "-m", "x")
        self.config = self.root / "config"
        for name, value in (("CONFIG_DIR", self.config), ("LOCK_ROOT", self.root / "locks"),
                            ("WORKSPACE_ROOT", self.root / "ws")):
            p = mock.patch.object(misscat, name, value)
            p.start()
            self.addCleanup(p.stop)
        self.addCleanup(os.chdir, os.getcwd())

    def resolve(self, cwd):
        os.chdir(cwd)
        with contextlib.redirect_stdout(io.StringIO()) as out:
            return misscat.resolve_repo_arg("."), out.getvalue()

    def test_forked_origin_ignores_upstream_and_prints_resolution(self):
        git(self.repo, "remote", "add", "origin", "git@github.com:me/typewriter.git")
        git(self.repo, "remote", "add", "upstream", "git@github.com:neverworkalone/typewriter.git")
        repo, out = self.resolve(self.repo)
        self.assertEqual(repo, "me/typewriter")
        self.assertIn("resolved . -> me/typewriter", out)
        self.assertEqual(repo, misscat.resolve_repo_arg("Me/Typewriter"))
        self.assertEqual(misscat.state_path(repo), misscat.state_path("Me/Typewriter"))
        self.assertEqual(misscat.workspace_path(repo), misscat.workspace_path("ME/typewriter"))

    def test_subdirectory_and_worktree(self):
        git(self.repo, "remote", "add", "origin", "https://github.com/me/typewriter.git")
        sub = self.repo / "a" / "b"
        sub.mkdir(parents=True)
        self.assertEqual(self.resolve(sub)[0], "me/typewriter")
        wt = self.root / "wt"
        git(self.repo, "worktree", "add", "-q", str(wt), "-b", "feature")
        self.assertEqual(self.resolve(wt)[0], "me/typewriter")

    def test_errors_have_no_side_effects(self):
        outside = self.root / "outside"
        outside.mkdir()
        cases = [outside, self.repo]  # not a repo; no origin
        git(self.repo, "remote", "add", "upstream", "git@github.com:a/b.git")
        for cwd in cases:
            with self.subTest(cwd=cwd.name), self.assertRaises(misscat.MissCatError):
                self.resolve(cwd)
        git(self.repo, "remote", "add", "origin", "https://u:secret@gitlab.com/a/b.git")
        with self.assertRaises(misscat.MissCatError) as ctx:
            self.resolve(self.repo)
        self.assertNotIn("secret", str(ctx.exception))

    def test_git_missing(self):
        with mock.patch.object(misscat.subprocess, "run", side_effect=FileNotFoundError):
            with self.assertRaises(misscat.MissCatError):
                misscat.resolve_repo_arg(".")

    def test_cli_failure_before_state_lock_or_workspace(self):
        os.chdir(self.repo)  # no origin
        for argv in (["."], [".", "luna"], ["state", "."]):
            with self.subTest(argv=argv), contextlib.redirect_stderr(io.StringIO()):
                with mock.patch.object(misscat, "repository_lock") as lock, \
                     mock.patch.object(misscat, "ensure_initial_profiles") as init:
                    self.assertEqual(misscat.main(argv), 2)
                    lock.assert_not_called()
                    init.assert_not_called()
        for p in (self.config, self.root / "locks", self.root / "ws"):
            self.assertFalse(p.exists())

    def test_state_dot_uses_same_identity_as_explicit(self):
        git(self.repo, "remote", "add", "origin", "git@github.com:Me/Typewriter.git")
        os.chdir(self.repo)
        seen = []
        for arg in (".", "me/typewriter"):
            with mock.patch.object(misscat, "repository_lock", side_effect=lambda r: seen.append(r) or contextlib.nullcontext()), \
                 mock.patch.object(misscat, "run_state_ui", return_value=0), \
                 mock.patch.object(misscat, "State"), \
                 mock.patch.object(misscat.sys.stdin, "isatty", return_value=True), \
                 contextlib.redirect_stdout(TtyBuffer()):
                self.assertEqual(misscat.main(["state", arg]), 0)
        self.assertEqual(seen, ["me/typewriter", "me/typewriter"])


if __name__ == "__main__":
    unittest.main()

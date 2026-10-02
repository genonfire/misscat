import os
import subprocess
import sys
import tempfile
import unittest
import venv
import zipfile
from pathlib import Path
from unittest import mock

import misscat

ROOT = Path(__file__).resolve().parent.parent
BUNDLED = {"luna.yml", "sol.yml", "sonnet.yml", "opus.yml", "gemini.yml"}


DEFAULT_PROMPT = (
    "Read REVIEW.md if exist and act as the 1st reviewer.\n"
    "Use the gh CLI for GitHub operations, including posting the review.\n"
)


class InitTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.home = Path(tmp.name)
        self.cfg = self.home / ".config" / "misscat"
        patcher = mock.patch.object(misscat, "CONFIG_DIR", self.cfg)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_bundled_set_excludes_default(self):
        self.assertEqual(set(misscat.bundled_profiles()), BUNDLED)

    def test_profiles_are_explicit_and_valid(self):
        import yaml
        for name, text in misscat.bundled_profiles().items():
            reviewer = yaml.safe_load(text)["reviewer"]
            self.assertEqual({"provider", "model", "args"}, set(reviewer), name)
            self.assertEqual(yaml.safe_load(text)["prompt"], DEFAULT_PROMPT, name)
            misscat.build_settings(misscat.deep_merge(misscat._load_default_config(), yaml.safe_load(text)))

    def test_sonnet_profile_uses_medium_effort(self):
        import yaml
        args = yaml.safe_load(misscat.bundled_profiles()["sonnet.yml"])["reviewer"]["args"]
        self.assertEqual(args, ["--effort", "medium", "--allowedTools", "Bash(gh *)"])

    def test_opus_profile_settings_and_command(self):
        import yaml
        cfg = misscat.deep_merge(misscat._load_default_config(),
                                 yaml.safe_load(misscat.bundled_profiles()["opus.yml"]))
        settings = misscat.build_settings(cfg)
        self.assertEqual((settings.provider, settings.model), ("claude", "claude-opus-5-5"))
        self.assertEqual(settings.args, ("--effort", "medium", "--allowedTools", "Bash(gh *)"))
        cmd = misscat.COMMANDS[settings.provider](settings.model, "PROMPT", list(settings.args))
        self.assertEqual(cmd[:5], ["claude", "--effort", "medium", "--allowedTools", "Bash(gh *)"])
        self.assertEqual(cmd[5:9], ["-p", "PROMPT", "--model", "claude-opus-5-5"])

    def test_first_run_creates_all(self):
        misscat.ensure_initial_profiles()
        self.assertEqual({p.name for p in self.cfg.iterdir()}, BUNDLED)

    def test_existing_dir_untouched_on_normal_run(self):
        self.cfg.mkdir(parents=True)
        (self.cfg / "custom.yml").write_text("x: 1\n")
        misscat.ensure_initial_profiles()
        self.assertEqual([p.name for p in self.cfg.iterdir()], ["custom.yml"])

    def test_init_fills_missing_only(self):
        self.cfg.mkdir(parents=True)
        (self.cfg / "sol.yml").write_bytes(b"mine: true\r\n")
        installed, skipped = misscat.install_profiles()
        self.assertEqual(skipped, ["sol.yml"])
        self.assertEqual(set(installed), BUNDLED - {"sol.yml"})
        self.assertEqual((self.cfg / "sol.yml").read_bytes(), b"mine: true\r\n")

    def test_force_overwrites_bundled_only(self):
        self.cfg.mkdir(parents=True)
        (self.cfg / "sol.yml").write_text("mine: true\n")
        (self.cfg / "custom.yml").write_text("keep\n")
        (self.cfg / "o__r.json").write_text("{}")
        misscat.install_profiles(force=True)
        self.assertEqual((self.cfg / "sol.yml").read_text(), misscat.bundled_profiles()["sol.yml"])
        self.assertEqual((self.cfg / "custom.yml").read_text(), "keep\n")
        self.assertEqual((self.cfg / "o__r.json").read_text(), "{}")
        self.assertFalse([p for p in self.cfg.iterdir() if p.name.endswith(".tmp")])

    def test_normal_init_never_replaces_concurrently_created_file(self):
        self.cfg.mkdir(parents=True)
        target = self.cfg / "sol.yml"
        real_link = os.link

        def racing_link(src, dst, *a, **k):
            if Path(dst) == target:
                target.write_text("created meanwhile\n")
            return real_link(src, dst, *a, **k)

        with mock.patch("os.link", racing_link):
            _, skipped = misscat.install_profiles()
        self.assertIn("sol.yml", skipped)
        self.assertEqual(target.read_text(), "created meanwhile\n")

    def test_partial_directory_recovered_by_init(self):
        self.cfg.mkdir(parents=True)
        (self.cfg / "luna.yml").write_text(misscat.bundled_profiles()["luna.yml"])
        misscat.ensure_initial_profiles()  # dir exists: no automatic repair
        self.assertEqual([p.name for p in self.cfg.iterdir()], ["luna.yml"])
        self.assertEqual(misscat.run_init([]), 0)
        self.assertEqual({p.name for p in self.cfg.iterdir()}, BUNDLED)

    def test_copy_error_is_actionable(self):
        self.home.joinpath(".config").write_text("a file, not a dir")
        with self.assertRaises(misscat.ConfigError) as ctx:
            misscat.install_profiles()
        self.assertIn("misscat init", str(ctx.exception))
        with mock.patch("sys.stderr"):
            self.assertEqual(misscat.run_init([]), 2)

    def test_help_and_version_have_no_side_effects(self):
        env = {**os.environ, "HOME": str(self.home)}
        for flag in ("--help", "--version", "init --help"):
            subprocess.run([sys.executable, "-m", "misscat", *flag.split()], env=env,
                           cwd=ROOT / "src", capture_output=True, check=True)
        subprocess.run([sys.executable, "-m", "misscat"], env=env, cwd=ROOT / "src", capture_output=True)
        self.assertFalse((self.home / ".config").exists())

    def test_wheel_contains_and_serves_profiles(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            build = subprocess.run(
                [sys.executable, "-m", "pip", "wheel", "--no-deps",
                 "-w", str(tmp / "dist"), str(ROOT)], capture_output=True, text=True)
            if build.returncode and os.environ.get("MISSCAT_SKIP_WHEEL_TEST"):
                self.skipTest("wheel build explicitly skipped (MISSCAT_SKIP_WHEEL_TEST)")
            self.assertEqual(build.returncode, 0, build.stderr[-1000:])
            wheel = next((tmp / "dist").glob("misscat-*.whl"))
            names = {Path(n).name for n in zipfile.ZipFile(wheel).namelist() if "/profiles/" in n}
            self.assertEqual(names, BUNDLED)
            site = tmp / "site"
            with zipfile.ZipFile(wheel) as z:
                z.extractall(site)
            home = tmp / "home"
            env = {**os.environ, "HOME": str(home), "PYTHONPATH": str(site)}
            out = subprocess.run([sys.executable, "-c", "import misscat;print(misscat.__file__);"
                                  "raise SystemExit(misscat.run_init([]))"],
                                 env=env, cwd=tmp, capture_output=True, text=True, check=True)
            self.assertIn(str(site), out.stdout)
            self.assertEqual({p.name for p in (home / ".config" / "misscat").iterdir()}, BUNDLED)


if __name__ == "__main__":
    unittest.main()

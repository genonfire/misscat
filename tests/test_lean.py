"""reviewer.lean: token-saving Claude/Codex options, checked once per process, never blocking."""
import io
import logging
import subprocess
import unittest
from pathlib import Path
from unittest import mock

import misscat as m

PR = m.PR(number=7, head="abc", draft=False, url="https://github.com/o/r/pull/7", title="t")
CLAUDE_HELP = """Usage: claude [options]
  --strict-mcp-config                   Only use MCP servers from --mcp-config
  --exclude-dynamic-system-prompt-sections
  --disable-slash-commands              Disable all skills and commands
"""
CODEX_FEATURES = """apps                                     stable             true
browser_use                              stable             true
multi_agent                              deprecated         true
sleep_tool                               under development  true
plugins                                  removed            false
unrelated                                stable             true
"""


def settings(provider="codex", lean=True, args=("--sandbox", "workspace-write")):
    cfg = m._load_default_config()
    cfg["reviewer"] = {"provider": provider, "model": "x", "args": list(args), "lean": lean}
    return m.build_settings(cfg)


class LeanConfigTests(unittest.TestCase):
    def test_bundled_default_enables_lean(self):
        self.assertTrue(m.load_settings(None).lean)

    def test_missing_key_is_off_and_non_bool_rejected(self):
        cfg = m._load_default_config()
        del cfg["reviewer"]["lean"]
        self.assertFalse(m.build_settings(cfg).lean)
        cfg["reviewer"]["lean"] = "yes"
        with self.assertRaisesRegex(m.ConfigError, "reviewer.lean"):
            m.build_settings(cfg)

    def test_profile_inherits_lean_and_can_opt_out(self):
        base = m._load_default_config()
        profile = {"reviewer": {"provider": "claude", "model": "x", "args": []}}
        self.assertTrue(m.build_settings(m.deep_merge(base, profile)).lean)
        profile["reviewer"]["lean"] = False
        self.assertFalse(m.build_settings(m.deep_merge(base, profile)).lean)


class LeanCommandTests(unittest.TestCase):
    def run_reviewer(self, s, lean=("--L",)):
        with mock.patch.object(m, "lean_args", return_value=lean) as detect, \
                mock.patch.object(m, "_run_structured_reviewer", return_value=True) as run:
            self.assertTrue(m.run_cli_reviewer(s, "o/r", PR, Path("/w")))
        provider, cmd, workspace, env = run.call_args.args
        return cmd, env, detect

    def test_codex_lean_args_precede_user_args(self):
        cmd, env, _ = self.run_reviewer(settings("codex"))
        self.assertEqual(cmd[:5], ["codex", "exec", "--json", "--L", "--sandbox"])
        self.assertIsNone(env)

    def test_claude_gets_flags_and_5m_cache_env(self):
        cmd, env, _ = self.run_reviewer(settings("claude", args=("--effort", "medium")))
        self.assertEqual(cmd[:4], ["claude", "--L", "--effort", "medium"])
        self.assertEqual(env, {"FORCE_PROMPT_CACHING_5M": "1"})

    def test_lean_off_is_the_previous_command(self):
        for provider in ("claude", "codex", "gemini"):
            s = settings(provider, lean=False)
            cmd, env, detect = self.run_reviewer(s)
            expected = m.COMMANDS[provider](s.model, m.build_prompt(s, "o/r", PR), list(s.args))
            self.assertEqual(cmd, expected)
            self.assertIsNone(env)
            detect.assert_not_called()

    def test_gemini_and_opencode_get_nothing(self):
        with mock.patch.object(m, "_probe") as probe:
            self.assertEqual(m.lean_args("gemini"), ())
            self.assertEqual(m.lean_args("opencode"), ())
        probe.assert_not_called()
        cmd, env, _ = self.run_reviewer(settings("gemini"), lean=())
        self.assertIsNone(env)
        self.assertNotIn("--L", cmd)

    def test_env_is_merged_into_the_process_environment(self):
        proc = mock.Mock(stdout=iter([]))
        proc.wait.return_value = 0
        with mock.patch.object(m.subprocess, "Popen", return_value=proc) as popen, \
                mock.patch.dict(m.os.environ, {"KEEP": "1"}):
            m._run_structured_reviewer("claude", ["claude"], Path("/w"), {"FORCE_PROMPT_CACHING_5M": "1"})
            env = popen.call_args.kwargs["env"]
            self.assertEqual((env["KEEP"], env["FORCE_PROMPT_CACHING_5M"]), ("1", "1"))
            m._run_structured_reviewer("codex", ["codex"], Path("/w"))
            self.assertIsNone(popen.call_args.kwargs["env"])


class LeanPreflightTests(unittest.TestCase):
    def setUp(self):
        patcher = mock.patch.dict(m._lean_cache, clear=True)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_claude_applies_only_supported_flags(self):
        with mock.patch.object(m, "_probe", return_value=CLAUDE_HELP):
            self.assertEqual(m.lean_args("claude"),
                             ("--strict-mcp-config", "--exclude-dynamic-system-prompt-sections"))
        old = "  --strict-mcp-config-ish  x\n  --strict-mcp-config  Only use\n"
        m._lean_cache.clear()
        with mock.patch.object(m, "_probe", return_value=old):
            self.assertEqual(m.lean_args("claude"), ("--strict-mcp-config",))

    def test_codex_disables_only_listed_non_removed_features(self):
        with mock.patch.object(m, "_probe", return_value=CODEX_FEATURES) as probe:
            args = m.lean_args("codex")
        probe.assert_called_once_with(["codex", "features", "list"])
        self.assertEqual(args, (
            "-c", 'web_search="disabled"', "-c", "skills.include_instructions=false",
            "-c", "features.apps=false", "-c", "features.multi_agent=false",
            "-c", "features.browser_use=false", "-c", "features.sleep_tool=false",
        ))

    def test_checked_once_per_process(self):
        with mock.patch.object(m, "_probe", return_value=CODEX_FEATURES) as probe:
            first, second = m.lean_args("codex"), m.lean_args("codex")
        self.assertEqual(first, second)
        probe.assert_called_once()

    def test_failed_check_warns_once_and_reviews_without_options(self):
        stream = io.StringIO()
        handler = logging.StreamHandler(stream)
        m.log.addHandler(handler)
        self.addCleanup(m.log.removeHandler, handler)
        with mock.patch.object(m, "_probe", return_value=None) as probe:
            self.assertEqual(m.lean_args("claude"), ())
            self.assertEqual(m.lean_args("claude"), ())
            self.assertEqual(m.lean_args("codex"), ())
        self.assertEqual(probe.call_count, 2)
        self.assertEqual(stream.getvalue().count("lean: no supported claude options"), 1)

    def test_probe_never_raises(self):
        for effect in (OSError("missing"), subprocess.TimeoutExpired("x", 60)):
            with mock.patch.object(m.subprocess, "run", side_effect=effect):
                self.assertIsNone(m._probe(["x"]))
        with mock.patch.object(m.subprocess, "run",
                               return_value=subprocess.CompletedProcess(["x"], 1, "out", "")):
            self.assertIsNone(m._probe(["x"]))
        with mock.patch.object(m.subprocess, "run",
                               return_value=subprocess.CompletedProcess(["x"], 0, "out", "")):
            self.assertEqual(m._probe(["x"]), "out")


class UsageLogTests(unittest.TestCase):
    def capture(self, emit):
        stream = io.StringIO()
        handler = logging.StreamHandler(stream)
        m.log.addHandler(handler)
        level, propagate = m.log.level, m.log.propagate
        m.log.setLevel(logging.INFO)
        m.log.propagate = False
        try:
            emit()
        finally:
            m.log.removeHandler(handler)
            m.log.setLevel(level)
            m.log.propagate = propagate
        return stream.getvalue()

    def test_codex_usage(self):
        out = self.capture(lambda: m._log_codex_event({"type": "turn.completed", "usage": {
            "input_tokens": 100, "cached_input_tokens": 60, "output_tokens": 5}}))
        self.assertIn("reviewer: turn done (input=100, cached=60, uncached=40, output=5)", out)

    def test_claude_usage(self):
        out = self.capture(lambda: m._log_claude_event({
            "type": "result", "num_turns": 3, "total_cost_usd": 0.25, "usage": {
                "input_tokens": 2, "cache_creation_input_tokens": 300,
                "cache_read_input_tokens": 4000, "output_tokens": 50}}, {}))
        self.assertIn("reviewer: claude done (turns=3, input=2, cache write=300, cache read=4000, "
                      "output=50, cost=$0.2500)", out)


if __name__ == "__main__":
    unittest.main()

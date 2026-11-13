"""`--route NAME:PROFILE[,...]`: PR-title based profile routing over the default profile."""
import argparse
import dataclasses
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

import misscat as m
from test_cats import FakeDiscussion, FakeWorkspaces, WatcherBase

REPO = "genonfire/typewriter"
HEAD = "a" * 40

LUNA_ROUTES = """\
routes:
  docs:
    title_regex: '^\\[Docs\\]'
  s3:
    title_regex: '^\\[S3/[^]]+\\]'
  s2:
    title_regex: '^\\[S2/[^]]+\\]'
  any:
    title_regex: '^\\['
"""


def write_profile(name, provider="claude", model=None, prompt=None, extra=""):
    model = model or f"{name}-model"
    prompt = prompt or f"{name} prompt"
    (m.CONFIG_DIR / f"{name}.yml").write_text(
        f"reviewer:\n  provider: {provider}\n  model: {model}\n  args: ['--{name}']\n"
        f"prompt: {prompt}\n{extra}")


class ParseRouteArgTest(unittest.TestCase):
    def test_order_is_kept(self):
        self.assertEqual(m.parse_route_arg("s2:sonnet,s3:terra"), [("s2", "sonnet"), ("s3", "terra")])
        self.assertEqual(m.parse_route_arg("docs:opus"), [("docs", "opus")])

    def test_malformed_and_duplicate_entries_fail(self):
        for bad in ("s2", ":x", "s2:", "a:b:c", "s2:x,s2:y", "s2:x,", ",s2:x", "s2: x", "s2:x;s3:y"):
            with self.subTest(bad=bad), self.assertRaises(argparse.ArgumentTypeError):
                m.parse_route_arg(bad)


class ProfileBase(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        for name, value in (("CONFIG_DIR", root / "config"), ("LOCK_ROOT", root / "locks"),
                            ("WORKSPACE_ROOT", root / "repos"), ("CAT_ROOT", root / "cats")):
            patcher = mock.patch.object(m, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        m.CONFIG_DIR.mkdir(parents=True)
        self.root = root
        write_profile("luna", provider="codex", extra=LUNA_ROUTES)
        write_profile("sonnet", provider="claude")
        write_profile("terra", provider="gemini")
        write_profile("opus", provider="claude")

    def routes(self, arg, base="luna"):
        return m.load_routes(m.load_config(base), m.parse_route_arg(arg), "luna.yml")


class LoadRoutesTest(ProfileBase):
    def test_loads_target_profiles_in_cli_order(self):
        routes = self.routes("s2:sonnet,s3:terra")
        self.assertEqual([(r.name, r.profile) for r in routes], [("s2", "sonnet"), ("s3", "terra")])
        self.assertEqual(routes[0].settings, m.load_settings("sonnet"))
        self.assertEqual(routes[1].settings.provider, "gemini")

    def assert_config_error(self, arg, text, base="luna"):
        with self.assertRaises(m.ConfigError) as caught:
            self.routes(arg, base)
        self.assertIn(text, str(caught.exception))

    def test_invalid_route_configuration_fails_clearly(self):
        self.assert_config_error("s2:sonnet", "needs a `routes` mapping", base="sonnet")
        self.assert_config_error("s9:sonnet", "unknown route 's9'")
        self.assert_config_error("s2:nope", "route s2: profile 'nope' not found")
        self.assert_config_error("s2:../x", "route s2: invalid profile name")
        for body, text in (("{}", "title_regex"), ("{title_regex: ''}", "title_regex"),
                           ("{title_regex: 3}", "title_regex"),
                           ("{title_regex: '[S2'}", "not a valid regex")):
            with self.subTest(body=body):
                write_profile("luna", provider="codex", extra=f"routes:\n  s2: {body}\n")
                self.assert_config_error("s2:sonnet", text)

    def test_invalid_target_profile_config_fails(self):
        (m.CONFIG_DIR / "sonnet.yml").write_text("reviewer:\n  provider: nope\n")
        self.assert_config_error("s2:sonnet", "route s2: reviewer.provider")


class RoutingWatcherTest(WatcherBase):
    """Dispatch picks the full reviewer config per PR; everything else uses the default profile."""

    def setUp(self):
        super().setUp()
        m.CONFIG_DIR.mkdir(parents=True)
        write_profile("luna", provider="codex", extra=LUNA_ROUTES)
        write_profile("sonnet", provider="claude")
        write_profile("terra", provider="gemini")
        self.cfg = m.load_settings("luna")
        self.titles = {101: "[S2/Codex] 063_02 Part 01", 102: "[S3/Claude] 063",
                       103: "[C/Codex] 063_02", 104: "implement the thing"}
        self.prs = [dataclasses.replace(p, title=self.titles[p.number]) for p in self.prs]

    def routes(self, arg):
        return m.load_routes(m.load_config("luna"), m.parse_route_arg(arg), "luna.yml")

    def recording(self, seen, disc=None):
        def reviewer(s, r, pr, ws):
            seen[pr.number] = s
            if disc is not None:
                disc.submit(pr)
            return True
        return reviewer

    def profiles(self):
        return {r.pr: r.profile for r in self.state.records()}

    def test_without_route_every_pr_uses_the_default_profile(self):
        seen = {}
        self.watcher(self.recording(seen), 4).cycle()
        self.assertEqual(set(seen), {101, 102, 103, 104})
        self.assertTrue(all(s is self.cfg for s in seen.values()))
        self.assertEqual(set(self.profiles().values()), {"luna"})

    def test_matching_titles_use_the_route_profile_settings(self):
        seen, disc = {}, FakeDiscussion()
        w = self.watcher(self.recording(seen, disc), 2, discussion=disc,
                         routes=self.routes("s2:sonnet,s3:terra"))
        w.cycle()
        w.cycle()
        sonnet, terra = m.load_settings("sonnet"), m.load_settings("terra")
        self.assertEqual((seen[101].provider, seen[101].model, seen[101].prompt, seen[101].args),
                         ("claude", "sonnet-model", "sonnet prompt", ("--sonnet",)))
        self.assertEqual(seen[101], sonnet)
        self.assertEqual(seen[102], terra)
        self.assertIs(seen[103], self.cfg)
        self.assertIs(seen[104], self.cfg)
        self.assertEqual(self.profiles(), {101: "sonnet", 102: "terra", 103: "luna", 104: "luna"})
        self.assertEqual(m.build_prompt(seen[101], REPO, self.prs[0]).split("\n")[0], "sonnet prompt")

    def test_first_cli_listed_match_wins_not_yaml_order(self):
        seen = {}
        # `any` matches every bracketed title and is defined last in YAML, but listed first here
        w = self.watcher(self.recording(seen), 4, routes=self.routes("any:terra,s2:sonnet"))
        w.cycle()
        self.assertEqual(self.profiles(), {101: "terra", 102: "terra", 103: "terra", 104: "luna"})

    def test_concurrent_mixed_routes_do_not_leak_settings_or_profiles(self):
        disc = FakeDiscussion()
        routes = self.routes("s2:sonnet")
        sonnet = routes[0].settings
        barrier = threading.Barrier(2, timeout=5)
        seen, route_seen = {}, {}

        pending = {}

        def default_reviewer(s, r, pr, ws):
            barrier.wait()
            pending.update({p.pr: p.profile for p in self.state.pending()})
            seen[pr.number] = s
            disc.submit(pr)
            return True

        def route_reviewer(s, r, pr, ws):
            barrier.wait()
            route_seen[pr.number] = s
            disc.submit(pr)
            return True

        routes = (routes[0]._replace(reviewer=route_reviewer),)
        w = self.watcher(default_reviewer, 2, self.prs[:1] + self.prs[2:3], discussion=disc,
                         routes=routes)
        w.cycle()  # both cats start; the barrier only passes if they really run together
        self.assertEqual(pending, {101: "sonnet", 103: "luna"})
        self.assertEqual(route_seen, {101: sonnet})
        self.assertEqual(seen, {103: self.cfg})
        self.assertEqual(self.profiles(), {101: "sonnet", 103: "luna"})

    def test_interrupted_route_run_recovers_with_its_recorded_profile(self):
        disc = FakeDiscussion()
        pr = self.prs[0]
        self.state.add_pending(m.PendingRun(pr.number, pr.head, "sonnet", "nonce1", ()))
        disc.runs[pr.number] = (pr.head, "nonce1")  # its review was submitted before the crash
        reloaded = m.State(m.state_path(REPO))
        w = m.Watcher(REPO, "luna", self.cfg, reloaded, lambda r: [pr], lambda *a: True,
                      discussion=disc, clock=lambda: self.now)  # restarted without --route
        w._reconcile_pending()
        self.assertEqual({r.pr: r.profile for r in reloaded.records()}, {101: "sonnet"})
        self.assertEqual(reloaded.pending(), [])

    def test_author_reply_rereview_uses_route_and_records_its_profile(self):
        disc = FakeDiscussion()
        seen = {}
        pr = dataclasses.replace(self.prs[0], reply=m.ReplyTrigger((77,), ("u#77",), 5))
        w = self.watcher(self.recording(seen, disc), 1, [pr], discussion=disc,
                         routes=self.routes("s2:sonnet"))
        w._dispatch([pr])
        while w.running:
            self.collect(w)
        self.assertEqual(seen[101].model, "sonnet-model")
        self.assertEqual([(r.comment_id, r.profile) for r in self.state._replies], [(77, "sonnet")])

    def test_completed_head_is_not_rereviewed_after_title_or_route_change(self):
        self.state.add(m.Key(101, self.prs[0].head), "luna")
        renamed = dataclasses.replace(self.prs[0], title="[S2/Other] renamed")
        w = self.watcher(lambda *a: True, 1, [renamed], routes=self.routes("s2:sonnet"))
        self.assertEqual(w._next_batch([renamed]), [])
        newer = dataclasses.replace(renamed, head="c" * 40)
        self.assertEqual(w._next_batch([newer]), [newer])

    def test_route_profile_does_not_change_scheduling(self):
        write_profile("sonnet", provider="claude", extra="review:\n  include_drafts: false\n"
                      "watch:\n  idle: [7]\n  active: [7]\n")
        w = self.watcher(lambda *a: True, 1, routes=self.routes("s2:sonnet"))
        draft = dataclasses.replace(self.prs[0], draft=True)
        self.assertTrue(self.cfg.include_drafts)
        self.assertEqual(w._next_batch([draft]), [draft])  # luna's draft policy, not sonnet's
        self.assertEqual(w.poll(), self.cfg.idle[0] if not w.running else self.cfg.active[0])


class MainTest(ProfileBase):
    def setUp(self):
        super().setUp()
        self.which = mock.patch("shutil.which", return_value="/x")
        self.which_mock = self.which.start()
        self.addCleanup(self.which.stop)
        patches = [mock.patch.object(m.Workspaces, "sync"),
                   mock.patch.object(m.Workspaces, "prepare", return_value=self.root),
                   mock.patch.object(m.GhDiscussion, "reviews", return_value=[]),
                   mock.patch.object(m.GhDiscussion, "head", return_value=HEAD)]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)

    def run_main(self, argv, prs=()):
        with mock.patch.object(m, "gh_open_prs", return_value=list(prs)), \
                mock.patch("sys.stderr") as err:
            code = m.main(argv)
        return code, "".join(c.args[0] for c in err.write.call_args_list)

    def stop_after_first_poll(self):
        return mock.patch.object(m.Watcher, "run_forever", side_effect=KeyboardInterrupt)

    def test_help_documents_route(self):
        with mock.patch("sys.stdout") as out, self.assertRaises(SystemExit):
            m.main(["--help"])
        text = " ".join("".join(c.args[0] for c in out.write.call_args_list).split())
        self.assertIn("--route NAME:PROFILE", text)
        self.assertIn("first match", text)

    def test_startup_errors_are_clear_and_exit_2(self):
        for argv, text in (([REPO, "luna", "--route=s9:sonnet"], "unknown route 's9'"),
                           ([REPO, "luna", "--route=s2:nope"], "route s2: profile 'nope' not found"),
                           ([REPO, "--route=s2:sonnet"], "the bundled defaults")):
            with self.subTest(argv=argv), self.stop_after_first_poll():
                self.assertEqual(self.run_main(argv)[0], 2)
                self.assertIn(text, self.run_main(argv)[1])

    def test_malformed_route_argument_is_a_usage_error(self):
        with mock.patch("sys.stderr"), self.assertRaises(SystemExit) as caught:
            m.main([REPO, "luna", "--route=s2"])
        self.assertEqual(caught.exception.code, 2)

    def test_missing_route_executable_fails_only_with_route(self):
        self.which_mock.side_effect = lambda tool: None if tool == "agy" else "/x"
        with self.stop_after_first_poll():
            code, err = self.run_main([REPO, "luna", "--route=s2:sonnet,s3:terra"])
        self.assertEqual(code, 2)
        self.assertIn("required CLI not found on PATH: agy", err)
        with self.stop_after_first_poll():  # without --route terra's CLI is never required
            self.assertEqual(self.run_main([REPO, "luna"])[0], 130)

    def test_routes_in_yaml_are_ignored_without_route(self):
        write_profile("luna", provider="codex", extra="routes:\n  s2: {title_regex: '[bad'}\n")
        with self.stop_after_first_poll():
            self.assertEqual(self.run_main([REPO, "luna"])[0], 130)

    def fatal(self, title):
        """A route whose profile is byte-identical to the default must still name its own file."""
        same = "reviewer:\n  provider: opencode\n  model: p/m\n  args: []\nprompt: p\n"
        (m.CONFIG_DIR / "a.yml").write_text(same + "routes:\n  s2: {title_regex: '^\\[S2/'}\n")
        (m.CONFIG_DIR / "b.yml").write_text(same)

        def fail(self_, settings, repo, pr, ws):
            self_._on_fatal("provider.no-route", "nope")
            return False

        with mock.patch.object(m.CliReviewer, "__call__", fail):
            return self.run_main([REPO, "a", "--route=s2:b"], [m.PR(1, HEAD, False, "u", title)])

    def test_fatal_advice_names_the_route_profile_even_with_identical_settings(self):
        code, err = self.fatal("[S2/Codex] x")
        self.assertEqual(code, 2)
        self.assertIn(str(m.CONFIG_DIR / "b.yml"), err)
        self.assertNotIn("a.yml", err)

    def test_fatal_advice_names_the_default_profile_for_unrouted_prs(self):
        code, err = self.fatal("[C/Codex] x")
        self.assertEqual(code, 2)
        self.assertIn(str(m.CONFIG_DIR / "a.yml"), err)
        self.assertNotIn("b.yml", err)


if __name__ == "__main__":
    unittest.main()

"""badcat-host: +1 events, Native Messaging framing, process behavior, registration, packaging."""
import contextlib
import io
import json
import logging
import os
import stat
import struct
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import zipfile
from pathlib import Path
from unittest import mock

import misscat
from badcat import host
from badcat.github import OpenPR
from badcat.protocol import Review

A, B = "a" * 40, "b" * 40
EXT = "a" * 32
logging.getLogger("badcat").addHandler(logging.NullHandler())


def body(marker, sha):
    return f"{marker}\nHEAD: {sha}\n\nlooks fine"


def review(rid, marker, sha, login="me", state="COMMENTED", text=None, commit=None):
    return Review(rid, login, state, text if text is not None else body(marker, sha),
                  commit or sha, f"2026-01-01T00:00:{rid:02d}Z")


class FakeClient:
    """open_prs + reviews + viewer only: the host has no other GitHub surface to call."""

    def __init__(self, head=A, reviews=None, number=7):
        self.head, self.number, self.review_list = head, number, reviews or []
        self.fail, self.open, self.reads = {}, True, 0

    def _maybe_fail(self, name):
        if name in self.fail:
            raise misscat.GhError(self.fail[name])

    def viewer(self):
        self._maybe_fail("viewer")
        return "Me"

    def open_prs(self):
        self._maybe_fail("open_prs")
        return [OpenPR(self.number, self.head, False, "u", "t")] if self.open else []

    def reviews(self, number):
        self.reads += 1
        self._maybe_fail("reviews")
        return self.review_list


def frames(data):
    """Decode a byte string made only of Native Messaging frames; fail on any other byte."""
    out, pos = [], 0
    while pos < len(data):
        (length,) = struct.unpack("=I", data[pos:pos + 4])
        payload = data[pos + 4:pos + 4 + length]
        assert len(payload) == length, "truncated frame"
        out.append(json.loads(payload))
        pos += 4 + length
    return out


def event(pr=7, head=A):
    return {"repo": "o/r", "pr": pr, "head": head, "status": "+1"}


class WatcherTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.path = Path(tmp.name) / "host" / "o__r.json"
        self.events = []

    def watcher(self, client):
        return host.ReviewStateWatcher("o/r", client, host.EmittedState(self.path),
                                       frozenset({"me"}), self.events.append)

    def test_same_head_plus1_emits_exactly_once(self):
        client = FakeClient(reviews=[review(1, "+1", A)])
        w = self.watcher(client)
        for _ in range(3):
            w.cycle()
        self.assertEqual(self.events, [event()])
        reads = client.reads
        w.cycle()
        self.assertEqual(client.reads, reads)  # a reported HEAD is not re-evaluated

    def test_restart_does_not_repeat_the_event(self):
        client = FakeClient(reviews=[review(1, "+1", A)])
        self.watcher(client).cycle()
        self.watcher(client).cycle()  # a new process reading the saved state
        self.assertEqual(self.events, [event()])

    def test_event_is_not_recorded_when_the_client_is_gone(self):
        def gone(_):
            raise host.ConnectionClosed("pipe")
        w = host.ReviewStateWatcher("o/r", FakeClient(reviews=[review(1, "+1", A)]),
                                    host.EmittedState(self.path), frozenset({"me"}), gone)
        with self.assertRaises(host.ConnectionClosed):
            w.cycle()
        self.assertFalse(w.state.has(7, A))

    def test_states_that_are_not_plus1_never_emit(self):
        cases = {
            "no review": [],
            "malformed": [review(1, None, A, text=f"+1 : fine\nHEAD: {A}\n\nx")],
            "no evidence": [review(1, None, A, text=f"+1\nHEAD: {A}\n")],
            "-1": [review(1, "+1", A), review(2, "-1", A)],
            "-1 only": [review(1, "-1", A)],
            "untrusted": [review(1, "+1", A, login="eve")],
            "+2 alone": [review(1, "+2", A)],
            "+1 then +2 (already past +1)": [review(1, "+1", A), review(2, "+2", A)],
            "+1 for another head": [review(1, "+1", B)],
            "header sha differs from commit": [review(1, "+1", A, commit=B)],
            "not a COMMENTED review": [review(1, "+1", A, state="CHANGES_REQUESTED")],
            "non-pass review after +1": [review(1, "+1", A),
                                         review(2, None, A, text="plain comment")],
        }
        for name, reviews in cases.items():
            with self.subTest(name):
                self.events.clear()
                w = host.ReviewStateWatcher("o/r", FakeClient(reviews=reviews),
                                            host.EmittedState(self.path.with_name(name[:3] + ".json")),
                                            frozenset({"me"}), self.events.append)
                w.cycle()
                self.assertEqual(self.events, [])

    def test_new_head_emits_again_after_reaching_plus1(self):
        client = FakeClient(reviews=[review(1, "+1", A)])
        w = self.watcher(client)
        w.cycle()
        client.head = B  # new commit: old +1 does not carry over
        w.cycle()
        self.assertEqual(self.events, [event()])
        client.review_list = [review(1, "+1", A), review(2, "+1", B)]
        w.cycle()
        w.cycle()
        self.assertEqual(self.events, [event(), event(head=B)])

    def test_api_failure_never_emits_and_recovers(self):
        client = FakeClient(reviews=[review(1, "+1", A)])
        w = self.watcher(client)
        client.fail["open_prs"] = "HTTP 502"
        with self.assertLogs("badcat", level="WARNING"):
            delay = w.cycle()
        self.assertGreaterEqual(delay, 60)
        client.fail = {"reviews": "timed out"}
        with self.assertLogs("badcat", level="WARNING"):
            w.cycle()
        self.assertEqual(self.events, [])
        client.fail = {}
        w.cycle()
        self.assertEqual(self.events, [event()])
        self.assertEqual(w.errors, 0)

    def test_closed_then_reopened_same_head_does_not_emit_again(self):
        client = FakeClient(reviews=[review(1, "+1", A)])
        w = self.watcher(client)
        w.cycle()
        client.open = False
        w.cycle()
        client.open = True  # reopened with the same HEAD
        w.cycle()
        self.assertEqual(self.events, [event()])
        self.assertTrue(host.EmittedState(self.path).has(7, A))  # also across a restart
        client.head = B
        client.review_list = [review(1, "+1", A), review(2, "+1", B)]
        w.cycle()
        self.assertEqual(self.events, [event(), event(head=B)])

    def test_gh_errors_are_logged_without_gh_text(self):
        secret = "HTTP 401: Bad credentials token ghp_secret Authorization: Bearer abc"
        for stage in ("open_prs", "reviews"):
            with self.subTest(stage):
                client = FakeClient(reviews=[review(1, "+1", A)])
                client.fail[stage] = secret
                w = self.watcher(client)
                with self.assertLogs("badcat", level="WARNING") as cm:
                    w.cycle()
                text = "\n".join(cm.output)
                self.assertNotIn("ghp_secret", text)
                self.assertNotIn("Bearer", text)
                self.assertIn("HTTP 401", text)
        self.assertEqual(host.describe_gh_error(misscat.GhError("boom ghp_x")), "gh request failed")

    def test_unreadable_state_is_ignored(self):
        self.path.parent.mkdir(parents=True)
        for text in ("{nope", json.dumps({"version": 9, "emitted": {}}), "[]"):
            self.path.write_text(text)
            with self.assertLogs("badcat.host", level="WARNING"):
                self.assertEqual(host.EmittedState(self.path).prs, {})

    def test_client_has_no_write_surface(self):
        # the watcher only ever calls open_prs/reviews/viewer; FakeClient has nothing else
        client = FakeClient(reviews=[review(1, "+1", A), review(2, "+2", A)])
        self.watcher(client).cycle()
        self.assertFalse(hasattr(client, "merge"))


class FramingTests(unittest.TestCase):
    def test_encode_is_native_length_prefixed_json(self):
        data = host.encode_frame({"a": "한"})
        payload = '{"a":"한"}'.encode()
        self.assertEqual(data, struct.pack("=I", len(payload)) + payload)

    def test_read_frames_and_clean_eof(self):
        stream = io.BytesIO(host.encode_frame({"x": 1}) + host.encode_frame({"y": 2}))
        self.assertEqual(json.loads(host.read_frame(stream)), {"x": 1})
        self.assertEqual(json.loads(host.read_frame(stream)), {"y": 2})
        self.assertIsNone(host.read_frame(stream))

    def test_truncated_input_counts_as_closed(self):
        self.assertIsNone(host.read_frame(io.BytesIO(b"\x05\x00")))
        self.assertIsNone(host.read_frame(io.BytesIO(struct.pack("=I", 10) + b"abc")))

    def test_oversized_incoming_frame_is_rejected_without_reading_it(self):
        with self.assertRaises(host.FrameError):
            host.read_frame(io.BytesIO(struct.pack("=I", host.MAX_INCOMING + 1)))

    def test_sender_turns_a_closed_pipe_into_connection_closed(self):
        class Broken(io.BytesIO):
            def write(self, _):
                raise BrokenPipeError

        with self.assertRaises(host.ConnectionClosed):
            host.Sender(Broken())({"a": 1})


class Connection:
    """serve() running on a thread with a pipe for stdin and a capturing stdout."""

    def __init__(self, test, client):
        tmp = tempfile.TemporaryDirectory()
        test.addCleanup(tmp.cleanup)
        patches = [mock.patch.object(host, "state_path",
                                     lambda repo: Path(tmp.name) / f"{repo.replace('/', '__')}.json"),
                   mock.patch("badcat.poll.POLL_INTERVAL", 0.02)]
        for p in patches:
            p.start()
            test.addCleanup(p.stop)
        read_fd, self.write_fd = os.pipe()
        self.stdin = os.fdopen(read_fd, "rb")
        self.out = io.BytesIO()
        self.result = []
        self.thread = threading.Thread(
            target=lambda: self.result.append(host.serve(self.stdin, self.out, lambda repo: client)))
        self.thread.start()
        test.addCleanup(self.close)

    def send(self, message):
        os.write(self.write_fd, host.encode_frame(message))

    def send_raw(self, data):
        os.write(self.write_fd, data)

    def wait_frames(self, count, timeout=3.0):
        deadline = time.time() + timeout
        while time.time() < deadline:
            got = frames(self.out.getvalue())
            if len(got) >= count:
                return got
            time.sleep(0.01)
        return frames(self.out.getvalue())

    def close(self):
        try:
            os.close(self.write_fd)
        except OSError:
            pass
        self.thread.join(5)
        self.stdin.close()


class ServeTests(unittest.TestCase):
    def test_start_emits_one_event_and_closing_exits_cleanly(self):
        conn = Connection(self, FakeClient(reviews=[review(1, "+1", A)]))
        conn.send({"type": "start", "repo": "O/R"})
        self.assertEqual(conn.wait_frames(1), [event()])
        time.sleep(0.2)  # many more polls (interval 20 ms) must not repeat it
        os.close(conn.write_fd)
        conn.thread.join(5)
        self.assertFalse(conn.thread.is_alive())
        self.assertEqual(conn.result, [0])
        self.assertEqual(frames(conn.out.getvalue()), [event()])  # frames only

    def test_nothing_is_sent_until_start(self):
        conn = Connection(self, FakeClient(reviews=[review(1, "+1", A)]))
        time.sleep(0.1)
        self.assertEqual(conn.out.getvalue(), b"")

    def test_stop_ends_monitoring(self):
        client = FakeClient()
        conn = Connection(self, client)
        conn.send({"type": "start", "repo": "o/r"})
        time.sleep(0.1)
        conn.send({"type": "stop"})
        time.sleep(0.1)
        client.review_list = [review(1, "+1", A)]
        time.sleep(0.2)
        self.assertEqual(conn.out.getvalue(), b"")

    def test_bad_requests_get_errors_and_never_start_a_monitor(self):
        client = FakeClient(reviews=[review(1, "+1", A)])
        conn = Connection(self, client)
        for repo in ["bad repo", "o/r\n", "../x", "o/r/extra", "o", "", None, 5, "--help/x", "o/.."]:
            conn.send({"type": "start", "repo": repo})
        conn.send({"type": "start"})
        conn.send({"type": "run", "cmd": "ls"})
        conn.send(["start"])
        conn.send_raw(struct.pack("=I", 3) + b"{no")
        got = conn.wait_frames(14)
        self.assertEqual(len(got), 14)
        self.assertTrue(all(list(g) == ["error"] for g in got), got)
        self.assertEqual(client.reads, 0)

    def test_gh_failure_reports_error_without_event_and_can_retry(self):
        client = FakeClient(reviews=[review(1, "+1", A)])
        client.fail["viewer"] = "HTTP 401 token ghp_secret"
        conn = Connection(self, client)
        with self.assertLogs("badcat", level="WARNING") as cm:
            conn.send({"type": "start", "repo": "o/r"})
            got = conn.wait_frames(1)
        self.assertEqual(got, [{"error": "cannot determine the authenticated gh user"}])
        self.assertNotIn(b"ghp_secret", conn.out.getvalue())  # no gh detail reaches the client
        self.assertNotIn("ghp_secret", "\n".join(cm.output))  # nor the log
        del client.fail["viewer"]
        conn.send({"type": "start", "repo": "o/r"})
        self.assertEqual(conn.wait_frames(2)[1:], [event()])

    def test_switching_repo_replaces_the_monitor(self):
        conn = Connection(self, FakeClient(reviews=[review(1, "+1", A)]))
        conn.send({"type": "start", "repo": "o/r"})
        conn.wait_frames(1)
        conn.send({"type": "start", "repo": "o/other"})
        got = conn.wait_frames(2)
        self.assertEqual([g["repo"] for g in got], ["o/r", "o/other"])

    def test_oversized_message_ends_the_connection(self):
        conn = Connection(self, FakeClient())
        conn.send_raw(struct.pack("=I", host.MAX_INCOMING + 1))
        conn.thread.join(5)
        self.assertEqual(conn.result, [1])
        self.assertEqual(frames(conn.out.getvalue()), [{"error": "invalid message framing"}])

    def test_client_disappearing_while_an_event_is_sent_ends_quietly(self):
        class Gone(io.BytesIO):
            def write(self, _):
                raise BrokenPipeError

        read_fd, write_fd = os.pipe()
        stdin = os.fdopen(read_fd, "rb")
        self.addCleanup(stdin.close)
        client = FakeClient(reviews=[review(1, "+1", A)])
        with mock.patch.object(host, "state_path", lambda r: Path(tempfile.mkdtemp()) / "s.json"), \
             mock.patch("badcat.poll.POLL_INTERVAL", 0.02):
            result = []
            t = threading.Thread(target=lambda: result.append(
                host.serve(stdin, Gone(), lambda repo: client)))
            t.start()
            os.write(write_fd, host.encode_frame({"type": "start", "repo": "o/r"}))
            time.sleep(0.2)
            os.close(write_fd)
            t.join(5)
        self.assertFalse(t.is_alive())
        self.assertEqual(result, [0])


class ProcessTests(unittest.TestCase):
    def run_host(self, data, env=None):
        with tempfile.TemporaryDirectory() as home:
            proc = subprocess.run(
                [sys.executable, "-m", "badcat.host"], input=data, capture_output=True, timeout=30,
                env={**os.environ, "HOME": home, **(env or {})})
        return proc

    def test_stdout_is_protocol_only_and_eof_exits_zero(self):
        data = (host.encode_frame({"type": "start", "repo": "not a repo"})
                + host.encode_frame({"type": "bogus"}))
        proc = self.run_host(data)
        self.assertEqual(proc.returncode, 0)
        self.assertEqual(frames(proc.stdout), [{"error": "repo must look like owner/repo"},
                                               {"error": "unknown message"}])

    def test_empty_connection_exits_cleanly_with_empty_stdout(self):
        proc = self.run_host(b"")
        self.assertEqual((proc.returncode, proc.stdout), (0, b""))

    def test_chrome_origin_argument_is_accepted(self):
        with tempfile.TemporaryDirectory() as home:
            proc = subprocess.run([sys.executable, "-m", "badcat.host", f"chrome-extension://{EXT}/"],
                                  input=b"", capture_output=True, timeout=30,
                                  env={**os.environ, "HOME": home})
        self.assertEqual((proc.returncode, proc.stdout), (0, b""))

    def test_print_cannot_corrupt_the_stream(self):
        script = (
            "import sys\n"
            "from unittest import mock\n"
            "from badcat import host\n"
            "def fake(stdin, stdout, *a):\n"
            "    print('stray output')\n"
            "    stdout.write(host.encode_frame({'ok': 1})); stdout.flush(); return 0\n"
            "with mock.patch.object(host, 'serve', fake):\n"
            "    sys.exit(host.main([]))\n"
        )
        proc = subprocess.run([sys.executable, "-c", script], input=b"", capture_output=True, timeout=30)
        self.assertEqual(proc.returncode, 0)
        self.assertEqual(frames(proc.stdout), [{"ok": 1}])
        self.assertIn(b"stray output", proc.stderr)

    def test_version_and_help(self):
        proc = subprocess.run([sys.executable, "-m", "badcat.host", "--version"],
                              capture_output=True, text=True, timeout=30)
        self.assertEqual(proc.stdout.strip(), f"badcat-host {misscat.__version__}")


class InstallTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.exe = self.root / "bin" / "badcat-host"
        self.exe.parent.mkdir()
        self.exe.write_text("#!/bin/sh\n")
        self.exe.chmod(self.exe.stat().st_mode | stat.S_IXUSR)

    def test_manifest_content_in_given_directory_only(self):
        hosts = self.root / "NativeMessagingHosts"
        path = host.install_manifest(EXT, str(self.exe), hosts)
        self.assertEqual(path, hosts / "com.genonfire.badcat_host.json")
        self.assertEqual(sorted(p.name for p in self.root.iterdir()), ["NativeMessagingHosts", "bin"])
        manifest = json.loads(path.read_text())
        self.assertEqual(manifest, {
            "name": "com.genonfire.badcat_host",
            "description": "BadCat review-state host (read-only)",
            "path": str(self.exe),
            "type": "stdio",
            "allowed_origins": [f"chrome-extension://{EXT}/"],
        })
        host.install_manifest("p" * 32, str(self.exe), hosts)  # re-install overwrites
        self.assertEqual(json.loads(path.read_text())["allowed_origins"], ["chrome-extension://" + "p" * 32 + "/"])

    def test_extension_id_is_required_and_validated(self):
        for bad in ["", "short", "A" * 32, "q" * 32, "a" * 33, "*", "a" * 31 + "\n"]:
            with self.subTest(bad), self.assertRaises(misscat.MissCatError):
                host.install_manifest(bad, str(self.exe), self.root / "h")
        self.assertFalse((self.root / "h").exists())

    def test_executable_must_exist_and_be_absolute(self):
        for bad in ["badcat-host", str(self.root / "missing")]:
            with self.subTest(bad), self.assertRaises(misscat.MissCatError):
                host.install_manifest(EXT, bad, self.root / "h")

    def test_default_directory_is_the_per_user_chrome_one_on_macos_only(self):
        with mock.patch.object(host.sys, "platform", "darwin"), \
             mock.patch.object(host.Path, "home", return_value=self.root):
            self.assertEqual(host.default_hosts_dir(), self.root / "Library" / "Application Support"
                             / "Google" / "Chrome" / "NativeMessagingHosts")
        with mock.patch.object(host.sys, "platform", "linux"), \
                self.assertRaises(misscat.MissCatError):
            host.default_hosts_dir()

    def test_install_command_uses_fake_home_and_requires_extension_id(self):
        hosts = self.root / "hosts"
        with mock.patch.object(host, "default_hosts_dir", return_value=hosts), \
             mock.patch.object(host, "find_executable", return_value=str(self.exe)):
            self.assertEqual(host.main(["install", "--extension-id", EXT]), 0)
            self.assertTrue((hosts / "com.genonfire.badcat_host.json").is_file())
            with contextlib.redirect_stderr(io.StringIO()):
                self.assertEqual(host.main(["install", "--extension-id", "nope"]), 2)
                with self.assertRaises(SystemExit):
                    host.main(["install"])

    def test_find_executable_prefers_the_invoked_script(self):
        with mock.patch.object(host.sys, "argv", [str(self.exe)]):
            self.assertEqual(host.find_executable(), str(self.exe))
        with mock.patch.object(host.sys, "argv", ["python"]), \
             mock.patch.object(host.shutil, "which", return_value=None), \
             self.assertRaises(misscat.MissCatError):
            host.find_executable()


class PackagingTests(unittest.TestCase):
    def test_wheel_installs_badcat_host_next_to_existing_entry_points(self):
        root = Path(__file__).resolve().parents[1]
        with tempfile.TemporaryDirectory() as out:
            proc = subprocess.run([sys.executable, "-m", "pip", "wheel", "--no-deps",
                                   "--no-build-isolation", "-q", "-w", out, str(root)],
                                  capture_output=True, text=True)
            if proc.returncode != 0:
                self.skipTest(f"cannot build wheel offline: {proc.stderr[-200:]}")
            wheel = next(Path(out).glob("misscat-*.whl"))
            with zipfile.ZipFile(wheel) as z:
                names = z.namelist()
                eps = z.read(next(n for n in names if n.endswith("entry_points.txt"))).decode()
        for module in ("host", "poll", "cli", "protocol"):
            self.assertIn(f"badcat/{module}.py", names)
        for line in ("badcat-host = badcat.host:main", "badcat = badcat.cli:main",
                     "misscat = misscat:main"):
            self.assertIn(line, eps)
        self.assertFalse([n for n in names if "extension" in n.lower()])  # no Chrome extension

    def test_existing_clis_still_run(self):
        for module in ("misscat", "badcat.cli"):
            proc = subprocess.run([sys.executable, "-m", module, "--version"],
                                  capture_output=True, text=True, timeout=30)
            self.assertEqual(proc.returncode, 0, proc.stderr)
            self.assertIn(misscat.__version__, proc.stdout)


if __name__ == "__main__":
    unittest.main()

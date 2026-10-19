"""nastycat-host: request validation, merge-state answers, framing, registration."""
import io
import json
import logging
import stat
import struct
import tempfile
import unittest
from pathlib import Path

from misscat import GhError, MissCatError
from badcat_host import host as badcat_host
from nastycat_host import host

logging.getLogger("nastycat_host").addHandler(logging.NullHandler())


class FakeGh:
    def __init__(self, data):
        self.data, self.calls = data, []

    def pr(self, number):
        self.calls.append(number)
        value = self.data[number]
        if isinstance(value, Exception):
            raise value
        return value


def frame(message):
    payload = json.dumps(message).encode()
    return struct.pack("=I", len(payload)) + payload


def run(messages, data):
    out = io.BytesIO()
    stdin = io.BytesIO(b"".join(frame(m) for m in messages))
    host.serve(stdin, out, lambda repo: FakeGh(data))
    raw, replies = out.getvalue(), []
    while raw:
        (n,) = struct.unpack("=I", raw[:4])
        replies.append(json.loads(raw[4:4 + n]))
        raw = raw[4 + n:]
    return replies


class CheckTests(unittest.TestCase):
    def test_merged_open_and_closed_unmerged(self):
        data = {
            1: {"state": "closed", "merged": True, "merged_at": "2026-01-01T00:00:00Z"},
            2: {"state": "open", "merged": False, "merged_at": None},
            3: {"state": "closed", "merged": False, "merged_at": None},
        }
        (reply,) = run([{"type": "check", "id": "a1", "prs": [{"repo": "o/r", "pr": n} for n in (1, 2, 3)]}], data)
        self.assertEqual(reply["id"], "a1")
        by = {p["pr"]: p for p in reply["prs"]}
        self.assertTrue(by[1]["merged"])
        self.assertEqual(by[1]["merged_at"], "2026-01-01T00:00:00Z")
        self.assertFalse(by[2]["merged"])
        self.assertEqual((by[3]["state"], by[3]["merged"], by[3]["merged_at"]), ("closed", False, None))

    def test_gh_failure_is_reported_per_pr_without_gh_text(self):
        data = {1: GhError("HTTP 404: token ghp_secret"), 2: {"state": "open", "merged": False}}
        (reply,) = run([{"type": "check", "id": "x", "prs": [{"repo": "o/r", "pr": 1}, {"repo": "o/r", "pr": 2}]}], data)
        self.assertEqual(reply["failed"], [{"repo": "o/r", "pr": 1}])
        self.assertEqual([p["pr"] for p in reply["prs"]], [2])
        self.assertNotIn("ghp_secret", json.dumps(reply))

    def test_merged_without_merged_at_fails_closed(self):
        (reply,) = run([{"type": "check", "id": "x", "prs": [{"repo": "o/r", "pr": 1}]}],
                       {1: {"state": "closed", "merged": True, "merged_at": None}})
        self.assertEqual(reply["prs"], [])
        self.assertEqual(len(reply["failed"]), 1)

    def test_rejects_bad_requests(self):
        bad = [
            {"type": "check", "id": "bad id", "prs": [{"repo": "o/r", "pr": 1}]},
            {"type": "check", "id": "a", "prs": []},
            {"type": "check", "id": "a", "prs": [{"repo": "o/r", "pr": 0}]},
            {"type": "check", "id": "a", "prs": [{"repo": "o/r", "pr": True}]},
            {"type": "check", "id": "a", "prs": [{"repo": "../x", "pr": 1}]},
            {"type": "check", "id": "a", "prs": [{"repo": "o/r", "pr": 1, "extra": 1}]},
            {"type": "check", "id": "a", "prs": [{"repo": "o/r", "pr": 1}] * (host.MAX_PRS + 1)},
            {"type": "start", "repo": "o/r"},
        ]
        for message in bad:
            (reply,) = run([message], {1: {}})
            self.assertIn("error", reply, message)
            self.assertNotIn("type", reply)

    def test_never_writes_to_github(self):
        client = FakeGh({1: {"state": "open", "merged": False}})
        host.check([("o/r", 1)], lambda repo: client)
        self.assertEqual(client.calls, [1])  # the fake exposes `pr` only: any other call would raise
        self.assertFalse(hasattr(client, "merge"))

    def test_bad_framing_ends_the_connection(self):
        out = io.BytesIO()
        self.assertEqual(host.serve(io.BytesIO(struct.pack("=I", 10 ** 9)), out), 1)


class RegistrationTests(unittest.TestCase):
    def test_manifest_uses_its_own_name_and_extension_origin(self):
        with tempfile.TemporaryDirectory() as tmp:
            exe = Path(tmp) / "nastycat-host"
            exe.write_text("#!/bin/sh\n")
            exe.chmod(exe.stat().st_mode | stat.S_IXUSR)
            path = host.install_manifest("a" * 32, str(exe), Path(tmp) / "hosts")
            manifest = json.loads(path.read_text())
        self.assertEqual(path.name, "com.genonfire.nastycat_host.json")
        self.assertEqual(manifest["name"], "com.genonfire.nastycat_host")
        self.assertEqual(manifest["allowed_origins"], ["chrome-extension://" + "a" * 32 + "/"])
        self.assertNotEqual(host.HOST_NAME, badcat_host.HOST_NAME)

    def test_rejects_bad_extension_id(self):
        with self.assertRaises(MissCatError):
            host.install_manifest("bad", "/bin/sh", Path("/nonexistent"))

    def test_does_not_import_merge_or_cli_code(self):
        source = Path(host.__file__).read_text()
        self.assertNotIn("badcat.cli", source)
        self.assertNotIn(".merge(", source)


if __name__ == "__main__":
    unittest.main()

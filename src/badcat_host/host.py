"""`badcat-host`: a read-only Chrome Native Messaging host that reports review-state events.

Reuses BadCat's `gh` client and strict review protocol; its poll/backoff loop is its own
(`badcat_host.poll`). It never writes to GitHub: it only
tells its client when a PR HEAD reaches a valid `+1`. stdout carries protocol frames and nothing
else; diagnostics go to stderr.

Client -> host (one JSON object per frame):
    {"type": "start", "repo": "owner/repo"}   monitor one repository (replaces the current one)
    {"type": "stop"}                            stop monitoring
    {"type": "ack", "repo": ..., "pr": ..., "head": ..., "review_id": ..., "delivery_id": ...}
Host -> client:
    {"repo": "owner/repo", "pr": 123, "head": "<40 hex>", "status": "+1", "review_id": 456, "delivery_id": "<nonce>"}
    {"error": "<message>"}                      a rejected request or a failed start
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import re
import shutil
import secrets
import time
import struct
import sys
import threading
from pathlib import Path
from typing import BinaryIO, Callable, Optional

from misscat import GhError, MissCatError, NAME_RE, OWNER_RE, _write_atomic, canonical_repo

from badcat.github import GhClient, OpenPR
from badcat.protocol import PLUS1, evaluate, initial_plus1_review_id

from . import __version__
from .poll import Poller

log = logging.getLogger("badcat_host")

STATE_DIR = Path.home() / ".config" / "badcat-host"  # its own state, separate from BadCat's

HOST_NAME = "com.genonfire.badcat_host"
MAX_INCOMING = 64 * 1024  # requests are tiny; anything larger is a protocol violation
MAX_OUTGOING = 1024 * 1024  # Chrome's limit for host -> browser messages
STATE_VERSION = 2
EXTENSION_ID_RE = re.compile(r"[a-p]{32}")  # Chrome extension IDs
STOP_TIMEOUT = 5.0
# A GUI-launched Chrome passes a minimal PATH; gh is usually installed in one of these.
FALLBACK_PATH = ("/opt/homebrew/bin", "/usr/local/bin", "/usr/bin", "/bin")


class FrameError(Exception):
    """The stream violates Native Messaging framing; the connection cannot be resynchronized."""


class ConnectionClosed(Exception):
    """The client's end of the connection is gone."""


class Stopped(Exception):
    """Raised inside a poll cycle to abandon it once monitoring was stopped."""


# --------------------------------------------------------------------------- framing


def _read_exact(stream: BinaryIO, size: int) -> Optional[bytes]:
    """`size` bytes, or None if the stream ends first."""
    chunks, remaining = [], size
    while remaining:
        chunk = stream.read(remaining)
        if not chunk:
            return None
        chunks.append(chunk)
        remaining -= len(chunk)
    return b"".join(chunks)


def read_frame(stream: BinaryIO) -> Optional[bytes]:
    """The next message payload; None once the connection is closed."""
    header = _read_exact(stream, 4)
    if header is None:
        return None
    (length,) = struct.unpack("=I", header)  # native byte order, as Chrome writes it
    if length > MAX_INCOMING:
        raise FrameError(f"message too large ({length} bytes)")
    return _read_exact(stream, length)


def encode_frame(message: dict) -> bytes:
    payload = json.dumps(message, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    if len(payload) > MAX_OUTGOING:
        raise ValueError("message too large")
    return struct.pack("=I", len(payload)) + payload


class Sender:
    """Serializes frames from the reader and polling threads onto one stream."""

    def __init__(self, stream: BinaryIO):
        self.stream, self.lock = stream, threading.Lock()

    def __call__(self, message: dict) -> None:
        frame = encode_frame(message)
        with self.lock:
            try:
                self.stream.write(frame)
                self.stream.flush()
            except (OSError, ValueError) as exc:  # broken pipe, closed file
                raise ConnectionClosed(str(exc)) from exc


# --------------------------------------------------------------------------- monitoring


class EmittedState:
    """Acknowledged review IDs plus one-time baselines for legacy emitted HEADs."""

    def __init__(self, path: Path):
        self.path, self.prs, self.legacy, self.baselines = path, {}, {}, {}
        self.lock = threading.RLock()
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
            version = raw.get("version")
            if version == 1:
                self.legacy = raw["emitted"]
                if not isinstance(self.legacy, dict) or any(
                        not isinstance(v, list) or any(not isinstance(h, str) for h in v)
                        for v in self.legacy.values()):
                    raise ValueError("incompatible legacy state")
            elif version == STATE_VERSION:
                self.prs, self.legacy = raw["acknowledged"], raw.get("legacy", {})
                self.baselines = raw.get("baselines", {})
                if not isinstance(self.prs, dict) or not isinstance(self.legacy, dict):
                    raise ValueError("incompatible state")
                for values in self.prs.values():
                    if not isinstance(values, dict) or any(
                            not isinstance(ids, list) or any(type(rid) is not int or rid < 1 for rid in ids)
                            for ids in values.values()):
                        raise ValueError("incompatible acknowledgements")
            else:
                raise ValueError("incompatible state")
            if not isinstance(self.baselines, dict) or any(
                    not isinstance(values, dict) or any(type(rid) is not int or rid < 0
                                                       for rid in values.values())
                    for values in self.baselines.values()):
                raise ValueError("incompatible baselines")
            if not isinstance(self.legacy, dict) or any(
                    not isinstance(values, list) or any(not isinstance(h, str) for h in values)
                    for values in self.legacy.values()):
                raise ValueError("incompatible legacy state")
            if version == STATE_VERSION and "migration_before" in raw:
                # Earlier provisional v2 files inferred baselines from a repo-wide mtime.
                # Recompute only those guesses; preserve every actual acknowledgement.
                for number, values in self.baselines.items():
                    self.legacy[number] = list(set(self.legacy.get(number, [])) | set(values))
                self.baselines = {}
        except FileNotFoundError:
            pass
        except (OSError, ValueError, KeyError, AttributeError, TypeError) as exc:
            # Preserve the file and fail closed: corrupt history must not replay every PR.
            raise MissCatError("cannot read host delivery state") from exc

    def has(self, number: int, head: str, review_id: int) -> bool:
        with self.lock:
            return (review_id in self.prs.get(str(number), {}).get(head, []) or
                    review_id == self.baselines.get(str(number), {}).get(head, 0))

    def baseline(self, number: int, head: str, reviews: list, trusted: frozenset) -> None:
        with self.lock:
            if head not in self.legacy.get(str(number), []):
                return
            rid = initial_plus1_review_id(reviews, head, trusted) or 0
            self.baselines.setdefault(str(number), {}).setdefault(head, rid)
            self._save()  # keep legacy until the baseline has been saved durably
            self.legacy[str(number)].remove(head)
            self._save()

    def add(self, number: int, head: str, review_id: int) -> None:
        with self.lock:
            previous = self.prs.get(str(number), {}).get(head, [])
            updated = {k: dict(v) for k, v in self.prs.items()}
            updated.setdefault(str(number), {})[head] = sorted(set(previous) | {review_id})
            self._save(updated)
            self.prs = updated  # a failed write must not acknowledge only in memory

    def _save(self, prs=None) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        _write_atomic(self.path, json.dumps({"version": STATE_VERSION,
                      "acknowledged": self.prs if prs is None else prs, "legacy": self.legacy,
                      "baselines": self.baselines}))


HTTP_STATUS_RE = re.compile(r"\bHTTP (\d{3})\b")


def describe_gh_error(exc: Exception) -> str:
    """A log-safe description of a gh failure: gh's own text may echo credentials, so only the
    HTTP status (if any) is kept."""
    match = HTTP_STATUS_RE.search(str(exc))
    return f"gh request failed (HTTP {match.group(1)})" if match else "gh request failed"


class ReviewStateWatcher(Poller):
    """Re-fetch effective +1s; retry unacknowledged frames with capped backoff."""

    def __init__(self, repo: str, client, state: EmittedState, trusted: frozenset,
                 emit: Callable[[dict], None], now=time.monotonic):
        super().__init__(client)
        self.repo, self.state, self.trusted, self.emit = repo, state, trusted, emit
        self.now, self.outstanding = now, {}
        self.acknowledged = set()  # idempotent ACKs for this connection, never previous sessions
        self.lock = threading.RLock()

    def _tracked(self):
        with self.lock:
            return list(self.outstanding)

    def _detail(self, exc: Exception) -> str:
        return describe_gh_error(exc)

    def acknowledge(self, message: dict) -> bool:
        if not valid_ack(message, self.repo):
            return False
        with self.lock:
            identity = tuple(message[k] for k in ("repo", "pr", "head", "review_id", "delivery_id"))
            if identity in self.acknowledged:
                return True
            pending = self.outstanding.get(message["pr"])
            if pending is None:
                return False
            event = pending[0]
            if any(message[k] != event[k] for k in ("repo", "pr", "head", "review_id", "delivery_id")):
                return False
            self.state.add(event["pr"], event["head"], event["review_id"])
            self.acknowledged.add(identity)
            self.outstanding.pop(event["pr"], None)
            return True

    def _process(self, pr: OpenPR) -> None:
        if (type(pr.number) is not int or not 0 < pr.number <= 2 ** 53 - 1 or
                not isinstance(pr.head, str) or not re.fullmatch(r"[0-9a-f]{40}", pr.head)):
            self._finished(pr.number)
            return
        reviews = self.client.reviews(pr.number)
        verdict = evaluate(reviews, pr.head, self.trusted)
        with self.lock:
            self.state.baseline(pr.number, pr.head, reviews, self.trusted)
            rid = verdict.plus1_review_id
            if pr.draft or verdict.stage != PLUS1 or type(rid) is not int or not 0 < rid <= 2 ** 53 - 1:
                self.outstanding.pop(pr.number, None)
                return
            if "batch" in pr.labels and "urgent" not in pr.labels:
                # BadCat merges these at +1: nothing for the extension to hand off (urgent overrides batch).
                self.outstanding.pop(pr.number, None)
                return
            if self.state.has(pr.number, pr.head, rid):
                self.outstanding.pop(pr.number, None)
                return
            pending = self.outstanding.get(pr.number)
            if pending is None or (pending[0]["head"], pending[0]["review_id"]) != (pr.head, rid):
                pending = ({"repo": self.repo, "pr": pr.number, "head": pr.head, "status": "+1",
                            "review_id": rid, "delivery_id": secrets.token_hex(16)}, 0, 0)
                baseline = self.state.baselines.get(str(pr.number), {}).get(pr.head)
                if baseline is not None:
                    pending[0]["legacy_review_id"] = baseline
            event, deadline, attempts = pending
            self.outstanding[pr.number] = pending
            if self.now() < deadline:
                return
            # Store before writing: a synchronous ack cannot miss its outstanding frame.
            self.outstanding[pr.number] = (event, self.now() + (60, 120, 300)[min(attempts, 2)], attempts + 1)
            self.emit(event)
            log.info("PR #%d: +1 review %d sent, awaiting acknowledgement", pr.number, rid)

    def _finished(self, number: int) -> None:
        with self.lock:
            self.outstanding.pop(number, None)


def valid_ack(message, repo: str) -> bool:
    return (isinstance(message, dict) and set(message) ==
            {"type", "repo", "pr", "head", "review_id", "delivery_id"} and
            message.get("type") == "ack" and message.get("repo") == repo and
            type(message.get("pr")) is int and message["pr"] > 0 and
            type(message.get("review_id")) is int and message["review_id"] > 0 and
            isinstance(message.get("head"), str) and
            re.fullmatch(r"[0-9a-f]{40}", message["head"]) is not None and
            isinstance(message.get("delivery_id"), str) and
            re.fullmatch(r"[0-9a-f]{32}", message["delivery_id"]) is not None)


class Monitor:
    """Polls one repository on a background thread until stopped."""

    def __init__(self, repo: str, client, state: EmittedState, send: Sender):
        self.repo, self.client, self.state, self.send = repo, client, state, send
        self._stop = threading.Event()
        self.watcher = None
        self._thread = threading.Thread(target=self._run, name=f"monitor-{repo}", daemon=True)

    def start(self) -> None:
        self._thread.start()

    @property
    def alive(self) -> bool:
        return self._thread.is_alive()

    def stop(self) -> None:
        self._stop.set()
        self._thread.join(STOP_TIMEOUT)  # a poll blocked in `gh` is a daemon thread and is abandoned

    def acknowledge(self, message: dict) -> bool:
        return bool(not self._stop.is_set() and self.watcher is not None and
                    self.watcher.acknowledge(message))

    def _emit(self, event: dict) -> None:
        if self._stop.is_set():
            raise Stopped
        self.send(event)

    def _run(self) -> None:
        try:
            try:
                trusted = frozenset({self.client.viewer().lower()})
            except (GhError, KeyError) as exc:
                log.warning("cannot determine the authenticated gh user: %s", describe_gh_error(exc))
                self._report("cannot determine the authenticated gh user")
                return
            log.info("Watching %s (trusted reviewer: %s)", self.repo, ", ".join(sorted(trusted)))
            watcher = ReviewStateWatcher(self.repo, self.client, self.state, trusted, self._emit)
            self.watcher = watcher
            while not self._stop.is_set():
                if self._stop.wait(watcher.cycle()):
                    break
        except Stopped:
            pass
        except ConnectionClosed as exc:
            log.info("connection closed: %s", exc)
            self._stop.set()
        except Exception as exc:  # keep the host alive and the stream clean; no message/traceback:
            log.error("monitor failed (%s)", type(exc).__name__)  # it could echo gh output
            self._report("monitoring stopped unexpectedly")

    def _report(self, message: str) -> None:
        try:
            if not self._stop.is_set():
                self.send({"error": message})
        except ConnectionClosed:
            pass


def validate_repo(value) -> str:
    """Canonical owner/repo from an untrusted request value, or MissCatError."""
    if not isinstance(value, str) or value.count("/") != 1 or len(value) > 200:
        raise MissCatError("repo must look like owner/repo")
    owner, name = value.split("/")
    if (owner.startswith("-") or name in (".", "..")  # GitHub owners never start with "-"
            or not (OWNER_RE.fullmatch(owner) and NAME_RE.fullmatch(name))):  # no trailing \n
        raise MissCatError("repo must look like owner/repo")
    return canonical_repo(value)


def state_path(repo: str) -> Path:
    return STATE_DIR / f"{repo.replace('/', '__')}.json"


def serve(stdin: BinaryIO, stdout: BinaryIO,
          client_factory: Callable[[str], object] = GhClient) -> int:
    """Handle one Native Messaging connection until the client closes it."""
    send = Sender(stdout)
    monitor: Optional[Monitor] = None
    try:
        while True:
            try:
                raw = read_frame(stdin)
            except FrameError as exc:
                log.error("%s", exc)
                send({"error": "invalid message framing"})
                return 1
            if raw is None:
                return 0  # connection closed
            try:
                message = json.loads(raw.decode("utf-8"))
            except ValueError:
                send({"error": "invalid JSON"})
                continue
            kind = message.get("type") if isinstance(message, dict) else None
            if kind == "stop":
                if monitor is not None:
                    monitor.stop()
                    monitor = None
            elif kind == "start":
                try:
                    repo = validate_repo(message.get("repo"))
                except MissCatError as exc:
                    send({"error": str(exc)})
                    continue
                if monitor is not None and monitor.repo == repo and monitor.alive:
                    continue
                if monitor is not None:
                    monitor.stop()
                try:
                    monitor = Monitor(repo, client_factory(repo), EmittedState(state_path(repo)), send)
                    monitor.start()
                except MissCatError:
                    monitor = None
                    send({"error": "cannot read host delivery state"})
            elif kind == "ack":
                try:
                    if monitor is None or not monitor.acknowledge(message):
                        send({"warning": "invalid or stale acknowledgement"})
                except OSError:
                    send({"warning": "cannot save acknowledgement; delivery remains retryable"})
            else:
                send({"error": "unknown message"})
    except ConnectionClosed:
        return 0
    finally:
        if monitor is not None:
            monitor.stop()


# --------------------------------------------------------------------------- registration


def default_hosts_dir() -> Path:
    if sys.platform != "darwin":
        raise MissCatError("automatic registration is supported on macOS only")
    return Path.home() / "Library" / "Application Support" / "Google" / "Chrome" / "NativeMessagingHosts"


def build_manifest(executable: str, extension_id: str) -> dict:
    return {
        "name": HOST_NAME,
        "description": "BadCat review-state host (read-only)",
        "path": executable,
        "type": "stdio",
        "allowed_origins": [f"chrome-extension://{extension_id}/"],
    }


def install_manifest(extension_id: str, executable: str, hosts_dir: Path) -> Path:
    """Write the per-user Chrome manifest; touches nothing but `hosts_dir`."""
    if not EXTENSION_ID_RE.fullmatch(extension_id):
        raise MissCatError("extension ID must be 32 characters a-p (see chrome://extensions)")
    if not os.path.isabs(executable) or not os.access(executable, os.X_OK):
        raise MissCatError(f"badcat-host executable not found: {executable}")
    hosts_dir.mkdir(parents=True, exist_ok=True)
    path = hosts_dir / f"{HOST_NAME}.json"
    _write_atomic(path, json.dumps(build_manifest(executable, extension_id), indent=2) + "\n")
    return path


def find_executable() -> str:
    argv0 = Path(sys.argv[0])
    if argv0.name == "badcat-host" and argv0.exists():
        return str(argv0.absolute())
    found = shutil.which("badcat-host")
    if found is None:
        raise MissCatError("cannot locate the badcat-host executable (is it on PATH?)")
    return str(Path(found).absolute())


def run_install(argv: list) -> int:
    parser = argparse.ArgumentParser(
        prog="badcat-host install",
        description="Register badcat-host as a Chrome Native Messaging host for this user (macOS).",
    )
    parser.add_argument("--extension-id", required=True, metavar="ID",
                        help="ID of the BadCat Chrome extension allowed to connect")
    args = parser.parse_args(argv)
    try:
        path = install_manifest(args.extension_id, find_executable(), default_hosts_dir())
    except (MissCatError, OSError) as exc:
        print(f"badcat-host: {exc}", file=sys.stderr)
        return 2
    print(f"Registered {HOST_NAME} for chrome-extension://{args.extension_id}/\n{path}")
    return 0


# --------------------------------------------------------------------------- entry point


def main(argv: Optional[list] = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    if argv[:1] == ["install"]:
        return run_install(argv[1:])
    parser = argparse.ArgumentParser(
        prog="badcat-host",
        description="Read-only Chrome Native Messaging host reporting BadCat +1 review states.",
        epilog="Run `badcat-host install --extension-id ID` once to register it with Chrome.\n"
               "Chrome starts the host itself; it speaks length-prefixed JSON on stdin/stdout.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("-v", "--version", action="version", version=f"%(prog)s {__version__}")
    if any(arg in ("-h", "--help", "-v", "--version") for arg in argv):
        parser.parse_args(argv)  # prints help/version and exits
    if sys.stdin.isatty():  # run by hand, not by Chrome
        parser.print_help()
        return 2
    # Chrome passes the caller's origin as an argument; the registered manifest already limits
    # who can connect, so arguments are ignored.
    protocol_in, protocol_out = sys.stdin.buffer, sys.stdout.buffer
    sys.stdout = sys.stderr  # a stray print can never corrupt the protocol stream
    logging.basicConfig(level=logging.INFO, stream=sys.stderr,
                        format="%(asctime)s %(levelname)s %(message)s", datefmt="%H:%M:%S")
    if shutil.which("gh") is None:
        os.environ["PATH"] = os.pathsep.join(filter(None, [os.environ.get("PATH"), *FALLBACK_PATH]))
    try:
        return serve(protocol_in, protocol_out)
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())

"""`badcat-host`: a read-only Chrome Native Messaging host that reports review-state events.

Reuses BadCat's strict review protocol and poll/backoff loop. It never writes to GitHub: it only
tells its client when a PR HEAD reaches a valid `+1`. stdout carries protocol frames and nothing
else; diagnostics go to stderr.

Client -> host (one JSON object per frame):
    {"type": "start", "repo": "owner/repo"}   monitor one repository (replaces the current one)
    {"type": "stop"}                            stop monitoring
Host -> client:
    {"repo": "owner/repo", "pr": 123, "head": "<40 hex>", "status": "+1"}   once per PR + HEAD
    {"error": "<message>"}                      a rejected request or a failed start
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import re
import shutil
import struct
import sys
import threading
from pathlib import Path
from typing import BinaryIO, Callable, Optional

from misscat import GhError, MissCatError, NAME_RE, OWNER_RE, _write_atomic, canonical_repo

from . import __version__
from . import cli as _cli
from .github import GhClient, OpenPR
from .poll import Poller
from .protocol import PLUS1, evaluate

log = logging.getLogger("badcat.host")

HOST_NAME = "com.genonfire.badcat_host"
MAX_INCOMING = 64 * 1024  # requests are tiny; anything larger is a protocol violation
MAX_OUTGOING = 1024 * 1024  # Chrome's limit for host -> browser messages
STATE_VERSION = 1
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
    """PR -> HEADs already reported as +1. Only dedupes events; never authorizes anything."""

    def __init__(self, path: Path):
        self.path, self.prs = path, {}
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
            if raw.get("version") != STATE_VERSION or not isinstance(raw.get("emitted"), dict):
                raise ValueError("incompatible state")
            self.prs = {str(k): [str(h) for h in v] for k, v in raw["emitted"].items()}
        except FileNotFoundError:
            pass
        except (OSError, ValueError, AttributeError, TypeError) as exc:
            log.warning("ignoring unreadable host state %s (%s)", path, exc)

    def has(self, number: int, head: str) -> bool:
        return head in self.prs.get(str(number), ())

    def add(self, number: int, head: str) -> None:
        self.prs.setdefault(str(number), []).append(head)
        self._save()

    def drop(self, number: int) -> None:
        if self.prs.pop(str(number), None) is not None:
            self._save()

    def _save(self) -> None:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            _write_atomic(self.path, json.dumps({"version": STATE_VERSION, "emitted": self.prs}))
        except OSError as exc:
            log.warning("cannot save host state: %s", exc)


class ReviewStateWatcher(Poller):
    """Emits one event per PR + HEAD that is at a valid +1. Read-only: no write is reachable."""

    def __init__(self, repo: str, client, state: EmittedState, trusted: frozenset,
                 emit: Callable[[dict], None]):
        super().__init__(client)
        self.repo, self.state, self.trusted, self.emit = repo, state, trusted, emit

    def _tracked(self):
        return [int(key) for key in self.state.prs]

    def _process(self, pr: OpenPR) -> None:
        if self.state.has(pr.number, pr.head):
            return
        verdict = evaluate(self.client.reviews(pr.number), pr.head, self.trusted)
        if verdict.stage != PLUS1:  # READY (+1 then +2) is past the +1 state; anything else holds
            return
        self.emit({"repo": self.repo, "pr": pr.number, "head": pr.head, "status": "+1"})
        self.state.add(pr.number, pr.head)  # only after the client was sent the event
        log.info("PR #%d: +1 (%s)", pr.number, pr.head[:7])

    def _finished(self, number: int) -> None:
        self.state.drop(number)


class Monitor:
    """Polls one repository on a background thread until stopped."""

    def __init__(self, repo: str, client, state: EmittedState, send: Sender):
        self.repo, self.client, self.state, self.send = repo, client, state, send
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, name=f"monitor-{repo}", daemon=True)

    def start(self) -> None:
        self._thread.start()

    @property
    def alive(self) -> bool:
        return self._thread.is_alive()

    def stop(self) -> None:
        self._stop.set()
        self._thread.join(STOP_TIMEOUT)  # a poll blocked in `gh` is a daemon thread and is abandoned

    def _emit(self, event: dict) -> None:
        if self._stop.is_set():
            raise Stopped
        self.send(event)

    def _run(self) -> None:
        try:
            try:
                trusted = frozenset({self.client.viewer().lower()})
            except (GhError, KeyError) as exc:
                log.warning("cannot determine the authenticated gh user: %s", exc)
                self._report("cannot determine the authenticated gh user")
                return
            log.info("Watching %s (trusted reviewer: %s)", self.repo, ", ".join(sorted(trusted)))
            watcher = ReviewStateWatcher(self.repo, self.client, self.state, trusted, self._emit)
            while not self._stop.is_set():
                if self._stop.wait(watcher.cycle()):
                    break
        except Stopped:
            pass
        except ConnectionClosed as exc:
            log.info("connection closed: %s", exc)
            self._stop.set()
        except Exception:  # keep the host alive and the stream clean; detail goes to stderr
            log.exception("monitor failed")
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


def state_path(repo: str):
    return _cli.CONFIG_DIR / "host" / f"{_cli._stem(repo)}.json"


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
                monitor = Monitor(repo, client_factory(repo), EmittedState(state_path(repo)), send)
                monitor.start()
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

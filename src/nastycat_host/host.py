"""`nastycat-host`: a read-only Chrome Native Messaging host reporting the merge state of PRs.

It never writes to GitHub and keeps no state of its own: the extension owns the per-session
baseline and delivery records, so a restarted host can never replay or resurrect anything. stdout
carries protocol frames and nothing else; diagnostics go to stderr and never include `gh` output.

Client -> host:
    {"type": "check", "id": "<1-64 chars of [A-Za-z0-9_-]>", "prs": [{"repo": "owner/repo", "pr": 123}, ...]}
Host -> client:
    {"type": "result", "id": ..., "prs": [{"repo", "pr", "state": "open"|"closed", "merged": bool,
                                            "merged_at": "<ISO 8601>"|null}], "failed": [{"repo", "pr"}]}
    {"error": "<message>", "id": ...}           a rejected request (id is omitted if it was unusable)
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import re
import shutil
import sys
from pathlib import Path
from typing import BinaryIO, Callable, Optional

from misscat import GhError, MissCatError, _write_atomic

from badcat.github import GhClient
from badcat_host.host import (ConnectionClosed, EXTENSION_ID_RE, FALLBACK_PATH, FrameError, Sender,
                              default_hosts_dir, describe_gh_error, read_frame, validate_repo)

from . import __version__

log = logging.getLogger("nastycat_host")

HOST_NAME = "com.genonfire.nastycat_host"
MAX_PRS = 30  # per request; the extension splits larger sets
ID_RE = re.compile(r"[A-Za-z0-9_-]{1,64}")
MAX_PR_NUMBER = 2 ** 53 - 1


def parse_check(message: dict):
    """(id, [(repo, pr), ...]) from a `check` request, or MissCatError."""
    request_id = message.get("id")
    if not isinstance(request_id, str) or not ID_RE.fullmatch(request_id):
        raise MissCatError("invalid request id")
    prs = message.get("prs")
    if not isinstance(prs, list) or not 0 < len(prs) <= MAX_PRS:
        raise MissCatError("prs must be a list of 1 to %d entries" % MAX_PRS)
    out = []
    for item in prs:
        if not isinstance(item, dict) or set(item) != {"repo", "pr"}:
            raise MissCatError("invalid PR entry")
        number = item["pr"]
        if type(number) is not int or not 0 < number <= MAX_PR_NUMBER:
            raise MissCatError("invalid PR number")
        out.append((validate_repo(item["repo"]), number))
    return request_id, out


def pr_state(client, number: int) -> dict:
    """Merge state from the PR object itself; `closed` without a merge is never reported as merged."""
    data = client.pr(number)
    if not isinstance(data, dict):
        raise GhError("unexpected gh output")
    merged_at = data.get("merged_at")
    merged = data.get("merged") is True or (data.get("merged") is None and isinstance(merged_at, str))
    if merged and not isinstance(merged_at, str):
        raise GhError("merged PR without merged_at")  # fail closed rather than guess a time
    return {"state": "closed" if data.get("state") == "closed" else "open",
            "merged": merged, "merged_at": merged_at if merged else None}


def check(prs, client_factory: Callable[[str], object] = GhClient) -> dict:
    clients, answered, failed = {}, [], []
    for repo, number in prs:
        try:
            client = clients.setdefault(repo, client_factory(repo))
            answered.append({"repo": repo, "pr": number, **pr_state(client, number)})
        except GhError as exc:
            log.warning("PR check failed: %s", describe_gh_error(exc))
            failed.append({"repo": repo, "pr": number})
    return {"prs": answered, "failed": failed}


def serve(stdin: BinaryIO, stdout: BinaryIO,
          client_factory: Callable[[str], object] = GhClient) -> int:
    """Handle one Native Messaging connection until the client closes it."""
    send = Sender(stdout)
    try:
        while True:
            try:
                raw = read_frame(stdin)
            except FrameError as exc:
                log.error("%s", exc)
                send({"error": "invalid message framing"})
                return 1
            if raw is None:
                return 0
            try:
                message = json.loads(raw.decode("utf-8"))
            except ValueError:
                send({"error": "invalid JSON"})
                continue
            if not isinstance(message, dict) or message.get("type") != "check":
                send({"error": "unknown message"})
                continue
            try:
                request_id, prs = parse_check(message)
            except MissCatError as exc:
                rid = message.get("id")
                send({"error": str(exc), **({"id": rid} if isinstance(rid, str) and ID_RE.fullmatch(rid) else {})})
                continue
            try:
                send({"type": "result", "id": request_id, **check(prs, client_factory)})
            except Exception as exc:  # keep the host alive; no message/traceback (it could echo gh output)
                if isinstance(exc, ConnectionClosed):
                    raise
                log.error("check failed (%s)", type(exc).__name__)
                send({"error": "check failed", "id": request_id})
    except ConnectionClosed:
        return 0


# --------------------------------------------------------------------------- registration


def build_manifest(executable: str, extension_id: str) -> dict:
    return {
        "name": HOST_NAME,
        "description": "NastyCat PR merge-state host (read-only)",
        "path": executable,
        "type": "stdio",
        "allowed_origins": [f"chrome-extension://{extension_id}/"],
    }


def install_manifest(extension_id: str, executable: str, hosts_dir: Path) -> Path:
    """Write the per-user Chrome manifest; touches nothing but `hosts_dir`."""
    if not EXTENSION_ID_RE.fullmatch(extension_id):
        raise MissCatError("extension ID must be 32 characters a-p (see chrome://extensions)")
    if not os.path.isabs(executable) or not os.access(executable, os.X_OK):
        raise MissCatError(f"nastycat-host executable not found: {executable}")
    hosts_dir.mkdir(parents=True, exist_ok=True)
    path = hosts_dir / f"{HOST_NAME}.json"
    _write_atomic(path, json.dumps(build_manifest(executable, extension_id), indent=2) + "\n")
    return path


def find_executable() -> str:
    argv0 = Path(sys.argv[0])
    if argv0.name == "nastycat-host" and argv0.exists():
        return str(argv0.absolute())
    found = shutil.which("nastycat-host")
    if found is None:
        raise MissCatError("cannot locate the nastycat-host executable (is it on PATH?)")
    return str(Path(found).absolute())


def run_install(argv: list) -> int:
    parser = argparse.ArgumentParser(
        prog="nastycat-host install",
        description="Register nastycat-host as a Chrome Native Messaging host for this user (macOS).",
    )
    parser.add_argument("--extension-id", required=True, metavar="ID",
                        help="ID of the MissCat/NastyCat Chrome extension allowed to connect")
    args = parser.parse_args(argv)
    try:
        path = install_manifest(args.extension_id, find_executable(), default_hosts_dir())
    except (MissCatError, OSError) as exc:
        print(f"nastycat-host: {exc}", file=sys.stderr)
        return 2
    print(f"Registered {HOST_NAME} for chrome-extension://{args.extension_id}/\n{path}")
    return 0


# --------------------------------------------------------------------------- entry point


def main(argv: Optional[list] = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    if argv[:1] == ["install"]:
        return run_install(argv[1:])
    parser = argparse.ArgumentParser(
        prog="nastycat-host",
        description="Read-only Chrome Native Messaging host reporting whether GitHub PRs are merged.",
        epilog="Run `nastycat-host install --extension-id ID` once to register it with Chrome.\n"
               "Chrome starts the host itself; it speaks length-prefixed JSON on stdin/stdout.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("-v", "--version", action="version", version=f"%(prog)s {__version__}")
    if any(arg in ("-h", "--help", "-v", "--version") for arg in argv):
        parser.parse_args(argv)
    if sys.stdin.isatty():
        parser.print_help()
        return 2
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

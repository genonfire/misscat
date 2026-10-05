"""BadCat CLI: watch submitted PR reviews, report transitions, optionally squash-merge."""
from __future__ import annotations

import argparse
import json
import logging
import os
import shutil
import sys
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Callable, Optional

from misscat import GhError, MissCatError, _write_atomic, resolve_repo_arg

from . import __version__
from .github import GhClient, OpenPR
from .protocol import BLOCKED, INVALID, PLUS1, READY, WAITING, Verdict, evaluate

log = logging.getLogger("badcat")

CONFIG_DIR = Path.home() / ".config" / "badcat"  # separate from MissCat's state
LOCK_ROOT = Path.home() / ".cache" / "badcat" / "locks"
POLL_INTERVAL = 60.0
ERROR_BACKOFF = (60.0, 120.0, 300.0)
STATE_VERSION = 1
# GitHub's own mergeable_state values that do not contradict a merge. "draft" is GitHub's
# literal label, not a BadCat decision: the merge is still attempted and GitHub's rejection
# is reported. Everything else (blocked, behind, dirty, unstable, unknown...) fails closed.
MERGEABLE_STATES = ("clean", "has_hooks", "draft")
PASSING = ("success", "neutral", "skipped")


class BadCatError(MissCatError):
    pass


def _stem(repo: str) -> str:
    return repo.replace("/", "__")


def state_path(repo: str) -> Path:
    return CONFIG_DIR / f"{_stem(repo)}.json"


@contextmanager
def merger_lock(repo: str):
    """Nonblocking OS lock for the watcher's lifetime; the file stays on disk (existence != owner)."""
    path = LOCK_ROOT / f"{_stem(repo)}.lock"
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        handle = path.open("a+b")
    except OSError as exc:
        raise BadCatError(f"cannot open lock {path}: {exc}") from exc
    acquired = False
    try:
        try:
            if os.name == "nt":
                import msvcrt
                handle.seek(0)
                if not handle.read(1):
                    handle.seek(0)
                    handle.write(bytes([0]))
                    handle.flush()
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            acquired = True
        except OSError as exc:
            raise BadCatError(
                f"BadCat is already watching {repo}, or its lock is unavailable ({exc})."
            ) from exc
        yield
    finally:
        if acquired:
            if os.name == "nt":
                import msvcrt
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        handle.close()


class NotifyState:
    """Last reported status per PR. Only dedupes notifications; never authorizes a merge."""

    def __init__(self, path: Path):
        self.path, self.prs = path, {}
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
            if raw.get("version") != STATE_VERSION or not isinstance(raw.get("prs"), dict):
                raise ValueError("incompatible state")
            self.prs = {str(k): dict(v) for k, v in raw["prs"].items()}
        except FileNotFoundError:
            pass
        except (OSError, ValueError, AttributeError, TypeError) as exc:
            log.warning("ignoring unreadable notification state %s (%s)", path, exc)

    def get(self, number: int) -> Optional[dict]:
        return self.prs.get(str(number))

    def set(self, number: int, entry: dict) -> None:
        if self.prs.get(str(number)) != entry:
            self.prs[str(number)] = entry
            self._save()

    def drop(self, number: int) -> None:
        if self.prs.pop(str(number), None) is not None:
            self._save()

    def _save(self) -> None:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            _write_atomic(self.path, json.dumps({"version": STATE_VERSION, "prs": self.prs}))
        except OSError as exc:
            log.warning("cannot save notification state: %s", exc)


def ci_gate(client, head: str, required: tuple) -> Optional[str]:
    """None when exact-HEAD CI passes, else the reason. Fails closed on anything unclear."""
    runs = client.check_runs(head)
    status = client.combined_status(head)
    successes = 0
    for run in runs:
        name = run.get("name", "?")
        if run.get("head_sha") != head:
            return f"stale check run {name}"
        if run.get("status") != "completed":
            return f"check {name} pending"
        conclusion = run.get("conclusion")
        if conclusion not in PASSING:
            return f"check {name} {conclusion or 'indeterminate'}"
        successes += conclusion == "success"
    if status.get("sha") not in (None, head):
        return "stale commit status"
    if status.get("total_count"):
        state = status.get("state")
        if state != "success":
            return f"commit status {state or 'indeterminate'}"
        successes += 1
    for name in required:
        matching = [r for r in runs if r.get("name") == name]
        # a skipped job is not an executed passing one
        if not matching or max(matching, key=lambda r: r.get("id", 0)).get("conclusion") != "success":
            return f"required check {name} has not succeeded"
    if not successes:
        return "no passing CI reported"
    return None


def merge_gate(client, raw: dict, head: str, required: tuple) -> Optional[str]:
    """None when a merge may be attempted for exactly `head`, else the reason."""
    if raw.get("merged"):
        return "already merged"
    if raw.get("state") != "open":
        return "closed"
    if (raw.get("head") or {}).get("sha") != head:
        return "HEAD moved"
    if raw.get("mergeable") is None:
        return "mergeability not computed yet"
    if raw.get("mergeable") is not True:
        return "not mergeable"
    if raw.get("mergeable_state") not in MERGEABLE_STATES:
        return f"mergeable_state={raw.get('mergeable_state')}"
    return ci_gate(client, head, required)


class Watcher:
    def __init__(
        self,
        repo: str,
        client,
        state: NotifyState,
        trusted: frozenset,
        merge: bool = False,
        required: tuple = (),
        sleep: Callable[[float], None] = time.sleep,
    ):
        self.repo, self.client, self.state = repo, client, state
        self.trusted, self.merge, self.required, self.sleep = trusted, merge, required, sleep
        self.errors = 0
        self.last_error: Optional[str] = None
        self.rejected: dict = {}  # PR number -> state at the last merge rejection

    def run_forever(self) -> None:
        while True:
            self.sleep(self.cycle())

    def cycle(self) -> float:
        """One poll; returns the delay before the next one."""
        try:
            prs = self.client.open_prs()
            ok = True
            for pr in sorted(prs, key=lambda p: p.number):
                ok = self._guard(pr.number, self._process, pr) and ok
            open_numbers = {p.number for p in prs}
            for key in list(self.state.prs):
                if int(key) not in open_numbers:
                    ok = self._guard(int(key), self._finished, int(key)) and ok
        except GhError as exc:
            self._error("cannot list PRs", exc)
            ok = False
        if ok:
            self.errors, self.last_error = 0, None
            return POLL_INTERVAL
        delay = ERROR_BACKOFF[min(self.errors, len(ERROR_BACKOFF) - 1)]
        self.errors += 1
        return delay

    def _guard(self, number: int, fn: Callable, *args) -> bool:
        try:
            fn(*args)
            return True
        except GhError as exc:
            self._error(f"PR #{number}", exc)
            return False

    def _error(self, what: str, exc: Exception) -> None:
        message = f"{what}: {exc}"
        if message != self.last_error:  # report a persisting error once
            log.warning("%s", message)
        self.last_error = message

    def _verdict(self, number: int, head: str) -> Verdict:
        return evaluate(self.client.reviews(number), head, self.trusted)

    def _process(self, pr: OpenPR) -> None:
        verdict = self._verdict(pr.number, pr.head)
        gate = None
        raw = None
        if verdict.stage == READY:
            raw = self.client.pr(pr.number)
            gate = merge_gate(self.client, raw, pr.head, self.required)
        self._report(pr, verdict, gate)
        if verdict.stage == READY and gate is None and self.merge:
            self._merge(pr)

    def _report(self, pr: OpenPR, verdict: Verdict, gate: Optional[str]) -> None:
        short, prev = pr.head[:7], self.state.get(pr.number)
        stage = f"{verdict.stage} {verdict.detail}".strip()
        gate_text = "ok" if gate is None else gate
        if prev is None:
            log.info("PR #%d: new (%s)", pr.number, short)
        elif prev.get("head") != pr.head:
            log.info("PR #%d: new HEAD (%s)", pr.number, short)
        if prev is None or prev.get("head") != pr.head or prev.get("stage") != stage:
            label = {
                WAITING: "WAITING", PLUS1: "+1", BLOCKED: "-1 BLOCKED", READY: "+2",
                INVALID: "INVALID review",
            }[verdict.stage]
            detail = f": {verdict.detail}" if verdict.detail and verdict.stage != BLOCKED else ""
            level = logging.WARNING if verdict.stage in (BLOCKED, INVALID) else logging.INFO
            log.log(level, "PR #%d: %s%s (%s)", pr.number, label, detail, short)
        entry = {"head": pr.head, "stage": stage, "gate": None}
        if verdict.stage == READY:
            entry["gate"] = gate_text
            if prev is None or prev.get("head") != pr.head or prev.get("gate") != gate_text:
                if gate is None:
                    log.info("PR #%d: ready to merge%s", pr.number,
                             "" if self.merge else " (notify only)")
                else:
                    log.info("PR #%d: waiting: %s", pr.number, gate)
        self.state.set(pr.number, entry)

    def _merge(self, pr: OpenPR) -> None:
        # Re-read everything from GitHub immediately before the only write.
        if self._verdict(pr.number, pr.head).stage != READY:
            log.info("PR #%d: review state changed, not merging", pr.number)
            return
        raw = self.client.pr(pr.number)
        reason = merge_gate(self.client, raw, pr.head, self.required)
        if reason is not None:
            log.info("PR #%d: not merging: %s", pr.number, reason)
            return
        marker = (pr.head, raw.get("draft"), raw.get("mergeable_state"))
        if self.rejected.get(pr.number) == marker:
            return  # same rejected situation: no repeated requests
        try:
            self.client.merge(pr.number, pr.head)
        except GhError as exc:
            if self._merged_now(pr.number):  # e.g. a timeout after GitHub already merged it
                return
            self.rejected[pr.number] = marker
            log.warning("PR #%d: merge rejected: %s", pr.number, exc)
            return
        self.rejected.pop(pr.number, None)
        log.info("PR #%d: merged (squash, %s)", pr.number, pr.head[:7])
        self.state.drop(pr.number)

    def _merged_now(self, number: int) -> bool:
        try:
            if self.client.pr(number).get("merged"):
                log.info("PR #%d: merged", number)
                self.state.drop(number)
                return True
        except GhError:
            pass
        return False

    def _finished(self, number: int) -> None:
        raw = self.client.pr(number)  # a GhError keeps the entry; retried next poll
        log.info("PR #%d: %s", number, "merged" if raw.get("merged") else "closed")
        self.rejected.pop(number, None)
        self.state.drop(number)


def main(argv: Optional[list] = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    parser = argparse.ArgumentParser(
        prog="badcat",
        description="Watch GitHub PR review results; with --merge, squash-merge eligible PRs.",
        epilog=(
            "Default is notify only: BadCat never writes to GitHub without --merge.\n"
            "A PR is eligible when the current HEAD has a valid +1 followed by a valid +2 from a\n"
            "trusted reviewer, no -1, passing CI and a clean GitHub mergeability state.\n"
            "Trusted reviewers: --trusted-reviewer LOGIN (repeatable), default the `gh` user.\n"
            "'.' means the current Git repository's `origin` remote (github.com only).\n"
            "Disable any other automatic merger for the repository when using --merge."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("repo", metavar="owner/repo|.")
    parser.add_argument("--merge", action="store_true",
                        help="squash-merge PRs that pass every gate (default: notify only)")
    parser.add_argument("--trusted-reviewer", action="append", default=[], metavar="LOGIN",
                        help="GitHub login whose review markers count (repeatable)")
    parser.add_argument("--required-check", action="append", default=[], metavar="NAME",
                        help="check run that must have succeeded on the HEAD (repeatable)")
    parser.add_argument("-v", "--version", action="version", version=f"%(prog)s {__version__}")
    if not argv:
        parser.print_help()
        return 2
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s",
                        datefmt="%H:%M:%S")
    try:
        repo = resolve_repo_arg(args.repo)
        if shutil.which("gh") is None:
            raise BadCatError("required CLI not found on PATH: gh")
        with merger_lock(repo):
            client = GhClient(repo)
            if args.trusted_reviewer:
                trusted = frozenset(t.lower() for t in args.trusted_reviewer)
            else:
                try:
                    trusted = frozenset({client.viewer().lower()})
                except (GhError, KeyError) as exc:
                    raise BadCatError(f"cannot determine the authenticated gh user: {exc}") from exc
            log.info("Watching %s (%s; trusted reviewers: %s)", repo,
                     "merge enabled" if args.merge else "notify only", ", ".join(sorted(trusted)))
            Watcher(repo, client, NotifyState(state_path(repo)), trusted, args.merge,
                    tuple(args.required_check)).run_forever()
    except MissCatError as exc:
        print(f"badcat: {exc}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        log.info("stopped")
        return 130
    return 0


if __name__ == "__main__":
    sys.exit(main())

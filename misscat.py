#!/usr/bin/env python3
"""MissCat - never miss a single commit.

Watches a GitHub repository and runs an AI review once for every new PR HEAD.
Single-threaded: one review at a time, then an immediate re-check before sleeping.
"""
from __future__ import annotations

import argparse
import fcntl
import json
import logging
import os
import re
import shutil
import signal
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

import yaml

log = logging.getLogger("misscat")

CONFIG_DIR = Path.home() / ".config" / "misscat"
STATE_DIR = Path.home() / ".local" / "state" / "misscat"
DEFAULT_CONFIG = Path(__file__).with_name("default.yml")
REPO_RE = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
PROVIDERS = ("claude", "codex")

PENDING, IN_PROGRESS, COMPLETED, FAILED = "pending", "in_progress", "completed", "failed"


class MissCatError(Exception):
    """Errors that should end the process with a clean message."""


class ConfigError(MissCatError):
    pass


class StateError(MissCatError):
    pass


class GhError(Exception):
    pass


# --------------------------------------------------------------------------- config


def deep_merge(base: dict, override: dict) -> dict:
    out = dict(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = deep_merge(out[key], value)
        else:
            out[key] = value
    return out


def _load_yaml(path: Path) -> dict:
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        raise ConfigError(f"cannot read {path}: {exc}") from exc
    if data is None:
        return {}
    if not isinstance(data, dict):
        raise ConfigError(f"{path}: top level must be a mapping")
    return data


def resolve_profile(name: str | None) -> Path | None:
    """A profile is a bare filename living in ~/.config/misscat/, never a path."""
    if name is None:
        return None
    if name in (".", "..") or name != Path(name).name or "\\" in name:
        raise ConfigError(f"profile must be a filename, not a path: {name!r}")
    path = CONFIG_DIR / name
    if not path.is_file():
        raise ConfigError(f"profile not found: {path}")
    return path


@dataclass(frozen=True)
class Settings:
    provider: str
    model: str
    prompt: str
    idle: tuple[float, ...]
    active: tuple[float, ...]
    include_drafts: bool
    timeout: float
    max_attempts: int
    retry_delay: float


def _schedule(value, name: str) -> tuple[float, ...]:
    ok = isinstance(value, list) and value and all(
        isinstance(v, (int, float)) and not isinstance(v, bool) and v > 0 for v in value
    )
    if not ok:
        raise ConfigError(f"{name} must be a non-empty list of positive numbers")
    return tuple(float(v) for v in value)


def _positive(value, name: str, integer: bool = False) -> float:
    kind = int if integer else (int, float)
    if isinstance(value, bool) or not isinstance(value, kind) or value <= 0:
        raise ConfigError(f"{name} must be a positive {'integer' if integer else 'number'}")
    return value


def build_settings(cfg: dict) -> Settings:
    try:
        reviewer, watch, review = cfg["reviewer"], cfg["watch"], cfg["review"]
        provider, model = reviewer["provider"], reviewer["model"]
        prompt = cfg["prompt"]
        if provider not in PROVIDERS:
            raise ConfigError(f"reviewer.provider must be one of {', '.join(PROVIDERS)}")
        if not isinstance(model, str) or not model.strip():
            raise ConfigError("reviewer.model must be a non-empty string")
        if not isinstance(prompt, str) or not prompt.strip():
            raise ConfigError("prompt must be a non-empty string")
        if not isinstance(review["include_drafts"], bool):
            raise ConfigError("review.include_drafts must be true or false")
        return Settings(
            provider=provider,
            model=model.strip(),
            prompt=prompt,
            idle=_schedule(watch["idle"], "watch.idle"),
            active=_schedule(watch["active"], "watch.active"),
            include_drafts=review["include_drafts"],
            timeout=float(_positive(review["timeout"], "review.timeout")),
            max_attempts=int(_positive(review["max_attempts"], "review.max_attempts", integer=True)),
            retry_delay=float(_positive(review["retry_delay"], "review.retry_delay")),
        )
    except (KeyError, TypeError) as exc:
        raise ConfigError(f"missing or malformed config section: {exc}") from exc


def load_settings(profile: str | None) -> Settings:
    cfg = _load_yaml(DEFAULT_CONFIG)
    path = resolve_profile(profile)
    if path is not None:
        cfg = deep_merge(cfg, _load_yaml(path))
    return build_settings(cfg)


# --------------------------------------------------------------------------- GitHub


@dataclass(frozen=True)
class PR:
    number: int
    head: str
    draft: bool
    url: str
    title: str


def gh_open_prs(repo: str) -> list[PR]:
    cmd = [
        "gh", "pr", "list", "--repo", repo, "--state", "open", "--limit", "200",
        "--json", "number,headRefOid,isDraft,url,title",
    ]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise GhError(str(exc)) from exc
    if proc.returncode != 0:
        raise GhError(proc.stderr.strip() or f"gh exited {proc.returncode}")
    try:
        return [
            PR(int(d["number"]), d["headRefOid"], bool(d["isDraft"]), d["url"], d["title"])
            for d in json.loads(proc.stdout)
        ]
    except (ValueError, KeyError, TypeError) as exc:
        raise GhError(f"unexpected gh output: {exc}") from exc


# --------------------------------------------------------------------------- reviewer
# Minimal backend so the watcher is runnable. Issue #2 owns the real design
# (permissions, result posting, per-provider flags).

COMMANDS: dict[str, Callable[[str, str], list[str]]] = {
    "claude": lambda model, prompt: ["claude", "-p", prompt, "--model", model],
    "codex": lambda model, prompt: ["codex", "exec", "--model", model, prompt],
}


def build_prompt(settings: Settings, repo: str, pr: PR) -> str:
    context = (
        f"Repository: {repo}\n"
        f"Pull request: #{pr.number} ({pr.url})\n"
        f"HEAD SHA: {pr.head}\n"
        "Review exactly this HEAD. Do not modify code, push commits, or merge."
    )
    return f"{settings.prompt.strip()}\n\n{context}"


def _kill_group(proc: subprocess.Popen) -> None:
    for sig in (signal.SIGTERM, signal.SIGKILL):
        try:
            os.killpg(proc.pid, sig)
        except ProcessLookupError:
            break
        try:
            proc.wait(timeout=5)
            break
        except subprocess.TimeoutExpired:
            continue


def run_cli_reviewer(settings: Settings, repo: str, pr: PR) -> bool:
    """Run the reviewer CLI to completion. True only on a clean exit."""
    cmd = COMMANDS[settings.provider](settings.model, build_prompt(settings, repo, pr))
    try:
        proc = subprocess.Popen(
            cmd, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT, text=True, start_new_session=True,
        )
    except OSError as exc:
        log.error("cannot start %s: %s", cmd[0], exc)
        return False
    try:
        out, _ = proc.communicate(timeout=settings.timeout)
    except subprocess.TimeoutExpired:
        _kill_group(proc)
        log.error("%s timed out after %ds", cmd[0], settings.timeout)
        return False
    except BaseException:  # Ctrl-C / SIGTERM: never leave the reviewer running
        _kill_group(proc)
        raise
    if proc.returncode != 0:
        log.error("%s exited %d: %s", cmd[0], proc.returncode, (out or "").strip()[-500:])
        return False
    return True


# --------------------------------------------------------------------------- state


class State:
    """Per-PR review state, persisted atomically as JSON.

    Record: {head, status, attempts, next_retry, active_step}
    active_step is None until a HEAD of that PR has been reviewed successfully.
    """

    def __init__(self, path: Path):
        self.path = path
        self.prs: dict[str, dict] = {}
        self.idle_step = 0

    @classmethod
    def load(cls, path: Path) -> "State":
        state = cls(path)
        if path.exists():
            try:
                raw = json.loads(path.read_text(encoding="utf-8"))
                state.prs = dict(raw["prs"])
                state.idle_step = int(raw.get("idle_step", 0))
            except (OSError, ValueError, KeyError, TypeError) as exc:
                raise StateError(
                    f"unreadable state file {path} ({exc}); fix or delete it to continue"
                ) from exc
        for number, rec in state.prs.items():
            if rec.get("status") == IN_PROGRESS:  # interrupted last run: eligible for retry
                rec["status"], rec["next_retry"] = FAILED, 0
                log.warning("PR #%s: previous review was interrupted, will retry", number)
        return state

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_name(self.path.name + ".tmp")
        tmp.write_text(
            json.dumps({"version": 1, "idle_step": self.idle_step, "prs": self.prs}, indent=2),
            encoding="utf-8",
        )
        os.replace(tmp, self.path)


# --------------------------------------------------------------------------- watcher


def _fmt(seconds: float) -> str:
    seconds = int(round(seconds))
    return f"{seconds // 60}m" if seconds >= 60 and seconds % 60 == 0 else f"{seconds}s"


class Watcher:
    def __init__(
        self,
        repo: str,
        settings: Settings,
        state: State,
        list_prs: Callable[[str], list[PR]],
        reviewer: Callable[[Settings, str, PR], bool],
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.time,
    ):
        self.repo, self.s, self.state = repo, settings, state
        self.list_prs, self.reviewer, self.sleep, self.clock = list_prs, reviewer, sleep, clock

    def run_forever(self) -> None:
        while True:
            self.cycle()

    def cycle(self) -> None:
        """One iteration: review one PR HEAD if any is waiting, otherwise sleep."""
        try:
            prs = self.list_prs(self.repo)
        except GhError as exc:
            log.warning("cannot list PRs: %s", exc)
            self._sleep_idle_step()
            return
        self._sync(prs)
        pr = self._next_reviewable(prs)
        if pr is not None:
            self._review(pr)  # afterwards: loop straight back to a fresh listing, no sleep
        else:
            self._sleep_adaptive(prs)

    # -- state bookkeeping

    def _sync(self, prs: list[PR]) -> None:
        open_numbers = {str(p.number) for p in prs}
        for number in [n for n in self.state.prs if n not in open_numbers]:
            del self.state.prs[number]
            log.info("PR #%s closed, no longer tracked", number)
        for pr in prs:
            rec = self.state.prs.get(str(pr.number))
            if rec is None or rec["head"] != pr.head:
                if rec is not None:
                    log.info("PR #%d: new HEAD %s", pr.number, pr.head[:7])
                else:
                    log.info("PR #%d: discovered (%s)", pr.number, pr.head[:7])
                self.state.prs[str(pr.number)] = {
                    "head": pr.head, "status": PENDING, "attempts": 0, "next_retry": 0,
                    "active_step": rec["active_step"] if rec else None,
                }
        self.state.save()

    def _eligible(self, pr: PR, rec: dict, now: float) -> bool:
        if pr.draft and not self.s.include_drafts:
            return False
        if rec["status"] == COMPLETED:
            return False
        if rec["status"] == FAILED:
            return rec["attempts"] < self.s.max_attempts and now >= rec["next_retry"]
        return True

    def _next_reviewable(self, prs: list[PR]) -> PR | None:
        now = self.clock()
        for pr in sorted(prs, key=lambda p: p.number):  # oldest PR first
            if self._eligible(pr, self.state.prs[str(pr.number)], now):
                return pr
        return None

    # -- one review

    def _review(self, pr: PR) -> None:
        rec = self.state.prs[str(pr.number)]
        rec["status"], rec["attempts"] = IN_PROGRESS, rec["attempts"] + 1
        self.state.save()  # persisted first: a crash leaves it in_progress -> retried
        log.info("PR #%d: review start (%s, attempt %d/%d)",
                 pr.number, pr.head[:7], rec["attempts"], self.s.max_attempts)
        try:
            ok = self.reviewer(self.s, self.repo, pr)
        except Exception:
            log.exception("PR #%d: reviewer crashed", pr.number)
            ok = False
        if ok:
            rec.update(status=COMPLETED, next_retry=0, active_step=0)
            self.state.idle_step = 0
            log.info("PR #%d: review done", pr.number)
        else:
            rec.update(status=FAILED, next_retry=self.clock() + self.s.retry_delay)
            if rec["attempts"] >= self.s.max_attempts:
                log.error("PR #%d: giving up on %s until a new HEAD is pushed", pr.number, pr.head[:7])
            else:
                log.warning("PR #%d: review failed, retry in %s", pr.number, _fmt(self.s.retry_delay))
        self.state.save()

    # -- adaptive polling

    def _sleep_idle_step(self) -> None:
        delay = self.s.idle[min(self.state.idle_step, len(self.s.idle) - 1)]
        self.state.idle_step = min(self.state.idle_step + 1, len(self.s.idle) - 1)
        self.state.save()
        self.sleep(delay)

    def _sleep_adaptive(self, prs: list[PR]) -> None:
        now = self.clock()
        recs = [(pr, self.state.prs[str(pr.number)]) for pr in prs]
        active = [r for _, r in recs if r["active_step"] is not None]
        if active:  # several PRs, one loop: the most impatient PR sets the pace
            poll = min(self.s.active[min(r["active_step"], len(self.s.active) - 1)] for r in active)
            self.state.idle_step = 0
        else:
            poll = self.s.idle[min(self.state.idle_step, len(self.s.idle) - 1)]
        retry_waits = [
            max(0.0, r["next_retry"] - now)
            for pr, r in recs
            if r["status"] == FAILED
            and r["attempts"] < self.s.max_attempts
            and not (pr.draft and not self.s.include_drafts)
        ]
        wait = min([poll, *retry_waits])
        log.info("nothing to review, next check in %s", _fmt(wait))
        self.state.save()
        self.sleep(wait)
        if wait >= poll:  # only a full poll interval advances the schedule
            if active:
                for r in active:
                    r["active_step"] = min(r["active_step"] + 1, len(self.s.active) - 1)
            else:
                self.state.idle_step = min(self.state.idle_step + 1, len(self.s.idle) - 1)
            self.state.save()


# --------------------------------------------------------------------------- CLI


def _acquire_lock(path: Path):
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = open(path, "w")
    try:
        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        handle.close()
        raise MissCatError("another misscat is already watching this repository and profile")
    return handle


def _on_term(signum, frame):
    raise KeyboardInterrupt


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    parser = argparse.ArgumentParser(
        prog="misscat",
        description="Watch a GitHub repository and run an AI review on every new PR HEAD.",
        epilog="profile.yml is a filename resolved as ~/.config/misscat/<profile.yml>.",
    )
    parser.add_argument("repo", metavar="owner/repo")
    parser.add_argument("profile", metavar="profile.yml", nargs="?")
    if not argv:
        parser.print_help()
        return 2
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s",
                        datefmt="%H:%M:%S")
    try:
        if not REPO_RE.match(args.repo):
            raise MissCatError(f"repository must look like owner/repo: {args.repo!r}")
        settings = load_settings(args.profile)
        for tool in ("gh", settings.provider):
            if shutil.which(tool) is None:
                raise MissCatError(f"required CLI not found on PATH: {tool}")
        profile_id = Path(args.profile).stem if args.profile else "default"
        stem = f"{args.repo.replace('/', '__')}__{profile_id}"
        lock = _acquire_lock(STATE_DIR / f"{stem}.lock")  # noqa: F841 (held for process lifetime)
        state = State.load(STATE_DIR / f"{stem}.json")
    except MissCatError as exc:
        print(f"misscat: {exc}", file=sys.stderr)
        return 2

    signal.signal(signal.SIGTERM, _on_term)
    log.info("watching %s with %s/%s", args.repo, settings.provider, settings.model)
    try:
        Watcher(args.repo, settings, state, gh_open_prs, run_cli_reviewer).run_forever()
    except KeyboardInterrupt:
        log.info("stopped")
        return 130
    return 0


if __name__ == "__main__":
    sys.exit(main())


#!/usr/bin/env python3
"""Watch a GitHub repository and review each new open-PR HEAD once."""
from __future__ import annotations

import argparse
import json
import logging
import os
import re
import shutil
import subprocess
import sys
import sysconfig
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, NamedTuple


log = logging.getLogger("misscat")

CONFIG_DIR = Path.home() / ".config" / "misscat"
STATE_FILE = CONFIG_DIR / "state.json"
REPO_RE = re.compile(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+\Z")
PROFILE_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]*\Z")
PROVIDERS = ("claude", "codex")
FAILURE_WAIT = 300.0
GH_LIMIT = 1000


class MissCatError(Exception):
    """A user-facing error that can be printed without a traceback."""


class ConfigError(MissCatError):
    pass


class StateError(MissCatError):
    pass


class GhError(MissCatError):
    pass


def deep_merge(base: dict, override: dict) -> dict:
    """Return a recursive mapping merge without mutating either input."""
    merged = dict(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = deep_merge(merged[key], value)
        else:
            merged[key] = value
    return merged


def profile_path(name: str) -> Path:
    """Resolve a plain profile name to ~/.config/misscat/<name>.yml."""
    if (
        not name
        or not PROFILE_RE.fullmatch(name)
        or "/" in name
        or "\\" in name
        or ".." in name
    ):
        raise ConfigError(
            f"invalid profile name {name!r}; pass a name such as 'sol', not a path"
        )
    if name.lower().endswith((".yml", ".yaml")):
        raise ConfigError(
            f"invalid profile name {name!r}; pass a name without a YAML filename suffix"
        )

    root = CONFIG_DIR.resolve()
    candidate = root / f"{name}.yml"
    # Do not let a profile symlink escape the profile directory.
    try:
        if candidate.resolve().parent != root:
            raise ConfigError(f"profile {name!r} resolves outside {root}")
    except (OSError, RuntimeError) as exc:
        raise ConfigError(f"cannot resolve profile {name!r}: {exc}") from exc
    if not candidate.is_file():
        raise ConfigError(f"profile {name!r} not found: {candidate}")
    return candidate


def default_config_path() -> Path:
    """Find default.yml beside the source or in the installed data directory."""
    source_file = Path(__file__).resolve().with_name("default.yml")
    if source_file.is_file():
        return source_file
    installed_file = (
        Path(sysconfig.get_path("data")) / "share" / "misscat" / "default.yml"
    )
    return installed_file


def _load_yaml(path: Path) -> dict:
    try:
        import yaml
    except ImportError as exc:
        raise ConfigError("PyYAML is required; install MissCat with `python -m pip install .`") from exc

    try:
        value = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        raise ConfigError(f"cannot read configuration {path}: {exc}") from exc
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise ConfigError(f"{path}: the top level must be a YAML mapping")
    return value


@dataclass(frozen=True)
class Settings:
    provider: str
    model: str
    prompt: str
    idle: tuple[float, ...]
    active: tuple[float, ...]
    include_drafts: bool


def _schedule(value, name: str) -> tuple[float, ...]:
    valid = (
        isinstance(value, list)
        and bool(value)
        and all(
            isinstance(delay, (int, float))
            and not isinstance(delay, bool)
            and delay > 0
            for delay in value
        )
    )
    if not valid:
        raise ConfigError(f"{name} must be a non-empty list of positive numbers")
    return tuple(float(delay) for delay in value)


def build_settings(config: dict) -> Settings:
    try:
        reviewer = config["reviewer"]
        watch = config["watch"]
        review = config["review"]
        provider = reviewer["provider"]
        model = reviewer["model"]
        prompt = config["prompt"]
        include_drafts = review["include_drafts"]
    except (KeyError, TypeError) as exc:
        raise ConfigError(f"missing or malformed configuration section: {exc}") from exc

    for section_name, section in (
        ("reviewer", reviewer),
        ("watch", watch),
        ("review", review),
    ):
        if not isinstance(section, dict):
            raise ConfigError(f"{section_name} must be a YAML mapping")

    if provider not in PROVIDERS:
        raise ConfigError(f"reviewer.provider must be one of: {', '.join(PROVIDERS)}")
    if not isinstance(model, str) or not model.strip():
        raise ConfigError("reviewer.model must be a non-empty string")
    if not isinstance(prompt, str) or not prompt.strip():
        raise ConfigError("prompt must be a non-empty string")
    if not isinstance(include_drafts, bool):
        raise ConfigError("review.include_drafts must be true or false")

    return Settings(
        provider=provider,
        model=model.strip(),
        prompt=prompt,
        idle=_schedule(watch.get("idle"), "watch.idle"),
        active=_schedule(watch.get("active"), "watch.active"),
        include_drafts=include_drafts,
    )


def load_settings(profile: str | None) -> Settings:
    config = _load_yaml(default_config_path())
    if profile is not None:
        config = deep_merge(config, _load_yaml(profile_path(profile)))
    return build_settings(config)


@dataclass(frozen=True)
class PullRequest:
    number: int
    head_sha: str
    is_draft: bool
    url: str
    title: str


def gh_open_prs(repo: str) -> list[PullRequest]:
    command = [
        "gh",
        "pr",
        "list",
        "--repo",
        repo,
        "--state",
        "open",
        "--limit",
        str(GH_LIMIT),
        "--json",
        "number,headRefOid,isDraft,url,title",
    ]
    try:
        result = subprocess.run(command, capture_output=True, text=True, timeout=60)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise GhError(f"could not run gh pr list: {exc}") from exc
    if result.returncode != 0:
        detail = result.stderr.strip() or f"gh exited with status {result.returncode}"
        raise GhError(detail)

    try:
        rows = json.loads(result.stdout)
        if not isinstance(rows, list):
            raise TypeError("expected a JSON array")
        prs = []
        for row in rows:
            if not isinstance(row, dict):
                raise TypeError("expected each pull request to be an object")
            number = row["number"]
            head_sha = row["headRefOid"]
            is_draft = row["isDraft"]
            url = row["url"]
            title = row["title"]
            if not isinstance(number, int) or isinstance(number, bool):
                raise TypeError("pull request number is not an integer")
            if not isinstance(head_sha, str) or not head_sha:
                raise TypeError(f"pull request #{number} has no HEAD SHA")
            if not isinstance(is_draft, bool):
                raise TypeError(f"pull request #{number} has an invalid draft flag")
            if not isinstance(url, str) or not isinstance(title, str):
                raise TypeError(f"pull request #{number} has invalid text fields")
            prs.append(PullRequest(number, head_sha, is_draft, url, title))
        return prs
    except (ValueError, KeyError, TypeError) as exc:
        raise GhError(f"unexpected gh pr list output: {exc}") from exc


class ReviewKey(NamedTuple):
    repo: str
    pr: int
    head_sha: str
    profile: str | None


class State:
    """Successfully reviewed PR HEADs stored in ~/.config/misscat/state.json."""

    def __init__(self, path: Path):
        self.path = path

    def reviewed(self) -> set[ReviewKey]:
        if not self.path.exists():
            return set()
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
            if not isinstance(raw, dict) or raw.get("version") != 1:
                raise ValueError("unsupported state file format")
            rows = raw.get("reviewed")
            if not isinstance(rows, list):
                raise ValueError("reviewed must be an array")
            keys = set()
            for row in rows:
                if not isinstance(row, dict):
                    raise TypeError("review entry must be an object")
                repo, pr, head_sha, profile = (
                    row["repo"], row["pr"], row["head_sha"], row["profile"]
                )
                if (
                    not isinstance(repo, str)
                    or not isinstance(pr, int)
                    or isinstance(pr, bool)
                    or not isinstance(head_sha, str)
                    or (profile is not None and not isinstance(profile, str))
                ):
                    raise TypeError("review entry has invalid field types")
                keys.add(ReviewKey(repo, pr, head_sha, profile))
            return keys
        except (OSError, ValueError, KeyError, TypeError) as exc:
            raise StateError(
                f"cannot read state file {self.path} ({exc}); fix or remove it to continue"
            ) from exc

    def add(self, key: ReviewKey) -> None:
        keys = self.reviewed()
        keys.add(key)
        self._write(keys)

    def _write(self, keys: set[ReviewKey]) -> None:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            rows = [
                {
                    "repo": key.repo,
                    "pr": key.pr,
                    "head_sha": key.head_sha,
                    "profile": key.profile,
                }
                for key in sorted(
                    keys,
                    key=lambda item: (
                        item.repo,
                        item.pr,
                        item.head_sha,
                        item.profile or "",
                    ),
                )
            ]
            fd, temp_name = tempfile.mkstemp(
                prefix=f".{self.path.name}.", suffix=".tmp", dir=self.path.parent
            )
            temp_path = Path(temp_name)
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as stream:
                    json.dump({"version": 1, "reviewed": rows}, stream, indent=2)
                    stream.write("\n")
                    stream.flush()
                    os.fsync(stream.fileno())
                os.replace(temp_path, self.path)
            finally:
                if temp_path.exists():
                    temp_path.unlink()
        except OSError as exc:
            raise StateError(f"cannot write state file {self.path}: {exc}") from exc


def build_prompt(settings: Settings, repo: str, pr: PullRequest) -> str:
    context = (
        f"Repository: {repo}\n"
        f"Pull request: #{pr.number} ({pr.url})\n"
        f"HEAD SHA: {pr.head_sha}\n"
        "Review this exact HEAD. Do not modify files, push commits, or merge."
    )
    return f"{settings.prompt.rstrip()}\n\n{context}"


def reviewer_command(settings: Settings, prompt: str) -> list[str]:
    if settings.provider == "claude":
        return [
            "claude",
            "-p",
            prompt,
            "--model",
            settings.model,
            "--permission-mode",
            "plan",
        ]
    return [
        "codex",
        "exec",
        "--model",
        settings.model,
        "--sandbox",
        "read-only",
        "--ask-for-approval",
        "never",
        prompt,
    ]


def run_reviewer(settings: Settings, repo: str, pr: PullRequest) -> bool:
    """Wait for the configured reviewer CLI; success means a zero exit status."""
    command = reviewer_command(settings, build_prompt(settings, repo, pr))
    try:
        result = subprocess.run(command, stdin=subprocess.DEVNULL)
    except OSError as exc:
        log.error("cannot start %s: %s", command[0], exc)
        return False
    if result.returncode != 0:
        log.error("%s exited with status %d", command[0], result.returncode)
        return False
    return True


IDLE = "idle"
ACTIVE = "active"


class Watcher:
    """A single-threaded watcher with one adaptive timer for the repository."""

    def __init__(
        self,
        repo: str,
        profile: str | None,
        settings: Settings,
        state: State,
        list_prs: Callable[[str], list[PullRequest]] = gh_open_prs,
        reviewer: Callable[[Settings, str, PullRequest], bool] = run_reviewer,
        sleep: Callable[[float], None] = time.sleep,
    ):
        self.repo = repo
        self.profile = profile
        self.settings = settings
        self.state = state
        self.list_prs = list_prs
        self.reviewer = reviewer
        self.sleep = sleep
        self.mode = IDLE
        self.step = 0
        self.last_failed: ReviewKey | None = None

    def run_forever(self) -> None:
        while True:
            self.cycle()

    def cycle(self) -> None:
        """Review one waiting HEAD, or sleep once if there is no work."""
        try:
            prs = self.list_prs(self.repo)
        except GhError as exc:
            log.warning("cannot list open PRs: %s", exc)
            self._sleep_adaptive()
            return

        if not prs and self.mode == ACTIVE:
            self.mode = IDLE
            self.step = 0

        pr = self._next_reviewable(prs, self.state.reviewed())
        if pr is None:
            self._sleep_adaptive()
            return

        if self._review(pr):
            # The next cycle lists the repository immediately. The active timer
            # starts only if that fresh listing contains no reviewable HEAD.
            self.mode = ACTIVE
            self.step = 0
            return

        failed_key = self._key(pr)
        self.last_failed = failed_key
        log.warning("PR #%d remains eligible; retrying checks in 5 minutes", pr.number)
        self.sleep(FAILURE_WAIT)

    def _key(self, pr: PullRequest) -> ReviewKey:
        return ReviewKey(self.repo, pr.number, pr.head_sha, self.profile)

    def _next_reviewable(
        self, prs: list[PullRequest], done: set[ReviewKey]
    ) -> PullRequest | None:
        waiting = [
            pr
            for pr in sorted(prs, key=lambda item: item.number)
            if (self.settings.include_drafts or not pr.is_draft)
            and self._key(pr) not in done
        ]
        for pr in waiting:
            if self._key(pr) != self.last_failed:
                return pr
        return waiting[0] if waiting else None

    def _review(self, pr: PullRequest) -> bool:
        log.info("PR #%d (%s): review started", pr.number, pr.head_sha[:7])
        try:
            succeeded = self.reviewer(self.settings, self.repo, pr)
        except Exception:
            log.exception("PR #%d: reviewer crashed", pr.number)
            succeeded = False

        if not succeeded:
            log.warning("PR #%d: reviewer failed; HEAD was not recorded", pr.number)
            return False

        self.state.add(self._key(pr))
        self.last_failed = None
        log.info("PR #%d: review completed", pr.number)
        return True

    def _sleep_adaptive(self) -> None:
        schedule = self.settings.active if self.mode == ACTIVE else self.settings.idle
        delay = schedule[min(self.step, len(schedule) - 1)]
        log.info("no reviewable PR HEAD (%s); checking again in %s seconds", self.mode, delay)
        self.sleep(delay)
        self.step = min(self.step + 1, len(schedule) - 1)


def make_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="misscat",
        description="Watch a GitHub repository and run a review for each new open-PR HEAD.",
        epilog="profile is a name such as 'sol'; it loads ~/.config/misscat/sol.yml.",
    )
    parser.add_argument("repo", metavar="owner/repo")
    parser.add_argument("profile", metavar="profile", nargs="?")
    return parser


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    parser = make_parser()
    if not argv:
        parser.print_help()
        return 0
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        datefmt="%H:%M:%S",
    )
    try:
        if not REPO_RE.fullmatch(args.repo):
            raise MissCatError(f"repository must look like owner/repo: {args.repo!r}")

        settings = load_settings(args.profile)
        for executable in ("gh", settings.provider):
            if shutil.which(executable) is None:
                raise MissCatError(f"required CLI not found on PATH: {executable}")

        state = State(STATE_FILE)
        state.reviewed()  # fail early with a clear error for corrupt state
        log.info(
            "watching %s with %s/%s (profile: %s)",
            args.repo,
            settings.provider,
            settings.model,
            args.profile or "default",
        )
        Watcher(args.repo, args.profile, settings, state).run_forever()
    except MissCatError as exc:
        print(f"misscat: {exc}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        log.info("stopped")
        return 130
    return 0


if __name__ == "__main__":
    sys.exit(main())

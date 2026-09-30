#!/usr/bin/env python3
"""MissCat - never miss a single commit.

Watches a GitHub repository and runs an AI review once for every new PR HEAD.
Single-threaded: one review at a time, then an immediate re-check before sleeping.

Each review runs the reviewer CLI inside a MissCat-owned checkout of the exact PR HEAD;
your own working tree is never touched.

Local layout (repository names are canonicalized to lowercase)
  ~/.config/misscat/<profile>.yml          reviewer profiles
  ~/.config/misscat/<owner>__<repo>.json   reviewed HEADs of one repository (PR + HEAD + profile)
  ~/.cache/misscat/repos/<owner>/<repo>/   persistent review workspace

Requirements and limits
- Git must be able to authenticate to the reviewed repository. Existing Git/SSH auth is
  fine; HTTPS users relying on GitHub CLI should run `gh auth setup-git`. MissCat never
  stores Git credentials.
- Reviewer CLIs run inside a checkout of PR-controlled files: use MissCat only with
  repositories and pull requests whose contents you trust.
- Run at most one MissCat process per repository. Several profiles on the same repository
  would share and modify the same review workspace.
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import re
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, NamedTuple

import yaml

log = logging.getLogger("misscat")

CONFIG_DIR = Path.home() / ".config" / "misscat"
WORKSPACE_ROOT = Path.home() / ".cache" / "misscat" / "repos"
DEFAULT_CONFIG = Path(__file__).with_name("default.yml")
OWNER_RE = re.compile(r"^[A-Za-z0-9-]+$")  # no "_": keeps the "__" in state filenames unambiguous
NAME_RE = re.compile(r"^[A-Za-z0-9_.-]+$")
PROVIDERS = ("claude", "codex")
FAILURE_WAIT = 300.0  # seconds to wait after an unsuccessful reviewer before checking again


class MissCatError(Exception):
    """Errors that end the process with a clean message."""


class ConfigError(MissCatError):
    pass


class StateError(MissCatError):
    pass


class GhError(Exception):
    pass


class WorkspaceError(Exception):
    """Review preparation failed; handled like a failed reviewer."""


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


def profile_path(name: str) -> Path:
    """`sol` -> ~/.config/misscat/sol.yml. A profile is a name, never a path."""
    if not name or "/" in name or "\\" in name or ".." in name:
        raise ConfigError(f"invalid profile name {name!r}: use a plain name such as 'sol'")
    path = CONFIG_DIR / f"{name}.yml"
    if not path.is_file():
        raise ConfigError(f"profile {name!r} not found: {path}")
    return path


@dataclass(frozen=True)
class Settings:
    provider: str
    model: str
    prompt: str
    idle: tuple[float, ...]
    active: tuple[float, ...]
    include_drafts: bool


def _schedule(value, name: str) -> tuple[float, ...]:
    ok = isinstance(value, list) and value and all(
        isinstance(v, (int, float)) and not isinstance(v, bool) and v > 0 for v in value
    )
    if not ok:
        raise ConfigError(f"{name} must be a non-empty list of positive numbers")
    return tuple(float(v) for v in value)


def build_settings(cfg: dict) -> Settings:
    try:
        provider, model = cfg["reviewer"]["provider"], cfg["reviewer"]["model"]
        prompt, include_drafts = cfg["prompt"], cfg["review"]["include_drafts"]
        if provider not in PROVIDERS:
            raise ConfigError(f"reviewer.provider must be one of {', '.join(PROVIDERS)}")
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
            idle=_schedule(cfg["watch"]["idle"], "watch.idle"),
            active=_schedule(cfg["watch"]["active"], "watch.active"),
            include_drafts=include_drafts,
        )
    except (KeyError, TypeError) as exc:
        raise ConfigError(f"missing or malformed config section: {exc}") from exc


def load_settings(profile: str | None) -> Settings:
    cfg = _load_yaml(DEFAULT_CONFIG)
    if profile is not None:
        cfg = deep_merge(cfg, _load_yaml(profile_path(profile)))
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


# --------------------------------------------------------------------------- repository identity


def canonical_repo(repo: str) -> str:
    """Validate owner/repo and return the lowercase form used for all local identity.

    GitHub names are case-insensitive, so Genonfire/MissCat and genonfire/misscat must
    share one workspace and one state file on every platform.
    """
    owner, sep, name = repo.partition("/")
    if not sep or not OWNER_RE.match(owner) or not NAME_RE.match(name) or name in (".", ".."):
        raise MissCatError(f"repository must look like owner/repo: {repo!r}")
    return f"{owner}/{name}".lower()


def state_path(repo: str) -> Path:
    owner, name = canonical_repo(repo).split("/")
    return CONFIG_DIR / f"{owner}__{name}.json"


# --------------------------------------------------------------------------- workspace


def workspace_path(repo: str) -> Path:
    try:
        owner, name = canonical_repo(repo).split("/")
    except MissCatError as exc:
        raise WorkspaceError(str(exc)) from exc
    return WORKSPACE_ROOT / owner / name


def _run(cmd: list[str], cwd: Path | None = None) -> str:
    try:
        proc = subprocess.run(
            cmd, cwd=cwd, stdin=subprocess.DEVNULL, capture_output=True, text=True,
            env={**os.environ, "GIT_TERMINAL_PROMPT": "0"},  # fail instead of waiting for a password
        )
    except OSError as exc:
        raise WorkspaceError(f"{cmd[0]}: {exc}") from exc
    if proc.returncode != 0:
        raise WorkspaceError(f"{' '.join(cmd[:3])} failed: {proc.stderr.strip()[-300:]}")
    return proc.stdout.strip()


def _git(args: list[str], cwd: Path) -> str:
    return _run(["git", *args], cwd)


def _clone(repo: str, path: Path) -> None:
    _run(["gh", "repo", "clone", repo, str(path)])  # follows gh's configured protocol (https/ssh)


def _usable(path: Path) -> bool:
    if not (path / ".git").exists():
        return False
    try:
        _git(["rev-parse", "--git-dir"], path)
    except WorkspaceError:
        return False
    return True


def _remove(path: Path) -> None:
    if path.is_symlink() or path.is_file():
        path.unlink()
    else:
        shutil.rmtree(path)


def prepare_workspace(repo: str, pr: PR) -> Path:
    """Return a clean checkout of exactly pr.head, cloning only on first use."""
    path = workspace_path(repo)
    try:
        if path.exists() and not _usable(path):
            log.warning("%s is not a usable git repository, recreating it", path)
            _remove(path)
        if not path.exists():
            path.parent.mkdir(parents=True, exist_ok=True)
            log.info("cloning %s into %s", repo, path)
            try:
                _clone(repo, path)
            except BaseException:  # incl. Ctrl-C: never leave a half-cloned workspace behind
                shutil.rmtree(path, ignore_errors=True)
                raise
        _git(["fetch", "origin", f"refs/pull/{pr.number}/head"], path)  # works for fork PRs
        _git(["reset", "--hard"], path)
        _git(["clean", "-ffdx"], path)  # drop leftovers of the previous reviewer, ignored files too
        _git(["checkout", "--detach", "FETCH_HEAD"], path)
        head = _git(["rev-parse", "HEAD"], path)
    except OSError as exc:
        raise WorkspaceError(str(exc)) from exc
    if head != pr.head:
        raise WorkspaceError(
            f"PR #{pr.number} changed during preparation ({pr.head[:7]} -> {head[:7]}); "
            "the next poll will pick up the new HEAD"
        )
    return path


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
        "The current directory is a checkout of exactly this HEAD.\n"
        "Review exactly this HEAD. Do not modify code, push commits, or merge."
    )
    return f"{settings.prompt.strip()}\n\n{context}"


def run_cli_reviewer(settings: Settings, repo: str, pr: PR) -> bool:
    """Prepare the workspace, run the reviewer CLI in it and wait. True only on a clean exit."""
    try:
        workspace = prepare_workspace(repo, pr)
    except WorkspaceError as exc:
        log.error("PR #%d: workspace preparation failed: %s", pr.number, exc)
        return False
    cmd = COMMANDS[settings.provider](settings.model, build_prompt(settings, repo, pr))
    try:
        proc = subprocess.run(
            cmd, cwd=workspace, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT, text=True,
        )
    except OSError as exc:
        log.error("cannot start %s: %s", cmd[0], exc)
        return False
    if proc.returncode != 0:
        log.error("%s exited %d: %s", cmd[0], proc.returncode, (proc.stdout or "").strip()[-500:])
        return False
    return True


# --------------------------------------------------------------------------- state


class Key(NamedTuple):
    pr: int
    head: str
    profile: str | None  # None = bundled default, kept distinct from a profile named "default"


class State:
    """Successfully reviewed HEADs of one repository (~/.config/misscat/<owner>__<repo>.json).

    The repository is identified by the filename, so a record is PR + HEAD + profile.
    Only completions are stored: a HEAD that is absent (failed, interrupted, never run) is
    eligible for review. Loaded once at start; every successful review rewrites the file
    atomically. One repository = one MissCat process = one state file, so there is no
    locking and no merging.
    """

    def __init__(self, path: Path):
        self.path = path
        self._reviewed = self._load()

    def _load(self) -> set[Key]:
        if not self.path.exists():
            return set()
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
            return {Key(int(r["pr"]), r["head"], r["profile"]) for r in raw["reviewed"]}
        except (OSError, ValueError, KeyError, TypeError) as exc:
            raise StateError(
                f"unreadable state file {self.path} ({exc}); fix or delete it to continue"
            ) from exc

    def reviewed(self) -> set[Key]:
        return self._reviewed

    def add(self, key: Key) -> None:
        self._reviewed.add(key)
        rows = sorted(self._reviewed, key=lambda k: (k.pr, k.head, k.profile or ""))
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_name(self.path.name + ".tmp")
        tmp.write_text(
            json.dumps({"version": 1, "reviewed": [k._asdict() for k in rows]}, indent=2),
            encoding="utf-8",
        )
        os.replace(tmp, self.path)


# --------------------------------------------------------------------------- watcher

IDLE, ACTIVE = "idle", "active"


def _fmt(seconds: float) -> str:
    seconds = int(round(seconds))
    return f"{seconds // 60}m" if seconds >= 60 and seconds % 60 == 0 else f"{seconds}s"


class Watcher:
    """One watcher, one polling timer per repository.

    Timer: starts idle (1m -> 5m). A successful review switches it to active (5m -> 1m)
    from the start of the sequence. When no PR is open any more it falls back to idle.
    """

    def __init__(
        self,
        repo: str,
        profile: str | None,
        settings: Settings,
        state: State,
        list_prs: Callable[[str], list[PR]],
        reviewer: Callable[[Settings, str, PR], bool],
        sleep: Callable[[float], None] = time.sleep,
    ):
        self.repo, self.profile, self.s, self.state = repo, profile, settings, state
        self.list_prs, self.reviewer, self.sleep = list_prs, reviewer, sleep
        self.mode, self.step = IDLE, 0
        self.last_failed: Key | None = None

    def run_forever(self) -> None:
        while True:
            self.cycle()

    def cycle(self) -> None:
        """One iteration: review one PR HEAD if any is waiting, otherwise sleep."""
        try:
            prs = self.list_prs(self.repo)
        except GhError as exc:
            log.warning("cannot list PRs: %s", exc)
            self._sleep_adaptive()
            return
        if not prs and self.mode == ACTIVE:
            self.mode, self.step = IDLE, 0
        pr = self._next_reviewable(prs, self.state.reviewed())
        if pr is None:
            self._sleep_adaptive()
        elif self._review(pr):
            self.mode, self.step = ACTIVE, 0  # then straight back to a fresh listing
        else:
            log.warning("checking again in %s", _fmt(FAILURE_WAIT))
            self.sleep(FAILURE_WAIT)

    def _key(self, pr: PR) -> Key:
        return Key(pr.number, pr.head, self.profile)

    def _next_reviewable(self, prs: list[PR], done: set[Key]) -> PR | None:
        waiting = [
            p for p in sorted(prs, key=lambda p: p.number)  # oldest PR first
            if (self.s.include_drafts or not p.draft) and self._key(p) not in done
        ]
        for pr in waiting:  # the PR that just failed goes last so it cannot starve the others
            if self._key(pr) != self.last_failed:
                return pr
        return waiting[0] if waiting else None

    def _review(self, pr: PR) -> bool:
        log.info("PR #%d: review start (%s)", pr.number, pr.head[:7])
        try:
            ok = self.reviewer(self.s, self.repo, pr)
        except Exception:
            log.exception("PR #%d: reviewer crashed", pr.number)
            ok = False
        if ok:
            self.state.add(self._key(pr))  # only a successful review is recorded
            self.last_failed = None
            log.info("PR #%d: review done", pr.number)
        else:
            self.last_failed = self._key(pr)
            log.warning("PR #%d: review failed, HEAD stays eligible", pr.number)
        return ok

    def _sleep_adaptive(self) -> None:
        seq = self.s.active if self.mode == ACTIVE else self.s.idle
        delay = seq[min(self.step, len(seq) - 1)]
        log.info("nothing to review (%s), next check in %s", self.mode, _fmt(delay))
        self.sleep(delay)
        self.step = min(self.step + 1, len(seq) - 1)


# --------------------------------------------------------------------------- CLI


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    parser = argparse.ArgumentParser(
        prog="misscat",
        description="Watch a GitHub repository and run an AI review on every new PR HEAD.",
        epilog=(
            "profile is a name: 'sol' loads ~/.config/misscat/sol.yml over the defaults.\n\n"
            "Requirements and limits:\n"
            "  - Git must authenticate to the repository (Git/SSH auth, or run\n"
            "    `gh auth setup-git` for HTTPS). MissCat stores no credentials.\n"
            "  - Reviewers run inside a checkout of PR-controlled files: use MissCat only\n"
            "    with repositories and pull requests you trust.\n"
            "  - Run at most one MissCat process per repository."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("repo", metavar="owner/repo")
    parser.add_argument("profile", metavar="profile", nargs="?")
    if not argv:
        parser.print_help()
        return 2
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s",
                        datefmt="%H:%M:%S")
    try:
        repo = canonical_repo(args.repo)  # canonicalize once; everything below uses this form
        settings = load_settings(args.profile)
        for tool in ("git", "gh", settings.provider):
            if shutil.which(tool) is None:
                raise MissCatError(f"required CLI not found on PATH: {tool}")
        state = State(state_path(repo))  # fails early on a corrupt state file
        log.info("watching %s with %s/%s (profile: %s)", repo, settings.provider,
                 settings.model, args.profile or "default")
        Watcher(repo, args.profile, settings, state, gh_open_prs, run_cli_reviewer).run_forever()
    except MissCatError as exc:
        print(f"misscat: {exc}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        log.info("stopped")
        return 130
    return 0


if __name__ == "__main__":
    sys.exit(main())

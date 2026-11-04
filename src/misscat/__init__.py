#!/usr/bin/env python3
"""MissCat - never miss a single commit.

Watches a GitHub repository and runs an AI review once for every new PR HEAD.
One repository = one MissCat process = one workspace = one state file.
One review at a time by default. `--cat=N` runs up to N independent cats at once, one
persistent worktree each: a finished review is recorded immediately, and idle cats take new
PR HEADs only at the next regular poll.

Each review runs the reviewer CLI (Claude Code, Codex, Gemini via Antigravity CLI, or OpenCode)
from the root of a MissCat-owned worktree of the exact PR HEAD, so the CLI finds the
repository's own instruction files (REVIEW.md, CLAUDE.md, AGENTS.md) by itself. MissCat
never parses them and never touches your own working tree.

Local layout (repository names are canonicalized to lowercase)
  ~/.config/misscat/<profile>.yml          reviewer profiles (bundled ones installed by `misscat init`)
  ~/.config/misscat/<owner>__<repo>.json   reviewed HEADs of one repository (PR + HEAD)
  ~/.cache/misscat/repos/<owner>/<repo>/   control clone (default branch, never reviewed in)
  ~/.cache/misscat/cats/<owner>/<repo>/cat-N/   persistent review worktrees

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
from contextlib import contextmanager
from datetime import datetime, timezone
import importlib.resources
import tempfile
import threading
import json
import logging
import os
import queue
import re
import secrets
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Callable, NamedTuple
from urllib.parse import urlsplit

import yaml

__version__ = "1.8.0"

log = logging.getLogger("misscat")
# Reviewer tool activity (exec/MCP/tool/web search). Successful lines show only with --loud;
# warnings and errors always show. Configured once in _configure_logging(), so cat threads agree.
activity_log = logging.getLogger("misscat.activity")

CONFIG_DIR = Path.home() / ".config" / "misscat"
WORKSPACE_ROOT = Path.home() / ".cache" / "misscat" / "repos"  # control clones
CAT_ROOT = Path.home() / ".cache" / "misscat" / "cats"  # persistent cat-N worktrees
LOCK_ROOT = Path.home() / ".cache" / "misscat" / "locks"  # outside CONFIG_DIR: locking must not
# create it, or a state-only command would make the first watcher run skip profile initialization
OWNER_RE = re.compile(r"^[A-Za-z0-9-]+$")  # no "_": keeps the "__" in state filenames unambiguous
NAME_RE = re.compile(r"^[A-Za-z0-9_.-]+$")
PROVIDERS = ("claude", "codex", "gemini", "opencode")
EXECUTABLES = {"claude": "claude", "codex": "codex", "gemini": "agy", "opencode": "opencode"}  # provider -> CLI binary
FAILURE_WAIT = 300.0  # seconds to wait after a failed review before checking again


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
        raise ConfigError(
            f"profile {name!r} not found: {path} (run `misscat init` to install bundled profiles)")
    return path


@dataclass(frozen=True)
class Settings:
    provider: str
    model: str
    prompt: str
    idle: tuple[float, ...]
    active: tuple[float, ...]
    include_drafts: bool
    args: tuple[str, ...] = ()  # reviewer.args: extra reviewer CLI options, passed through untouched


def _schedule(value, name: str) -> tuple[float, ...]:
    ok = isinstance(value, list) and value and all(
        isinstance(v, (int, float)) and not isinstance(v, bool) and v > 0 for v in value
    )
    if not ok:
        raise ConfigError(f"{name} must be a non-empty list of positive numbers")
    return tuple(float(v) for v in value)


def build_settings(cfg: dict, providers: tuple[str, ...] = PROVIDERS) -> Settings:
    try:
        reviewer = cfg["reviewer"]
        provider, model = reviewer["provider"], reviewer["model"]
        args = reviewer["args"]
        prompt, include_drafts = cfg["prompt"], cfg["review"]["include_drafts"]
        idle, active = cfg["watch"]["idle"], cfg["watch"]["active"]
    except (KeyError, TypeError, AttributeError) as exc:
        raise ConfigError(f"missing or malformed config section: {exc}") from exc
    if provider not in providers:
        raise ConfigError(f"reviewer.provider must be one of {', '.join(providers)}")
    if not isinstance(model, str) or not model.strip():
        raise ConfigError("reviewer.model must be a non-empty string")
    if not isinstance(args, list) or not all(isinstance(a, str) and a for a in args):
        raise ConfigError("reviewer.args must be a list of non-empty strings (quote numbers)")
    if not isinstance(prompt, str) or not prompt.strip():
        raise ConfigError("prompt must be a non-empty string")
    if not isinstance(include_drafts, bool):
        raise ConfigError("review.include_drafts must be true or false")
    return Settings(
        provider=provider,
        model=model.strip(),
        prompt=prompt,
        idle=_schedule(idle, "watch.idle"),
        active=_schedule(active, "watch.active"),
        include_drafts=include_drafts,
        args=tuple(args),
    )


def _load_default_config() -> dict:
    try:
        content = importlib.resources.files("misscat").joinpath("default.yml").read_text(encoding="utf-8")
        data = yaml.safe_load(content)
    except (OSError, yaml.YAMLError, TypeError) as exc:
        raise ConfigError(f"cannot read bundled default.yml: {exc}") from exc
    if data is None:
        return {}
    if not isinstance(data, dict):
        raise ConfigError("bundled default.yml: top level must be a mapping")
    return data


def load_settings(profile: str | None) -> Settings:
    cfg = _load_default_config()
    if profile is not None:
        cfg = deep_merge(cfg, _load_yaml(profile_path(profile)))
    return build_settings(cfg)


# --------------------------------------------------------------------------- bundled profiles


def bundled_profiles() -> dict[str, str]:
    """Bundled reviewer profiles (`profiles/*.yml` in the package), name -> content.

    `default.yml` is the internal base configuration and is never part of this set.
    """
    try:
        root = importlib.resources.files("misscat").joinpath("profiles")
        return {
            item.name: item.read_text(encoding="utf-8")
            for item in sorted(root.iterdir(), key=lambda i: i.name)
            if item.name.endswith(".yml")
        }
    except (OSError, TypeError) as exc:
        raise ConfigError(f"cannot read bundled profiles: {exc}") from exc


def _write_atomic(path: Path, content: str, overwrite: bool = True) -> bool:
    """Write via a temporary file in the same directory: no half-written YAML.

    Without `overwrite` the file is linked into place, which fails if `path` already exists
    (even when created concurrently); returns False in that case, True once written.
    """
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as handle:
            handle.write(content)
        if overwrite:
            os.replace(tmp, path)
        else:
            try:
                os.link(tmp, path)
            except FileExistsError:
                return False
        return True
    finally:
        try:
            os.unlink(tmp)
        except OSError:
            pass


def install_profiles(force: bool = False) -> tuple[list[str], list[str]]:
    """Copy bundled profiles into CONFIG_DIR; return (installed, skipped).

    Existing files are kept unless `force`. Other files in CONFIG_DIR are never touched.
    """
    profiles = bundled_profiles()
    installed: list[str] = []
    skipped: list[str] = []
    try:
        CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        for name, content in profiles.items():
            target = CONFIG_DIR / name
            if _write_atomic(target, content, overwrite=force):
                installed.append(name)
            else:
                skipped.append(name)
    except OSError as exc:
        raise ConfigError(
            f"cannot install bundled profiles into {CONFIG_DIR}: {exc}. "
            "Fix the problem, then run `misscat init` to restore missing profiles."
        ) from exc
    return installed, skipped


def ensure_initial_profiles() -> None:
    """First normal run only: if CONFIG_DIR does not exist, create it and copy all profiles."""
    if CONFIG_DIR.exists():
        return
    installed, _ = install_profiles()
    log.info("first run: installed profiles into %s: %s", CONFIG_DIR, ", ".join(installed) or "none")


def run_init(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(
        prog="misscat init",
        description="Copy the bundled reviewer profiles into ~/.config/misscat/. "
        "Only missing profiles are created; existing files are left untouched.",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="overwrite files named like bundled profiles with fresh copies "
        "(other profiles are kept)",
    )
    args = parser.parse_args(argv)
    try:
        installed, skipped = install_profiles(force=args.force)
    except MissCatError as exc:
        print(f"misscat: {exc}", file=sys.stderr)
        return 2
    verb = "overwrote/created" if args.force else "installed"
    print(f"{verb}: {', '.join(installed) or 'none'}")
    if skipped:
        print(f"skipped (already exist): {', '.join(skipped)}")
    print(f"profiles directory: {CONFIG_DIR}")
    return 0


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


SCP_REMOTE_RE = re.compile(r"^(?:[^@/:]+@)?github\.com:(?P<path>[^/].*)$", re.IGNORECASE)


def parse_github_remote(url: str) -> str:
    """Return owner/repo for a github.com remote URL; never echo the URL (may hold credentials)."""
    url = url.strip()
    path = None
    match = SCP_REMOTE_RE.match(url)
    if match and "://" not in url:
        path = match.group("path")
    elif "://" in url:
        try:
            parts = urlsplit(url)
            host = (parts.hostname or "").lower()
        except ValueError:
            host, parts = "", None
        if parts and parts.scheme in ("https", "ssh", "git") and host == "github.com":
            path = parts.path.lstrip("/")
    if path is None:
        raise MissCatError("origin remote is not a github.com repository")
    path = path.rstrip("/")
    if path.endswith(".git"):
        path = path[: -len(".git")]
    try:
        return canonical_repo(path)
    except MissCatError:
        raise MissCatError("origin remote is not a valid github.com owner/repo") from None


def _local_git(*args: str) -> subprocess.CompletedProcess:
    try:
        return subprocess.run(["git", *args], capture_output=True, text=True, check=False)
    except OSError:
        raise MissCatError("git is not available; cannot resolve '.'") from None


def resolve_repo_arg(arg: str) -> str:
    """Canonical owner/repo for a CLI argument; '.' always means this checkout's origin remote.

    Runs before any State, config, lock or workspace access.
    """
    if arg != ".":
        return canonical_repo(arg)
    if _local_git("rev-parse", "--is-inside-work-tree").returncode != 0:
        raise MissCatError("'.' requires running inside a Git repository")
    result = _local_git("remote", "get-url", "origin")
    if result.returncode != 0 or not result.stdout.strip():
        raise MissCatError("'.' requires an 'origin' remote in the current Git repository")
    repo = parse_github_remote(result.stdout)
    print(f"resolved . -> {repo}")
    return repo


def state_path(repo: str) -> Path:
    owner, name = canonical_repo(repo).split("/")
    return CONFIG_DIR / f"{owner}__{name}.json"


def workspace_path(repo: str) -> Path:
    try:
        owner, name = canonical_repo(repo).split("/")
    except MissCatError as exc:
        raise WorkspaceError(str(exc)) from exc
    return WORKSPACE_ROOT / owner / name


# --------------------------------------------------------------------------- GitHub


@dataclass(frozen=True)
class PR:
    number: int
    head: str
    draft: bool
    url: str
    title: str
    urgent: bool = False
    no_review: bool = False
    author: str = ""  # PR author login; empty = unknown, which disables same-HEAD reply triggers
    updated_at: str = ""  # cheap change cursor from the PR listing
    reply: "ReplyTrigger | None" = None  # set only on a same-HEAD re-review picked by the watcher
    run_id: str = ""  # per-run nonce the reviewer must echo in its review (set by the watcher)


URGENT_LABEL = "urgent"
NO_REVIEW_LABEL = "no-review"


def gh_open_prs(repo: str) -> list[PR]:
    cmd = [
        "gh", "pr", "list", "--repo", repo, "--state", "open", "--limit", "200",
        "--json", "number,headRefOid,isDraft,url,title,labels,author,updatedAt",
    ]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise GhError(str(exc)) from exc
    if proc.returncode != 0:
        raise GhError(proc.stderr.strip() or f"gh exited {proc.returncode}")
    try:
        prs = []
        for d in json.loads(proc.stdout):
            names = {label["name"] for label in d.get("labels") or []}
            prs.append(PR(int(d["number"]), d["headRefOid"], bool(d["isDraft"]), d["url"], d["title"],
                          URGENT_LABEL in names, NO_REVIEW_LABEL in names,
                          str((d.get("author") or {}).get("login") or ""),
                          str(d.get("updatedAt") or "")))
        return prs
    except (ValueError, KeyError, TypeError) as exc:
        raise GhError(f"unexpected gh output: {exc}") from exc


# --------------------------------------------------------------------------- same-HEAD replies

MAX_PAGES = 20
RUN_PREFIX = "MissCat-Run:"
_MARKER_RE = re.compile(r"\A(\+1|\+2|-1)\r?\nHEAD: ([0-9a-f]{40})\r?\n(.*)\Z", re.DOTALL)


@dataclass(frozen=True)
class GhReview:
    id: int
    login: str
    state: str
    body: str
    commit_id: str
    submitted_at: str


@dataclass(frozen=True)
class GhComment:
    """A top-level PR conversation (issue) comment; never a review or an inline comment."""
    id: int
    login: str
    created_at: str
    url: str


@dataclass(frozen=True)
class ReplyTrigger:
    """Why an already-reviewed HEAD is reviewed again: the PR author answered a `-1`."""
    comment_ids: tuple[int, ...]  # unhandled author comments after the blocking review, oldest first
    comment_urls: tuple[str, ...]
    blocking_review_id: int


class GhDiscussion:
    """Reads PR reviews, conversation comments and the current HEAD through `gh api`."""

    def __init__(self, repo: str):
        self.repo = repo

    @staticmethod
    def _api(path: str):
        try:
            proc = subprocess.run(["gh", "api", path], capture_output=True, text=True, timeout=60,
                                  stdin=subprocess.DEVNULL)
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise GhError(str(exc)) from exc
        if proc.returncode != 0:
            raise GhError(proc.stderr.strip() or f"gh exited {proc.returncode}")
        try:
            return json.loads(proc.stdout)
        except ValueError as exc:
            raise GhError(f"unexpected gh output: {exc}") from exc

    def _pages(self, path: str) -> list:
        items: list = []
        for page in range(1, MAX_PAGES + 1):
            chunk = self._api(f"{path}?per_page=100&page={page}")
            if not isinstance(chunk, list):
                raise GhError("unexpected gh output: expected a list")
            items.extend(chunk)
            if len(chunk) < 100:
                return items
        raise GhError(f"too many pages for {path}")  # fail closed rather than truncate

    def reviews(self, number: int) -> list[GhReview]:
        try:
            return [GhReview(int(d["id"]), str((d.get("user") or {}).get("login") or ""),
                             str(d.get("state") or ""), str(d.get("body") or ""),
                             str(d.get("commit_id") or ""), str(d.get("submitted_at") or ""))
                    for d in self._pages(f"repos/{self.repo}/pulls/{number}/reviews")]
        except (KeyError, TypeError, ValueError, AttributeError) as exc:
            raise GhError(f"unexpected gh output: {exc}") from exc

    def comments(self, number: int) -> list[GhComment]:
        try:
            return [GhComment(int(d["id"]), str((d.get("user") or {}).get("login") or ""),
                              str(d["created_at"]), str(d.get("html_url") or ""))
                    for d in self._pages(f"repos/{self.repo}/issues/{number}/comments")]
        except (KeyError, TypeError, ValueError, AttributeError) as exc:
            raise GhError(f"unexpected gh output: {exc}") from exc

    def head(self, number: int) -> str:
        try:
            return str(self._api(f"repos/{self.repo}/pulls/{number}")["head"]["sha"])
        except (KeyError, TypeError) as exc:
            raise GhError(f"unexpected gh output: {exc}") from exc


def marker_verdict(review: GhReview, head: str) -> str | None:
    """`+1`/`+2`/`-1` of a submitted COMMENTED review that is a valid marker for exactly `head`."""
    if review.state != "COMMENTED" or not review.submitted_at:
        return None
    match = _MARKER_RE.match(review.body)
    evidence = "\n".join(line for line in match.group(3).splitlines()
                         if not line.strip().startswith(RUN_PREFIX)) if match else ""
    if not match or not evidence.strip():  # the run id line alone is not evidence
        return None
    if match.group(2) != head or review.commit_id != head:
        return None
    return match.group(1)


def run_line(run_id: str) -> str:
    return f"{RUN_PREFIX} {run_id}"


def submitted_by_run(reviews: list[GhReview], head: str, run_id: str) -> list[GhReview]:
    """Valid verdict reviews of exactly `head` that carry this run's nonce line.

    The nonce is generated per run and given only to that run's reviewer, so neither another
    stage, a manual review nor another run on the same GitHub account can be mistaken for it.
    """
    if not run_id:
        return []
    return [r for r in reviews if marker_verdict(r, head) is not None
            and run_line(run_id) in (line.strip() for line in r.body.splitlines())]


def blocking_review(reviews: list[GhReview], head: str) -> GhReview | None:
    """The latest substantive marker review of `head` if it is a `-1`, else None.

    Only submitted COMMENTED reviews count (not issue comments, inline comments, APPROVED or
    CHANGES_REQUESTED, as in BadCat's protocol). A marker is
    `+1|+2|-1`, then `HEAD: <sha>` equal to the review's commit and `head`, then evidence.
    A later marker review on the same HEAD (a pass, or a newer `-1`) supersedes earlier ones.
    """
    latest = None
    submitted = [r for r in reviews if r.submitted_at]
    for review in sorted(submitted, key=lambda r: (_instant(r.submitted_at), r.id)):
        verdict = marker_verdict(review, head)
        if verdict is not None:
            latest = review if verdict == "-1" else None
    return latest



# --------------------------------------------------------------------------- workspace


def _run(cmd: list[str], cwd: Path | None = None, env: dict[str, str] | None = None) -> str:
    try:
        proc = subprocess.run(
            cmd, cwd=cwd, stdin=subprocess.DEVNULL, capture_output=True, text=True,
            # fail instead of waiting for a password
            env={**os.environ, "GIT_TERMINAL_PROMPT": "0", **(env or {})},
        )
    except OSError as exc:
        raise WorkspaceError(f"{cmd[0]}: {exc}") from exc
    if proc.returncode != 0:
        raise WorkspaceError(_redact(f"{' '.join(cmd[:3])} failed: {proc.stderr.strip()[-300:]}"))
    return proc.stdout.strip()


_USERINFO_RE = re.compile(r"(?i)\b([a-z][a-z0-9+.-]*://)[^/@\s]*@")


def _redact(text: str) -> str:
    """Drop user:token@ from URLs (an HTTPS remote may carry credentials) before logging."""
    return _USERINFO_RE.sub(r"\1", text)


def _git(args: list[str], cwd: Path) -> str:
    return _run(["git", *args], cwd, {"LC_ALL": "C"})  # untranslated errors, see _MISSING_REF


# git's error when the requested ref does not exist on the remote (not auth/network/other)
_MISSING_REF = "couldn't find remote ref"


def _sibling_url(origin: str, repo: str, other: str) -> str | None:
    """`origin`'s fetch URL with the `repo` path swapped for `other` (same host and protocol).

    None when the URL does not end in `repo`, so a fork is never fetched from a guessed host.
    """
    base = origin.rstrip("/")
    suffix = ".git" if base.endswith(".git") else ""
    base = base[: len(base) - len(suffix)]
    if len(base) <= len(repo) or base[-len(repo) - 1] not in "/:" or base[-len(repo):].lower() != repo.lower():
        return None
    return base[: -len(repo)] + other + suffix


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


def cat_root(repo: str) -> Path:
    """Directory holding the persistent cat worktrees of one repository."""
    try:
        owner, name = canonical_repo(repo).split("/")
    except MissCatError as exc:
        raise WorkspaceError(str(exc)) from exc
    return CAT_ROOT / owner / name


class Workspaces:
    """The control clone plus persistent `cat-N` worktrees of one repository.

    The control clone is never used by a reviewer: it only fetches and stays on the default
    branch HEAD. Each cat is a detached worktree that is cleaned and moved to the exact PR
    HEAD before a review. Only the main MissCat process touches this shared Git metadata,
    one step at a time; reviewers run solely inside their own cat worktree.
    """

    def __init__(self, repo: str, cats: int = 1):
        self.repo, self.cats = repo, cats
        self.control = workspace_path(repo)
        self.root = cat_root(repo)

    def cat_path(self, cat: int) -> Path:
        return self.root / f"cat-{cat}"

    def sync(self, skip: frozenset[int] | set[int] = frozenset()) -> None:
        """Ensure the control clone and cat-1..cat-N exist; leave control on the default HEAD.

        Only missing pieces are created. Cats beyond `cats` stay on disk untouched, and so
        do the cats in `skip` (running reviews): their worktrees are never inspected.
        """
        path = self.control
        try:
            if path.exists() and not _usable(path):
                log.warning("%s is not a usable git repository, recreating it", path)
                _remove(path)
            if not path.exists():
                path.parent.mkdir(parents=True, exist_ok=True)
                log.info("cloning %s into %s", self.repo, path)
                try:
                    _clone(self.repo, path)
                except BaseException:  # incl. Ctrl-C: never leave a half-cloned workspace behind
                    shutil.rmtree(path, ignore_errors=True)
                    raise
            _git(["fetch", "origin"], path)
            _git(["remote", "set-head", "origin", "--auto"], path)
            remote_head = _git(["symbolic-ref", "--short", "refs/remotes/origin/HEAD"], path)
            _git(["reset", "--hard"], path)
            _git(["clean", "-ffdx"], path)
            _git(["checkout", "-f", "-B", remote_head.split("/", 1)[1], remote_head], path)
            for cat in range(1, self.cats + 1):
                if cat not in skip:
                    self._ensure_cat(cat)
        except OSError as exc:
            raise WorkspaceError(str(exc)) from exc

    def _ensure_cat(self, cat: int) -> Path:
        path = self.cat_path(cat)
        if path.exists() and not _usable(path):
            log.warning("%s is not a usable worktree, recreating it", path)
            _remove(path)
        if not path.exists():
            path.parent.mkdir(parents=True, exist_ok=True)
            _git(["worktree", "prune"], self.control)
            log.info("adding worktree cat-%d at %s", cat, path)
            _git(["worktree", "add", "--force", "--detach", str(path)], self.control)
        return path

    def prepare(self, pr: PR, cat: int) -> Path:
        """Return cat-N cleaned and detached at exactly pr.head."""
        try:
            try:
                _git(["fetch", "origin", f"refs/pull/{pr.number}/head"], self.control)  # fork PRs too
            except WorkspaceError as exc:
                if _MISSING_REF not in str(exc):
                    raise
                self._fetch_source(pr)
            fetched = _git(["rev-parse", "FETCH_HEAD^{commit}"], self.control)
            self._check_head(pr, fetched)
            path = self._ensure_cat(cat)
            _git(["reset", "--hard"], path)
            _git(["clean", "-ffdx"], path)  # drop leftovers of the previous reviewer, ignored files too
            _git(["checkout", "--detach", fetched], path)
            self._check_head(pr, _git(["rev-parse", "HEAD"], path))
        except OSError as exc:
            raise WorkspaceError(str(exc)) from exc
        return path

    def _fetch_source(self, pr: PR) -> None:
        """Fetch the PR's source branch into FETCH_HEAD when GitHub has no refs/pull/N/head.

        Runs only after that ref was reported missing: one PR metadata lookup, then the exact
        branch from origin (same repository) or from the fork's URL, never a new remote.
        """
        failed = f"refs/pull/{pr.number}/head missing; source branch fallback failed"
        try:
            data = GhDiscussion._api(f"repos/{self.repo}/pulls/{pr.number}")
            head = data["head"]
            sha, ref = str(head["sha"]), str(head["ref"])
            source = str((head.get("repo") or {}).get("full_name") or "")
        except (GhError, KeyError, TypeError, AttributeError) as exc:
            raise WorkspaceError(f"{failed}: PR metadata unavailable: {exc}") from exc
        self._check_head(pr, sha)
        try:
            branch = _git(["check-ref-format", "--normalize", f"refs/heads/{ref}"], self.control)
            if canonical_repo(source) == canonical_repo(self.repo):  # a deleted fork has no name
                remote = "origin"
            else:
                remote = _sibling_url(_git(["remote", "get-url", "origin"], self.control), self.repo, source)
                if remote is None:
                    raise WorkspaceError(f"cannot derive the URL of {source} from origin")
            _git(["fetch", remote, branch], self.control)
        except (WorkspaceError, MissCatError) as exc:
            raise WorkspaceError(f"{failed}: {source}:{ref}: {exc}") from exc
        log.info("PR #%d: refs/pull/%d/head missing, fetched %s:%s instead",
                 pr.number, pr.number, source, ref)

    @staticmethod
    def _check_head(pr: PR, head: str) -> None:
        if head != pr.head:
            raise WorkspaceError(
                f"PR #{pr.number} changed during preparation ({pr.head[:7]} -> {head[:7]}); "
                "the next poll will pick up the new HEAD"
            )


# --------------------------------------------------------------------------- reviewer

# reviewer.args go before the prompt so a variadic option such as
# `--allowedTools A B` cannot swallow it. Structured output flags are
# MissCat's internal transport: profiles do not need to specify them.
COMMANDS: dict[str, Callable[[str, str, list[str]], list[str]]] = {
    "claude": lambda model, prompt, args: [
        "claude", *args, "-p", prompt, "--model", model,
        "--output-format", "stream-json", "--verbose",
    ],
    "codex": lambda model, prompt, args: [
        "codex", "exec", "--json", *args, "--model", model, prompt,
    ],
    # agy (Antigravity CLI) treats everything after `-p <prompt>` as prompt text, so every
    # flag, --model included, must come before it. Plain text output: its JSON event schema
    # is not relied on.
    "gemini": lambda model, prompt, args: ["agy", *args, "--model", model, "-p", prompt],
    # OpenCode native V2: a private per-run server and a fresh session (no --continue); the
    # model is `provider/model[#variant]`, passed unchanged.
    "opencode": lambda model, prompt, args: [
        "opencode", "run", "--standalone", "--format", "json", *args, "--model", model, prompt,
    ],
}


def _short(value, limit: int = 120) -> str:
    """Collapse a value to one short log line."""
    text = " ".join(str(value or "").split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _mcp_name(item: dict) -> str:
    server = item.get("server") or item.get("server_name")
    tool = item.get("tool") or item.get("tool_name") or item.get("name")
    if server and tool:
        return f"{server}.{tool}"
    return str(tool or server or "mcp")


def _log_codex_event(event: dict) -> None:
    """Log progress from `codex exec --json` without dumping tool output."""
    kind = event.get("type")

    if kind == "turn.failed":
        error = event.get("error") or {}
        log.error("reviewer: %s", _short(error.get("message") or "turn failed"))
        return
    if kind == "error":
        log.error("reviewer: %s", _short(event.get("message") or event.get("error") or "error"))
        return
    if kind == "turn.completed":
        usage = event.get("usage") or {}
        if usage:
            log.info(
                "reviewer: turn done (input=%s, output=%s)",
                usage.get("input_tokens", "?"),
                usage.get("output_tokens", "?"),
            )
        else:
            log.info("reviewer: turn done")
        return

    if kind not in ("item.started", "item.completed"):
        return

    item = event.get("item") or {}
    item_type = item.get("type")

    if kind == "item.started":
        if item_type == "command_execution":
            activity_log.info("exec: %s", _short(item.get("command") or "command"))
        elif item_type == "mcp_tool_call":
            activity_log.info("mcp: %s started", _mcp_name(item))
        elif item_type == "web_search":
            query = _short(item.get("query"))
            activity_log.info("web search: %s", query or "started")
        return

    if item_type == "command_execution":
        status = item.get("status") or "completed"
        exit_code = item.get("exit_code")
        suffix = f" ({exit_code})" if exit_code is not None else ""
        if status == "failed" or (isinstance(exit_code, int) and exit_code != 0):
            activity_log.warning("exec: %s%s", status, suffix)
        else:
            activity_log.info("exec: %s%s", status, suffix)
    elif item_type == "mcp_tool_call":
        status = item.get("status") or "completed"
        if status == "failed":
            activity_log.warning("mcp: %s failed", _mcp_name(item))
        else:
            activity_log.info("mcp: %s %s", _mcp_name(item), status)
    elif item_type == "web_search":
        activity_log.info("web search: completed")


def _claude_tool_label(name: str, tool_input) -> str:
    if not isinstance(tool_input, dict):
        return name
    detail = (
        tool_input.get("command")
        or tool_input.get("file_path")
        or tool_input.get("path")
        or tool_input.get("query")
        or tool_input.get("pattern")
    )
    return f"{name}: {_short(detail)}" if detail else name


def _log_claude_event(event: dict, tool_names: dict[str, str]) -> None:
    """Log progress from Claude Code stream-json without dumping message bodies."""
    kind = event.get("type")

    if kind == "system" and event.get("subtype") == "init":
        log.info("reviewer: claude initialized")
        return

    if kind == "assistant":
        message = event.get("message") or {}
        for block in message.get("content") or []:
            if not isinstance(block, dict) or block.get("type") != "tool_use":
                continue
            name = str(block.get("name") or "tool")
            tool_id = block.get("id")
            if tool_id:
                tool_names[str(tool_id)] = name
            activity_log.info("tool: %s", _claude_tool_label(name, block.get("input")))
        return

    if kind == "user":
        message = event.get("message") or {}
        for block in message.get("content") or []:
            if not isinstance(block, dict) or block.get("type") != "tool_result":
                continue
            tool_id = str(block.get("tool_use_id") or "")
            name = tool_names.get(tool_id, "tool")
            if block.get("is_error"):
                activity_log.warning("tool: %s failed", name)
            else:
                activity_log.info("tool: %s completed", name)
        return

    if kind == "result":
        if event.get("is_error"):
            log.error("reviewer: claude result error")
            return
        turns = event.get("num_turns")
        cost = event.get("total_cost_usd")
        details = []
        if turns is not None:
            details.append(f"turns={turns}")
        if isinstance(cost, (int, float)):
            details.append(f"cost=${cost:.4f}")
        log.info("reviewer: claude done%s", f" ({', '.join(details)})" if details else "")


def _log_gemini_event(event: dict) -> None:
    kind = event.get("event")

    if kind == "step_update":
        step = event.get("step_update") or {}
        step_type = step.get("step_type")
        state = step.get("state")

        if step_type == "tool" and state == "ACTIVE":
            tool = step.get("tool_name") or "tool"
            params = (step.get("tool_info") or {}).get("parameters") or {}
            command = params.get("CommandLine")

            if command:
                activity_log.info("reviewer: %s %s", tool, _short(command))
            else:
                activity_log.info("reviewer: %s", tool)
            return

    if kind == "result":
        result = event.get("result") or {}
        usage = result.get("usage") or {}
        total = usage.get("total_tokens")

        if total is not None:
            log.info("reviewer: done (tokens=%s)", total)
        else:
            log.info("reviewer: done")


def _run_structured_reviewer(provider: str, cmd: list[str], workspace: Path) -> bool:
    """Stream reviewer JSONL and expose only compact progress events."""
    try:
        proc = subprocess.Popen(
            cmd, cwd=workspace, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT, text=True, bufsize=1,
        )
    except OSError as exc:
        log.error("cannot start %s: %s", cmd[0], exc)
        return False

    assert proc.stdout is not None
    tail: list[str] = []
    claude_tools: dict[str, str] = {}
    structured = provider in ("claude", "codex", "gemini")  # other providers print plain text

    for raw in proc.stdout:
        line = raw.strip()
        if not line:
            continue
        event = None
        if structured:
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                pass
        if not isinstance(event, dict):
            tail.append(line)
            tail = tail[-20:]
            log.debug("%s: %s", provider, _short(line, 200))
            continue
        if provider == "codex":
            _log_codex_event(event)
        elif provider == "claude":
            _log_claude_event(event, claude_tools)
        elif provider == "gemini":
            _log_gemini_event(event)
        else:
            log.warning("unknown structured provider: %s", provider)

    returncode = proc.wait()
    if returncode != 0:
        detail = " | ".join(tail)[-500:]
        log.error("%s exited %d%s", cmd[0], returncode, f": {detail}" if detail else "")
        return False
    return True


# OpenCode error types that no retry can fix: stop dispatching and tell the owner what to do.
FATAL_ERRORS = {  # V2 structured error `type`
    "provider.auth": "authenticate the provider (`opencode auth login`)",
    "provider.no-route": "check reviewer.model in {path}",
}


def _opencode_tool_label(name: str, tool_input) -> str:
    if isinstance(tool_input, dict):
        for key in ("command", "filePath", "file_path", "path", "pattern", "query", "url"):
            if tool_input.get(key):
                return f"{name}: {_short(tool_input[key])}"
    return name


class Usage:
    """Sums per-step OpenCode `step_finish` figures (each step is reported, not a running total).

    A figure that never appears stays None rather than becoming a fabricated zero.
    """
    KEYS = ("input", "output", "reasoning")

    def __init__(self) -> None:
        self.steps = 0
        self.tokens: dict[str, int] = {}
        self.cost: float | None = None

    def add(self, part: dict) -> None:
        self.steps += 1
        tokens = part.get("tokens")
        if isinstance(tokens, dict):
            for key in self.KEYS:
                if type(tokens.get(key)) is int:
                    self.tokens[key] = self.tokens.get(key, 0) + tokens[key]
        cost = part.get("cost")
        if isinstance(cost, (int, float)) and not isinstance(cost, bool):
            self.cost = (self.cost or 0.0) + cost

    def summary(self) -> str:
        details = [f"steps={self.steps}"]
        details += [f"{k}={self.tokens[k]}" for k in self.KEYS if k in self.tokens]
        if self.cost is not None:
            details.append(f"cost≈${self.cost:.4f}")
        return ", ".join(details)


def _log_opencode_event(event: dict, usage: Usage, on_fatal: Callable[[str, str], None]) -> bool:
    """Log compact OpenCode progress; True when the event is a runner error."""
    kind = event.get("type")
    part = event.get("part") if isinstance(event.get("part"), dict) else {}
    if kind == "step_finish":
        usage.add(part)
    elif kind == "tool_use":
        state = part.get("state") if isinstance(part.get("state"), dict) else {}
        label = _opencode_tool_label(str(part.get("tool") or "tool"), state.get("input"))
        if state.get("status") == "error":
            activity_log.warning("tool: %s failed: %s", label, _short(state.get("error")))
        else:
            activity_log.info("tool: %s", label)
    elif kind == "error":
        error = event.get("error") if isinstance(event.get("error"), dict) else {}
        name = str(error.get("type") or error.get("name") or "error")
        message = _short(error.get("message") or name)
        log.error("reviewer: %s: %s", name, message)
        if name in FATAL_ERRORS:
            on_fatal(name, message)
        return True
    return False


def _run_opencode(cmd: list[str], workspace: Path, on_fatal: Callable[[str, str], None]) -> bool:
    """Run `opencode run` in the cat worktree, streaming its JSONL. True only on a clean run."""
    try:
        # V2 picks its root from $PWD before the process cwd: point both at the worktree
        proc = subprocess.Popen(
            cmd, cwd=workspace, env={**os.environ, "PWD": str(workspace)},
            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, bufsize=1,
        )
    except OSError as exc:
        log.error("cannot start %s: %s", cmd[0], exc)
        return False
    assert proc.stdout is not None
    usage, tail, failed = Usage(), [], False
    for raw in proc.stdout:
        line = raw.strip()
        if not line:
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            event = None
        if not isinstance(event, dict):
            tail = [*tail, _redact(line)][-20:]
            log.debug("opencode: %s", _short(line, 200))
            continue
        failed |= _log_opencode_event(event, usage, on_fatal)
    returncode = proc.wait()
    if returncode != 0:
        detail = " | ".join(tail)[-500:]
        log.error("opencode exited %d%s", returncode, f": {detail}" if detail else "")
        return False
    if failed:
        return False
    log.info("reviewer: opencode done (%s)", usage.summary())
    return True


def build_prompt(settings: Settings, repo: str, pr: PR) -> str:
    context = (
        f"Repository: {repo}\n"
        f"Pull request: #{pr.number} ({pr.url})\n"
        f"HEAD SHA: {pr.head}\n"
        "The current directory is a checkout of exactly this HEAD.\n"
        "Review exactly this HEAD. Do not modify code, push commits, or merge."
    )
    if pr.run_id:
        context += (
            "\n\nWhen you submit your PR Review, make the last line of its body exactly "
            f"`{run_line(pr.run_id)}` (after the verdict and evidence). This run identifier is "
            "required; do not reuse it anywhere else."
        )
    if pr.reply is not None:
        urls = "\n".join(f"  - {url}" for url in pr.reply.comment_urls if url)
        context += (
            "\n\nThis HEAD was already reviewed and an earlier review of it was a blocking -1. "
            "The PR author has since added top-level conversation comment(s):\n"
            f"{urls or '  (see the PR conversation)'}\n"
            "Re-review this same HEAD. Read your previous -1 review and the author's comments "
            "after it (the PR conversation and the review thread), plus the latest evidence "
            "(code, tests, artifacts, checks). Respond to the substantive argument, then submit "
            "a new GitHub PR Review for this HEAD that explicitly explains whether you maintain "
            "or change your judgment. Follow the repository's REVIEW.md for the verdict policy. "
            "Answer only through that submitted PR Review, not through a separate conversation "
            "comment."
        )
    return f"{settings.prompt.strip()}\n\n{context}"


def run_cli_reviewer(settings: Settings, repo: str, pr: PR, workspace: Path) -> bool:
    """Run the reviewer CLI in the prepared cat worktree and wait. True only on a clean exit."""
    cmd = COMMANDS[settings.provider](
        settings.model,
        build_prompt(settings, repo, pr),
        list(settings.args),
    )
    return _run_structured_reviewer(settings.provider, cmd, workspace)


class CliReviewer:
    """Watcher reviewer callback. Dispatches by provider and records a non-retryable OpenCode
    error (auth / no route) so the next cycle stops instead of retrying forever."""

    def __init__(self, config_path: Path | str) -> None:
        self.config_path = config_path
        self.fatal: str | None = None

    def check(self) -> None:
        if self.fatal:
            raise MissCatError(self.fatal)

    def _on_fatal(self, name: str, message: str) -> None:
        if not self.fatal:
            self.fatal = f"opencode {name}: {message}. Fix it: " + FATAL_ERRORS[name].format(
                path=self.config_path)

    def __call__(self, settings: Settings, repo: str, pr: PR, workspace: Path | None) -> bool:
        assert workspace is not None
        if settings.provider != "opencode":
            return run_cli_reviewer(settings, repo, pr, workspace)
        cmd = COMMANDS["opencode"](settings.model, build_prompt(settings, repo, pr), list(settings.args))
        return _run_opencode(cmd, workspace, self._on_fatal)


# --------------------------------------------------------------------------- state


class Key(NamedTuple):
    """Completion identity of a review: profile-independent."""
    pr: int
    head: str


class ReplyKey(NamedTuple):
    pr: int
    head: str
    comment_id: int  # immutable GitHub id of the author's conversation comment


@dataclass(frozen=True)
class ReplyRecord:
    pr: int
    head: str
    profile: str | None  # diagnostic metadata: who recorded it, never part of the identity
    comment_id: int
    handled_at: str  # UTC ISO 8601, recorded when the triggered review was verified
    review_id: int = 0  # the verified review that answered it
    extra_review_ids: tuple[int, ...] = ()  # further verified reviews kept for the same comment

    def key(self) -> ReplyKey:
        return ReplyKey(self.pr, self.head, self.comment_id)


@dataclass(frozen=True)
class PendingRun:
    """A review attempt written down before it starts, so a crash cannot lose its outcome."""
    pr: int
    head: str
    profile: str | None
    run_id: str
    comment_ids: tuple[int, ...] = ()  # author comments a same-HEAD re-review answers
    ok: bool = False  # the reviewer CLI exited cleanly but GitHub could not be checked yet

    def key(self) -> Key:
        return Key(self.pr, self.head)


@dataclass(frozen=True)
class ReviewRecord:
    pr: int
    head: str
    profile: str | None
    reviewed_at: str  # UTC ISO 8601, recorded when the reviewer completes successfully
    review_ids: tuple[int, ...] = ()  # GitHub reviews this run submitted (empty: not attributed)

    def key(self) -> Key:
        return Key(self.pr, self.head)


def _instant(timestamp: str) -> datetime:
    """Parse a stored reviewed_at into a timezone-aware instant (compare instants, not strings)."""
    return datetime.fromisoformat(timestamp.replace("Z", "+00:00"))


class State:
    """State v2: completed reviews by repository, PR and HEAD (the row `profile` is metadata only).

    An optional `replies` list (absent in files written by older versions, which stay valid)
    holds the author comment ids already answered by a same-HEAD re-review, and an optional
    `pending` list holds review attempts whose outcome has not been settled yet.

    v1 contained no review timestamps; by design it is discarded on first access
    instead of guessing review order. Call only while holding repository_lock().
    """

    def __init__(self, path: Path):
        self.path = path
        self._replies: list[ReplyRecord] = []
        self._pending: list[PendingRun] = []
        self._records = self._load()
        # corruption is judged on the full stored identity; the logical sets drop the profile
        if len(self._records) != len({(r.pr, r.head, r.profile) for r in self._records}):
            raise StateError(f"duplicate review records in {path}")
        self._reviewed = {row.key() for row in self._records}
        if len(self._replies) != len({(r.pr, r.head, r.profile, r.comment_id) for r in self._replies}):
            raise StateError(f"duplicate reply records in {path}")
        self._handled = {row.key() for row in self._replies}

    def _load(self) -> list[ReviewRecord]:
        if not self.path.exists():
            return []
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
            if not isinstance(raw, dict):
                raise ValueError("expected a JSON object")
            version = raw.get("version")
            if type(version) is int and version == 1:  # not True / 1.0, which compare equal to 1
                log.warning(
                    "resetting legacy State v1 in %s: previously reviewed PR HEADs become eligible "
                    "again, so currently open PRs may be reviewed again and use reviewer tokens",
                    self.path)
                self._write([], [], [])
                return []
            if type(version) is not int or version != 2:
                raise ValueError("unsupported state version")
            data = raw["reviewed"]
            if not isinstance(data, list):
                raise ValueError("reviewed must be a list")
            result = []
            for item in data:
                if (not isinstance(item, dict) or type(item.get("pr")) is not int
                        or item["pr"] <= 0 or not isinstance(item.get("head"), str)
                        or not item["head"] or not (item.get("profile") is None
                        or isinstance(item["profile"], str))):
                    raise ValueError("malformed review entry")
                ts = item["reviewed_at"]
                if not isinstance(ts, str):
                    raise ValueError("reviewed_at must be a timestamp")
                parsed = datetime.fromisoformat(ts.replace("Z", "+00:00"))
                if parsed.tzinfo is None or parsed.utcoffset() is None:
                    raise ValueError("reviewed_at must have a timezone")
                ids = item.get("review_ids", [])
                if not isinstance(ids, list) or not all(type(i) is int and i > 0 for i in ids):
                    raise ValueError("review_ids must be a list of ids")
                result.append(ReviewRecord(item["pr"], item["head"], item["profile"], ts, tuple(ids)))
            replies = raw.get("replies", [])
            if not isinstance(replies, list):
                raise ValueError("replies must be a list")
            for item in replies:
                if (not isinstance(item, dict) or type(item.get("pr")) is not int
                        or item["pr"] <= 0 or not isinstance(item.get("head"), str)
                        or not item["head"] or not (item.get("profile") is None
                        or isinstance(item["profile"], str))
                        or type(item.get("comment_id")) is not int or item["comment_id"] <= 0
                        or not isinstance(item.get("handled_at"), str)
                        or type(item.get("review_id", 0)) is not int):
                    raise ValueError("malformed reply entry")
                parsed = datetime.fromisoformat(item["handled_at"].replace("Z", "+00:00"))
                if parsed.tzinfo is None or parsed.utcoffset() is None:
                    raise ValueError("handled_at must have a timezone")
                extra = item.get("extra_review_ids", [])
                if not isinstance(extra, list) or not all(type(i) is int and i > 0 for i in extra):
                    raise ValueError("extra_review_ids must be a list of ids")
                self._replies.append(ReplyRecord(item["pr"], item["head"], item["profile"],
                                                 item["comment_id"], item["handled_at"],
                                                 item.get("review_id", 0), tuple(extra)))
            pending = raw.get("pending", [])
            if not isinstance(pending, list):
                raise ValueError("pending must be a list")
            for item in pending:
                ids = item.get("comment_ids", []) if isinstance(item, dict) else None
                if (not isinstance(item, dict) or type(item.get("pr")) is not int
                        or item["pr"] <= 0 or not isinstance(item.get("head"), str)
                        or not item["head"] or not (item.get("profile") is None
                        or isinstance(item["profile"], str))
                        or not isinstance(item.get("run_id"), str) or not item["run_id"]
                        or not isinstance(ids, list) or not all(type(i) is int for i in ids)
                        or type(item.get("ok", False)) is not bool):
                    raise ValueError("malformed pending entry")
                self._pending.append(PendingRun(item["pr"], item["head"], item["profile"],
                                                item["run_id"], tuple(ids), item.get("ok", False)))
            return result
        except (OSError, ValueError, KeyError, TypeError) as exc:
            raise StateError(
                f"unreadable state file {self.path} ({exc}); fix or delete it to continue"
            ) from exc

    def _write(self, records: list[ReviewRecord], replies: list[ReplyRecord],
               pending: list[PendingRun]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        rows = sorted(records, key=lambda r: (r.pr, r.head, r.profile or ""))
        data: dict = {"version": 2, "reviewed": [
            {"pr": r.pr, "head": r.head, "profile": r.profile, "reviewed_at": r.reviewed_at,
             **({"review_ids": list(r.review_ids)} if r.review_ids else {})}
            for r in rows
        ]}
        if replies:
            data["replies"] = [
                {"pr": r.pr, "head": r.head, "profile": r.profile, "comment_id": r.comment_id,
                 "handled_at": r.handled_at, "review_id": r.review_id,
                 **({"extra_review_ids": list(r.extra_review_ids)} if r.extra_review_ids else {})}
                for r in sorted(replies, key=lambda r: (r.pr, r.head, r.profile or "", r.comment_id))
            ]
        if pending:
            data["pending"] = [
                {"pr": r.pr, "head": r.head, "profile": r.profile, "run_id": r.run_id,
                 "comment_ids": list(r.comment_ids), "ok": r.ok}
                for r in pending
            ]
        _write_atomic(self.path, json.dumps(data, indent=2) + "\n")

    def reviewed(self) -> set[Key]:
        return self._reviewed

    def records(self) -> list[ReviewRecord]:
        return list(self._records)

    @staticmethod
    def _now() -> str:
        return datetime.now(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")

    def _with_ids(self, pr: int, head: str, profile: str | None,
                  ids: tuple[int, ...]) -> list[ReviewRecord]:
        """Records with verified review ids kept for (pr, head); unchanged if all are known.

        Only for a HEAD that is already complete. Ids merge into this profile's existing row
        (its reviewed_at stays) or form a new row that takes the HEAD's latest existing
        reviewed_at, so late evidence never changes the logical completion order of HEADs.
        """
        new = tuple(sorted(set(ids) - self.review_ids(Key(pr, head))))
        if not new:
            return self._records
        for n, r in enumerate(self._records):
            if (r.pr, r.head, r.profile) == (pr, head, profile):
                merged = replace(r, review_ids=tuple(sorted({*r.review_ids, *new})))
                return [*self._records[:n], merged, *self._records[n + 1:]]
        logical = max((r.reviewed_at for r in self._records if r.key() == Key(pr, head)), key=_instant)
        return [*self._records, ReviewRecord(pr, head, profile, logical, new)]

    def _keep_ids(self, pr: int, head: str, profile: str | None, ids: tuple[int, ...]) -> None:
        records = self._with_ids(pr, head, profile, ids)
        if records is not self._records:
            self._write(records, self._replies, self._pending)
            self._records = records

    def add(self, key: Key, profile: str | None, review_ids: tuple[int, ...] = ()) -> None:
        if key in self._reviewed:  # already complete: never re-record, but keep verified evidence
            self._keep_ids(key.pr, key.head, profile, review_ids)
            return
        records = [*self._records, ReviewRecord(key.pr, key.head, profile, self._now(), review_ids)]
        self._write(records, self._replies, self._pending)
        self._records = records
        self._reviewed.add(key)

    def pending(self) -> list[PendingRun]:
        return list(self._pending)

    def add_pending(self, run: PendingRun) -> None:
        pending = [*self._pending, run]
        self._write(self._records, self._replies, pending)
        self._pending = pending

    def update_pending(self, run: PendingRun) -> None:
        pending = [run if r.run_id == run.run_id else r for r in self._pending]
        self._write(self._records, self._replies, pending)
        self._pending = pending

    def drop_pending(self, run_id: str) -> None:
        pending = [r for r in self._pending if r.run_id != run_id]
        if len(pending) != len(self._pending):
            self._write(self._records, self._replies, pending)
            self._pending = pending

    def handled(self) -> set[ReplyKey]:
        return self._handled

    def review_ids(self, key: Key) -> set[int]:
        """GitHub reviews known to have been submitted for this PR HEAD, by any profile."""
        ids = {i for r in self._records if r.key() == key for i in r.review_ids}
        for r in self._replies:
            if (r.pr, r.head) == (key.pr, key.head):
                ids.update(i for i in (r.review_id, *r.extra_review_ids) if i)
        return ids

    def add_replies(self, keys: list[ReplyKey], profile: str | None, review_id: int) -> None:
        """Record author comments as answered by one verified same-HEAD review.

        Comments already handled (by any profile) are not recorded again, but the verified
        review id is still kept. One write covers both.
        """
        new = [k for k in dict.fromkeys(keys) if k not in self._handled]
        timestamp = self._now()
        replies = [*self._replies, *(ReplyRecord(k.pr, k.head, profile, k.comment_id, timestamp,
                                                 review_id) for k in new)]
        records = self._records
        if not new and keys and review_id:
            pr, head = keys[0].pr, keys[0].head
            if Key(pr, head) in self._reviewed:
                records = self._with_ids(pr, head, profile, (review_id,))
            elif review_id not in self.review_ids(Key(pr, head)):
                # The HEAD's completion was deleted by the user: do not bring it back. Keep the
                # verified id on a reply row of this profile (reply rows survive deletion and
                # count in review_ids): a new row, or the existing one's extra ids.
                same = next((n for n, r in enumerate(replies) if (r.pr, r.head, r.profile) ==
                             (pr, head, profile) and r.comment_id in {k.comment_id for k in keys}), None)
                if same is not None:
                    r = replies[same]
                    replies = [*replies[:same], replace(r, extra_review_ids=tuple(sorted(
                        {*r.extra_review_ids, review_id}))), *replies[same + 1:]]
                else:
                    k = keys[0]
                    replies = [*replies, ReplyRecord(k.pr, k.head, profile, k.comment_id, timestamp,
                                                     review_id)]
        if not new and records is self._records and replies == self._replies:
            return
        self._write(records, replies, self._pending)
        self._records, self._replies = records, replies
        self._handled.update(new)

    def is_latest(self, key: Key) -> bool:
        """The HEAD of this PR whose newest record is the most recent across the PR."""
        group = [r for r in self._records if r.pr == key.pr]
        if not group:
            return False
        return max(group, key=lambda r: _instant(r.reviewed_at)).key() == key

    def has_pending(self, key: Key) -> bool:
        return any(r.key() == key for r in self._pending)

    def remove_latest(self, key: Key) -> bool:
        """Remove every profile's record of the PR's most recently reviewed HEAD.

        Refused while an unsettled review attempt exists for that HEAD: the watcher would
        settle it into a fresh completion and silently undo the deletion.
        """
        if not self.is_latest(key) or self.has_pending(key):
            return False
        records = [r for r in self._records if r.key() != key]
        self._write(records, self._replies, self._pending)
        self._records = records
        self._reviewed.remove(key)
        return True


@contextmanager
def repository_lock(repo: str):
    """Hold an OS-backed, nonblocking lock for the whole watcher or state UI lifetime.

    The .lock file intentionally remains on disk; existence is NOT lock ownership.
    """
    path = LOCK_ROOT / state_path(repo).with_suffix(".lock").name
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        handle = path.open("a+b")
    except OSError as exc:
        raise StateError(f"cannot open repository lock {path}: {exc}") from exc
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
            raise StateError(
                f"MissCat is already using {repo}, or its lock is unavailable ({exc}). "
                "Stop the watcher before managing state or starting another watcher."
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


# --------------------------------------------------------------------------- watcher

IDLE, ACTIVE = "idle", "active"


def _fmt(seconds: float) -> str:
    seconds = int(round(seconds))
    return f"{seconds // 60}m" if seconds >= 60 and seconds % 60 == 0 else f"{seconds}s"


def _fmt_elapsed(seconds: float) -> str:
    seconds = max(0, int(seconds))
    minutes, secs = divmod(seconds, 60)
    return f"{minutes}m {secs}s" if minutes else f"{secs}s"


REVIEW_SEPARATOR = "-" * 47


def _log_review_separator() -> None:
    """Blank line + separator on the stream the log handlers write to, so order is kept."""
    stream = next((h.stream for h in (*log.handlers, *logging.getLogger().handlers)
                   if isinstance(h, logging.StreamHandler) and hasattr(h, "stream")), sys.stderr)
    stream.write(f"\n{REVIEW_SEPARATOR}\n")
    stream.flush()


class Run(NamedTuple):
    """A review running in one cat (MainThread bookkeeping)."""
    pr: PR
    started: float


class Done(NamedTuple):
    """A worker's report: the reviewer finished (ok) or failed. State is decided from it."""
    cat: int
    pr: PR
    ok: bool
    elapsed: float


class Watcher:
    """One watcher, one polling timer per repository. A review is complete per PR + HEAD,
    whichever profile ran it (`profile` is recorded as metadata only).

    The MainThread is the only scheduler and the only writer of State and Git. Up to `cats`
    independent cat workers each run one reviewer in their own worktree. A worker only runs
    the reviewer and reports `Done`; the MainThread verifies and records it at once, without
    waiting for the other cats, but a finished cat gets no new PR until the next regular poll.

    Timer: one poll per interval. New work is assigned only at a poll, to the cats that were
    idle then. The mode is ACTIVE while any cat is running (active schedule, 5m -> 1m) and
    IDLE otherwise (idle schedule, 1m -> 5m); a change of mode restarts the schedule.
    A completion neither triggers a poll nor moves the next deadline.

    A PR with a running review is never assigned again, whatever its HEAD; a changed HEAD
    is picked up by the first poll after the run is recorded. A failed HEAD cools down for
    FAILURE_WAIT and then goes last in its priority. A failed workspace sync postpones the
    next poll by at least FAILURE_WAIT without penalizing any HEAD.

    An already-reviewed HEAD is reviewed once more when the PR author adds a top-level
    conversation comment after a submitted `-1` of that HEAD (see `_find_reply`).
    """

    def __init__(
        self,
        repo: str,
        profile: str | None,
        settings: Settings,
        state: State,
        list_prs: Callable[[str], list[PR]],
        reviewer: Callable[[Settings, str, PR, Path | None], bool],
        cats: int = 1,
        workspaces: Workspaces | None = None,
        discussion: GhDiscussion | None = None,
        clock: Callable[[], float] = time.monotonic,
        fatal: Callable[[], str | None] = lambda: None,
    ):
        self.repo, self.profile, self.s, self.state = repo, profile, settings, state
        self.discussion = discussion  # None disables same-HEAD reply re-reviews
        self._checked: dict[tuple[int, str], str] = {}  # (PR, HEAD) -> updated_at with no reply due
        self.list_prs, self.reviewer, self.clock, self.fatal = list_prs, reviewer, clock, fatal
        self.cats, self.workspaces = cats, workspaces
        self.mode, self.step = IDLE, 0
        # MainThread only from here on
        self.idle: list[int] = list(range(1, cats + 1))
        self.running: dict[int, Run] = {}
        self.events: queue.Queue[Done] = queue.Queue()
        self.failed: dict[Key, float] = {}  # failed HEAD -> when: cooling down, then last
        self._stop: str | None = None  # first unrecoverable reviewer error seen
        self._started = 0  # reviews started by the current poll

    def run_forever(self) -> None:
        deadline = self.clock()  # first poll right away
        while True:
            try:
                event = self.events.get(timeout=max(0.0, deadline - self.clock()))
            except queue.Empty:
                event = None
            if event is not None:
                self._finish(event)  # the deadline is neither advanced nor reset
                self._check_fatal()
                continue
            self._check_fatal()
            delay = self.poll()
            self._check_fatal()  # a fatal error that poll() itself drained must not wait for the timer
            deadline = self.clock() + delay

    def cycle(self) -> float:
        """One poll, then wait for everything it started and record it. Returns the delay
        until the next poll. A one-shot driver for tests; `run_forever` never waits like this."""
        delay = self.poll()
        while self.running:
            self._finish(self.events.get())
        return delay

    def poll(self) -> float:
        """One regular poll: list PRs once, settle stale attempts, assign idle cats.
        Returns the delay until the next poll."""
        self._started = 0
        sync_failed = False
        try:
            prs = self.list_prs(self.repo)
        except GhError as exc:
            log.warning("cannot list PRs: %s", exc)
        else:
            self._reconcile_pending({r.pr.run_id for r in self.running.values()})
            listed = {self._key(p) for p in prs}
            self.failed = {k: t for k, t in self.failed.items() if k in listed}
            if self.idle:
                busy = {r.pr.number for r in self.running.values()}
                batch = self._next_batch(prs, len(self.idle), busy)
                if batch:
                    sync_failed = self._dispatch(batch)
        mode = ACTIVE if self.running else IDLE
        if mode != self.mode:
            self.mode, self.step = mode, 0
        seq = self.s.active if self.mode == ACTIVE else self.s.idle
        delay = seq[min(self.step, len(seq) - 1)]
        self.step = min(self.step + 1, len(seq) - 1)
        if sync_failed:
            delay = max(delay, FAILURE_WAIT)
        if self._started:
            log.info("next check in %s (%d running)", _fmt(delay), len(self.running))
        else:
            log.info("nothing to review (%s), next check in %s", self.mode, _fmt(delay))
        return delay

    def _key(self, pr: PR) -> Key:
        return Key(pr.number, pr.head)  # self.profile is only recorded as metadata

    def _next_batch(self, prs: list[PR], limit: int | None = None,
                    exclude: frozenset[int] | set[int] = frozenset()) -> list[PR]:
        """Up to `limit` PRs to review now. PRs in `exclude` (running reviews) are skipped
        entirely, without any GitHub lookup."""
        limit = self.cats if limit is None else limit
        now = self.clock()
        done = self.state.reviewed()
        unsettled = {r.key() for r in self.state.pending()}  # outcome not known yet: do not rerun
        eligible = [
            p for p in sorted(prs, key=lambda p: p.number)  # oldest PR first
            if p.number not in exclude
            and (self.s.include_drafts or not p.draft) and not p.no_review
            and self._key(p) not in unsettled
            and now - self.failed.get(self._key(p), -FAILURE_WAIT) >= FAILURE_WAIT
        ]
        waiting = [p for p in eligible if self._key(p) not in done]
        waiting += self._reply_candidates([p for p in eligible if self._key(p) in done])
        waiting.sort(key=lambda p: p.number)
        # `no-review` PRs are never eligible, even if also `urgent`. `urgent` PRs are picked
        # first; within each priority class, HEADs that failed before go last so they cannot
        # starve the others
        ordered = [p for urgent in (True, False) for failed in (False, True) for p in waiting
                   if p.urgent == urgent and (self._key(p) in self.failed) == failed]
        return ordered[:limit]

    def _reply_candidates(self, reviewed: list[PR]) -> list[PR]:
        """Reviewed current HEADs whose author answered a blocking `-1`, marked with their trigger.

        GitHub is queried only for a PR whose `updatedAt` moved since it was last found to have
        no due reply; a failed lookup is not remembered, so the next cycle retries it.
        """
        if self.discussion is None:
            return []
        self._checked = {k: v for k, v in self._checked.items()
                         if any((p.number, p.head) == k for p in reviewed)}
        due = []
        for pr in reviewed:
            if not pr.author or not pr.updated_at or self._checked.get((pr.number, pr.head)) == pr.updated_at:
                continue
            try:
                trigger = self._find_reply(pr)
            except GhError as exc:
                log.warning("PR #%d: cannot check for an author reply: %s", pr.number, exc)
                continue
            if trigger is None:
                self._checked[(pr.number, pr.head)] = pr.updated_at
            else:
                due.append(replace(pr, reply=trigger))
        return due

    def _find_reply(self, pr: PR) -> ReplyTrigger | None:
        assert self.discussion is not None
        reviews = self.discussion.reviews(pr.number)
        block = blocking_review(reviews, pr.head)
        if block is None:
            return None
        # Only a `-1` that one of MissCat's own runs submitted, under any profile, is answered
        # (proved by the run nonce, stored as a review id). A record without ids (made before attribution
        # existed, or whose reviewer omitted the nonce) is never guessed at: no trigger.
        if block.id not in self.state.review_ids(self._key(pr)):
            return None
        blocked_at = _instant(block.submitted_at)
        new = [c for c in self.discussion.comments(pr.number)
               if c.login.lower() == pr.author.lower() and _instant(c.created_at) > blocked_at
               and ReplyKey(pr.number, pr.head, c.id) not in self.state.handled()]
        if not new:
            return None
        new.sort(key=lambda c: (_instant(c.created_at), c.id))
        return ReplyTrigger(tuple(c.id for c in new), tuple(c.url for c in new), block.id)

    def _reply_review(self, run: PendingRun, reviews: list[GhReview]) -> int | None:
        """Id of the valid verdict review this re-review submitted, if any."""
        found = submitted_by_run(reviews, run.head, run.run_id)
        return max(r.id for r in found) if found else None

    def _record_reply(self, run: PendingRun, review_id: int) -> None:
        self.state.add_replies(
            [ReplyKey(run.pr, run.head, c) for c in run.comment_ids], run.profile, review_id)
        self.state.drop_pending(run.run_id)

    def _reconcile_pending(self, live: set[str] = frozenset()) -> None:
        """Settle attempts left over by a crash, restart or earlier GitHub error.
        Attempts of running reviews (`live` run ids) are not left over: they are skipped.

        A valid review submitted by this run counts even if its CLI died afterwards.
        A lookup failure keeps the attempt pending, and its HEAD is not rerun until it
        can be settled. Completion is saved before removing pending, so a crash between
        the writes leaves enough information to settle again without another invocation.
        """
        if self.discussion is None:
            return
        for run in self.state.pending():
            if run.run_id in live:
                continue
            try:
                reviews = self.discussion.reviews(run.pr)
                head = self.discussion.head(run.pr) if run.comment_ids else run.head
            except GhError as exc:
                log.warning("PR #%d: review outcome still unknown: %s", run.pr, exc)
                continue
            if head != run.head:
                log.warning("PR #%d: HEAD moved during the re-review; dropping stale attempt", run.pr)
                self.state.drop_pending(run.run_id)
                continue
            if run.comment_ids:
                review_id = self._reply_review(run, reviews)
                if review_id is not None:
                    log.info("PR #%d: settled a re-review submitted earlier", run.pr)
                    self._record_reply(run, review_id)
                    self.failed.pop(run.key(), None)
                    continue
            else:
                found = submitted_by_run(reviews, run.head, run.run_id)
                if found:
                    self.state.add(run.key(), run.profile, tuple(sorted(r.id for r in found)))
                    self.state.drop_pending(run.run_id)
                    self.failed.pop(run.key(), None)  # verified after all: no cooldown
                    continue
            self.state.drop_pending(run.run_id)  # nothing was submitted: eligible again

    def _settle(self, pr: PR, cli_ok: bool) -> bool:
        """Decide a finished run against GitHub. True = recorded as done, False = failed/pending."""
        run = PendingRun(pr.number, pr.head, self.profile, pr.run_id,
                         pr.reply.comment_ids if pr.reply else (), cli_ok)
        if self.discussion is None:
            return cli_ok
        try:
            reviews = self.discussion.reviews(pr.number)
            head = self.discussion.head(pr.number) if pr.reply is not None else pr.head
        except GhError as exc:
            log.error("PR #%d: cannot verify the review, outcome stays pending: %s", pr.number, exc)
            if cli_ok:
                self.state.update_pending(run)  # remember that the CLI itself finished cleanly
            return False
        if pr.reply is not None:
            if head != pr.head:
                self.state.drop_pending(pr.run_id)
                log.warning("PR #%d: HEAD moved to %s during the re-review", pr.number, head[:7])
                return False
            review_id = self._reply_review(run, reviews)
            if review_id is None:
                self.state.drop_pending(pr.run_id)
                log.error("PR #%d: no valid review of %s carrying this run's id was submitted",
                          pr.number, pr.head[:7])
                return False
            self._record_reply(run, review_id)  # even if the CLI failed after submitting
            return True
        found = submitted_by_run(reviews, pr.head, pr.run_id)
        if not found:
            self.state.drop_pending(pr.run_id)
            log.error("PR #%d: review of %s has no submitted review carrying this run's id; "
                      "it will be retried", pr.number, pr.head[:7])
            return False
        self.state.add(self._key(pr), self.profile, tuple(sorted(r.id for r in found)))
        self.state.drop_pending(pr.run_id)
        return True

    def _tag(self, cat: int) -> str:
        return f" [cat-{cat}]" if self.cats > 1 else ""

    def _stopping(self) -> bool:
        return bool(self._stop or self.fatal())

    def _check_fatal(self) -> None:
        """On an unrecoverable reviewer error: assign nothing more, record the reviews still
        running, then stop the watch."""
        message = self._stop or self.fatal()
        if not message:
            return
        if not self._stop:
            self._stop = message
            log.error("stopping: %s; no new reviews, waiting for %d running cat(s)",
                      message, len(self.running))
        while self.running:
            self._finish(self.events.get())
        raise MissCatError(message)

    def _drain_nowait(self) -> None:
        """Record every review that has already finished. Never polls, never blocks."""
        while True:
            try:
                done = self.events.get_nowait()
            except queue.Empty:
                return
            self._finish(done)

    def _dispatch(self, batch: list[PR]) -> bool:
        """Start `batch` on cats that were idle when the poll began. Returns True when the
        workspace sync failed.

        Preparation (sync, prepare) comes before anything is written to State, so a failed
        attempt leaves no pending run. Between steps finished reviews are recorded, so a slow
        Git step never delays them.
        """
        if self._stopping():
            return False
        available = list(self.idle)  # a cat that finishes meanwhile waits for the next poll
        if self.workspaces is not None:
            try:
                self.workspaces.sync(skip=frozenset(self.running))
            except WorkspaceError as exc:
                log.error("workspace sync failed: %s; next check in at least %s",
                          exc, _fmt(FAILURE_WAIT))
                return True  # not the HEADs' fault: no cooldown, running cats go on
        first = True
        for pr in batch:
            self._drain_nowait()
            if self._stopping():
                break
            cat = available.pop(0)
            path = None
            if self.workspaces is not None:
                try:
                    path = self.workspaces.prepare(pr, cat)
                except WorkspaceError as exc:
                    log.error("PR #%d: workspace preparation failed: %s", pr.number, exc)
                    self.failed[self._key(pr)] = self.clock()
                    continue
            self._drain_nowait()
            if self._stopping():  # a reviewer may have hit a fatal error while preparing
                break
            if self.discussion is not None:  # one nonce per run: proves which run submitted a review
                pr = replace(pr, run_id=secrets.token_hex(8))
            if first:
                _log_review_separator()
                first = False
            log.info("PR #%d: %s start (%s)%s", pr.number,
                     "re-review after author reply" if pr.reply else "review", pr.head[:7],
                     self._tag(cat))
            if self.discussion is not None:  # written down before anything runs: a crash cannot lose the attempt
                self.state.add_pending(PendingRun(pr.number, pr.head, self.profile, pr.run_id,
                                                  pr.reply.comment_ids if pr.reply else ()))
            started = self.clock()
            self.idle.remove(cat)
            self.running[cat] = Run(pr, started)
            self._started += 1
            try:
                threading.Thread(target=self._work, args=(cat, pr, path, started),
                                 name=f"cat-{cat}", daemon=True).start()
            except (RuntimeError, OSError) as exc:  # no worker will ever report: undo the start
                log.error("PR #%d: cannot start the cat worker: %s", pr.number, exc)
                del self.running[cat]
                self.idle.append(cat)
                self.idle.sort()
                self._started -= 1
                if self.discussion is not None:
                    self.state.drop_pending(pr.run_id)
                self.failed[self._key(pr)] = self.clock()
                continue
        self._drain_nowait()
        return False

    def _work(self, cat: int, pr: PR, path: Path | None, started: float) -> None:
        """Cat worker thread: runs the reviewer only. State, Git and GitHub are not touched."""
        try:
            ok = bool(self.reviewer(self.s, self.repo, pr, path))
        except Exception:
            log.exception("PR #%d: reviewer crashed", pr.number)
            ok = False
        self.events.put(Done(cat, pr, ok, self.clock() - started))

    def _finish(self, done: Done) -> None:
        """Decide and record one finished review, then free its cat and PR."""
        pr, ok = done.pr, done.ok
        if self.discussion is not None:
            ok = self._settle(pr, ok)
        elif ok:  # no GitHub check available: a clean exit is the record
            self.state.add(self._key(pr), self.profile)
        if ok:
            self.failed.pop(self._key(pr), None)
            log.info("PR #%d: review done in %s%s", pr.number, _fmt_elapsed(done.elapsed),
                     self._tag(done.cat))
        else:
            self.failed[self._key(pr)] = self.clock()
            log.warning("PR #%d: review failed after %s, HEAD stays eligible (retry after %s)%s",
                        pr.number, _fmt_elapsed(done.elapsed), _fmt(FAILURE_WAIT),
                        self._tag(done.cat))
        del self.running[done.cat]  # only now may the PR be assigned again
        self.idle.append(done.cat)
        self.idle.sort()


# --------------------------------------------------------------------------- interactive state UI


def display_order(records: list[ReviewRecord]) -> list[ReviewRecord]:
    """Newest completed review first across all PRs/profiles; presentation only.

    Ties on the instant are broken deterministically by PR (higher first), profile, then HEAD.
    """
    by_tie = sorted(records, key=lambda r: (r.profile or "", r.head))
    by_tie.sort(key=lambda r: r.pr, reverse=True)
    return sorted(by_tie, key=lambda r: _instant(r.reviewed_at), reverse=True)


def run_state_ui(state: State, repo: str) -> int:
    """Keyboard-only state manager. This deliberately does not query GitHub."""
    from prompt_toolkit.application import Application
    from prompt_toolkit.key_binding import KeyBindings
    from prompt_toolkit.layout import Layout, Window, FormattedTextControl
    from prompt_toolkit.styles import Style

    def ordered() -> list[ReviewRecord]:
        return display_order(state.records())

    rows = ordered()
    index = 0
    mode = "list"
    message = ""
    styles = Style.from_dict({
        "header": "bold ansicyan", "selected": "reverse",
        "muted": "ansibrightblack", "warning": "bold ansiyellow",
    })

    def current() -> ReviewRecord | None:
        return rows[index] if rows else None

    def siblings(r: ReviewRecord) -> list[ReviewRecord]:
        return [x for x in state.records() if x.key() == r.key()]

    def local_time(value: str) -> str:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone().strftime(
            "%Y-%m-%d %H:%M:%S %Z"
        )

    def redraw():
        result = [("class:header", "MissCat — State Manager\n"),
                  ("", f"Repository: {repo}\n\n")]
        r = current()
        if mode == "detail" and r:
            result += [
                ("class:header", "HEAD details\n\n"),
                ("", f"PR:          #{r.pr}\nProfile:     {r.profile or 'default'}\n"),
                ("", f"HEAD SHA:    {r.head}\n"),
                ("", f"Reviewed at: {local_time(r.reviewed_at)}\n\n"),
                ("class:muted", "Esc/Enter return   Q quit\n"),
            ]
        else:
            if not rows:
                result.append(("class:muted", "No recorded reviews.\n"))
            # Show a moving window: long histories must remain navigable by arrow keys.
            page_size = 14
            first = max(0, min(index - page_size // 2, len(rows) - page_size))
            last = min(len(rows), first + page_size)
            if first:
                result.append(("class:muted", f"… {first} earlier rows above …\n"))
            for n in range(first, last):
                row = rows[n]
                latest = state.is_latest(row.key())
                text = (f"{'>' if n == index else ' '}  #{row.pr:<5} "
                        f"{(row.profile or 'default'):<15} {row.head[:10]}  "
                        f"{local_time(row.reviewed_at)}"
                        f"{'  [latest]' if latest else ''}\n")
                result.append(("class:selected" if n == index else "", text))
            if last < len(rows):
                result.append(("class:muted", f"… {len(rows) - last} more rows below …\n"))
            result += [("", "\n"), ("class:muted",
                "↑↓ Move   Enter Details   Del/BS Remove latest   Q/Esc Quit\n")]
            if mode == "confirm" and r:
                group = siblings(r)
                profiles = ", ".join(sorted(x.profile or "default" for x in group))
                result.append(("class:warning",
                    f"\nDelete PR #{r.pr} / {r.head[:10]}: removes {len(group)} record(s) "
                    f"(profiles: {profiles}) and makes this HEAD eligible again? [y/N] "))
        if message:
            result += [("", "\n"), ("class:warning", message + "\n")]
        return result

    bindings = KeyBindings()

    @bindings.add("up")
    def move_up(event):
        nonlocal index, message
        if mode == "list" and rows:
            index = max(0, index - 1)
            message = ""

    @bindings.add("down")
    def move_down(event):
        nonlocal index, message
        if mode == "list" and rows:
            index = min(len(rows) - 1, index + 1)
            message = ""

    @bindings.add("enter")
    def enter(event):
        nonlocal mode, message
        if mode == "confirm":
            mode = "list"  # default N
        elif mode == "detail":
            mode = "list"
        elif current():
            mode = "detail"
        message = ""

    def request_delete(event):
        nonlocal mode, message
        r = current()
        if mode != "list" or r is None:
            return
        if not state.is_latest(r.key()):
            message = "Only the most recently reviewed HEAD of this PR can be removed."
        elif state.has_pending(r.key()):
            message = ("This HEAD has an unsettled review attempt. Run the watcher once to settle "
                       "it, then remove the HEAD.")
        else:
            message = ""
            mode = "confirm"

    bindings.add("delete")(request_delete)
    bindings.add("backspace")(request_delete)

    @bindings.add("y")
    def confirm_yes(event):
        nonlocal mode, rows, index, message
        if mode != "confirm":
            return
        r = current()
        try:
            count = len(siblings(r)) if r is not None else 0
            if r is not None and state.remove_latest(r.key()):
                rows = ordered()
                index = max(0, min(index, len(rows) - 1))
                message = (f"Deleted {count} record(s) for PR #{r.pr} / {r.head[:10]}. "
                           "Only a matching current PR HEAD is re-reviewed.")
            else:
                message = "No eligible record selected."
        except (OSError, StateError) as exc:
            message = f"Cannot save state: {exc}"
        mode = "list"

    @bindings.add("n")
    def confirm_no(event):
        nonlocal mode, message
        if mode == "confirm":
            mode, message = "list", ""

    @bindings.add("escape")
    def escape(event):
        nonlocal mode, message
        if mode == "list":
            event.app.exit()
        else:
            mode, message = "list", ""

    @bindings.add("q")
    @bindings.add("c-c")
    def quit_ui(event):
        nonlocal mode
        if mode == "confirm":
            mode = "list"  # cancellation
        else:
            event.app.exit()

    app = Application(
        layout=Layout(Window(FormattedTextControl(redraw), wrap_lines=False)),
        key_bindings=bindings,
        style=styles,
        full_screen=True,
        mouse_support=False,
    )
    app.run()
    return 0


def run_state(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(
        prog="misscat state", description="Interactively inspect/remove completed review HEADs."
    )
    parser.add_argument("repo", metavar="owner/repo|.")
    args = parser.parse_args(argv)
    try:
        repo = resolve_repo_arg(args.repo)
        if not (sys.stdin.isatty() and sys.stdout.isatty()):
            raise StateError("state manager needs an interactive terminal (TTY)")
        with repository_lock(repo):
            return run_state_ui(State(state_path(repo)), repo)
    except MissCatError as exc:
        print(f"misscat: {exc}", file=sys.stderr)
        return 2


# --------------------------------------------------------------------------- CLI


def _positive_int(value: str) -> int:
    try:
        number = int(value)
    except ValueError:
        number = 0
    if number < 1:
        raise argparse.ArgumentTypeError(f"must be a whole number >= 1, not {value!r}")
    return number


def _configure_logging(loud: bool) -> None:
    """Default: review status plus every warning/error. --loud: also tool activity and DEBUG."""
    log.setLevel(logging.DEBUG if loud else logging.INFO)
    activity_log.setLevel(logging.NOTSET if loud else logging.WARNING)


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    parser = argparse.ArgumentParser(
        prog="misscat",
        description="Watch a GitHub repository and run an AI review on every new PR HEAD.",
        epilog=(
            "profile is a name: 'sol' loads ~/.config/misscat/sol.yml over the defaults.\n"
            "`misscat init [--force]` installs bundled profiles.\n"
            "`misscat state owner/repo` opens the state manager.\n"
            "A review is complete per PR + HEAD whatever profile ran it, e.g. `misscat . opencode`\n"
            "(OpenCode: reviewer.provider: opencode in ~/.config/misscat/opencode.yml).\n"
            "'.' as the repository always means the current Git repository's `origin` remote\n"
            "(github.com only), e.g. `misscat .`, `misscat . luna`, `misscat state .`.\n\n"
            "Requirements and limits:\n"
            "  - Git must authenticate to the repository (Git/SSH auth, or run\n"
            "    `gh auth setup-git` for HTTPS). MissCat stores no credentials.\n"
            "  - Reviewers run inside a checkout of PR-controlled files: use MissCat only\n"
            "    with repositories and pull requests you trust.\n"
            "  - Run at most one MissCat process per repository."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    if argv and argv[0] == "init":
        return run_init(argv[1:])
    if argv and argv[0] == "state":
        return run_state(argv[1:])
    parser.add_argument("repo", metavar="owner/repo|.")
    parser.add_argument("profile", metavar="profile", nargs="?")
    parser.add_argument(
        "--loud",
        action="store_true",
        help="also show reviewer tool activity (exec/MCP/tool/web search) and raw CLI "
        "debug output; warnings and errors are always shown",
    )
    parser.add_argument(
        "--cat",
        type=_positive_int,
        default=1,
        metavar="N",
        help="review up to N PR HEADs concurrently (default: 1); each cat takes new work "
        "only at the regular poll",
    )
    parser.add_argument(
        "-v",
        "--version",
        action="version",
        version=f"%(prog)s {__version__}",
    )
    if not argv:
        parser.print_help()
        return 2
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(threadName)s %(message)s" if args.cat > 1
        else "%(asctime)s %(levelname)s %(message)s",
        datefmt="%H:%M:%S",
    )
    _configure_logging(args.loud)

    try:
        repo = resolve_repo_arg(args.repo)  # canonicalize once; everything below uses this form
        with repository_lock(repo):
            ensure_initial_profiles()
            settings = load_settings(args.profile)
            for tool in ("git", "gh", EXECUTABLES[settings.provider]):
                if shutil.which(tool) is None:
                    raise MissCatError(f"required CLI not found on PATH: {tool}")
            state = State(state_path(repo))  # v1 resets; corrupt/unknown files fail safely
            log.info("watching %s with %s/%s (profile: %s)", repo, settings.provider,
                     settings.model, args.profile or "default")
            workspaces = Workspaces(repo, args.cat)
            try:
                workspaces.sync()
            except WorkspaceError as exc:  # retried before every assignment
                log.warning("workspace not ready yet: %s", exc)
            reviewer = CliReviewer(profile_path(args.profile) if args.profile else "the bundled defaults")

            # a non-retryable reviewer error ends the watch once running reviews are recorded
            Watcher(repo, args.profile, settings, state, gh_open_prs, reviewer,
                    cats=args.cat, workspaces=workspaces, discussion=GhDiscussion(repo),
                    fatal=lambda: reviewer.fatal).run_forever()
    except MissCatError as exc:
        print(f"misscat: {exc}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        log.info("stopped")
        return 130
    return 0


if __name__ == "__main__":
    sys.exit(main())

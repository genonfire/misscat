#!/usr/bin/env python3
"""MissCat - never miss a single commit.

Watches a GitHub repository and runs an AI review once for every new PR HEAD.
One repository = one MissCat process = one workspace = one state file.
One review at a time by default (`--cat=N` reviews up to N waiting PR HEADs in a batch, one
persistent worktree per cat, and waits for the whole batch); then an immediate re-check.

Each review runs the reviewer CLI (Claude Code, Codex, or Gemini via Antigravity CLI)
from the root of a MissCat-owned worktree of the exact PR HEAD, so the CLI finds the
repository's own instruction files (REVIEW.md, CLAUDE.md, AGENTS.md) by itself. MissCat
never parses them and never touches your own working tree.

Local layout (repository names are canonicalized to lowercase)
  ~/.config/misscat/<profile>.yml          reviewer profiles (bundled ones installed by `misscat init`)
  ~/.config/misscat/<owner>__<repo>.json   reviewed HEADs of one repository (PR + HEAD + profile)
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
import re
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, NamedTuple
from urllib.parse import urlsplit

import yaml

__version__ = "1.4.2"

log = logging.getLogger("misscat")

CONFIG_DIR = Path.home() / ".config" / "misscat"
WORKSPACE_ROOT = Path.home() / ".cache" / "misscat" / "repos"  # control clones
CAT_ROOT = Path.home() / ".cache" / "misscat" / "cats"  # persistent cat-N worktrees
LOCK_ROOT = Path.home() / ".cache" / "misscat" / "locks"  # outside CONFIG_DIR: locking must not
# create it, or a state-only command would make the first watcher run skip profile initialization
OWNER_RE = re.compile(r"^[A-Za-z0-9-]+$")  # no "_": keeps the "__" in state filenames unambiguous
NAME_RE = re.compile(r"^[A-Za-z0-9_.-]+$")
PROVIDERS = ("claude", "codex", "gemini")
EXECUTABLES = {"claude": "claude", "codex": "codex", "gemini": "agy"}  # provider -> CLI binary
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
    args: tuple[str, ...] = ()  # reviewer.args: extra reviewer CLI options, passed through untouched


def _schedule(value, name: str) -> tuple[float, ...]:
    ok = isinstance(value, list) and value and all(
        isinstance(v, (int, float)) and not isinstance(v, bool) and v > 0 for v in value
    )
    if not ok:
        raise ConfigError(f"{name} must be a non-empty list of positive numbers")
    return tuple(float(v) for v in value)


def build_settings(cfg: dict) -> Settings:
    try:
        reviewer = cfg["reviewer"]
        provider, model = reviewer["provider"], reviewer["model"]
        args = reviewer["args"]
        prompt, include_drafts = cfg["prompt"], cfg["review"]["include_drafts"]
        idle, active = cfg["watch"]["idle"], cfg["watch"]["active"]
    except (KeyError, TypeError, AttributeError) as exc:
        raise ConfigError(f"missing or malformed config section: {exc}") from exc
    if provider not in PROVIDERS:
        raise ConfigError(f"reviewer.provider must be one of {', '.join(PROVIDERS)}")
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


URGENT_LABEL = "urgent"
NO_REVIEW_LABEL = "no-review"


def gh_open_prs(repo: str) -> list[PR]:
    cmd = [
        "gh", "pr", "list", "--repo", repo, "--state", "open", "--limit", "200",
        "--json", "number,headRefOid,isDraft,url,title,labels",
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
                          URGENT_LABEL in names, NO_REVIEW_LABEL in names))
        return prs
    except (ValueError, KeyError, TypeError) as exc:
        raise GhError(f"unexpected gh output: {exc}") from exc


# --------------------------------------------------------------------------- workspace


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

    def sync(self) -> None:
        """Ensure the control clone and cat-1..cat-N exist; leave control on the default HEAD.

        Only missing pieces are created. Cats beyond `cats` stay on disk untouched.
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
            _git(["fetch", "origin", f"refs/pull/{pr.number}/head"], self.control)  # fork PRs too
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

    if kind == "turn.started":
        log.info("reviewer: working")
        return
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
            log.info("exec: %s", _short(item.get("command") or "command"))
        elif item_type == "mcp_tool_call":
            log.info("mcp: %s started", _mcp_name(item))
        elif item_type == "web_search":
            query = _short(item.get("query"))
            log.info("web search: %s", query or "started")
        return

    if item_type == "command_execution":
        status = item.get("status") or "completed"
        exit_code = item.get("exit_code")
        suffix = f" ({exit_code})" if exit_code is not None else ""
        if status == "failed" or (isinstance(exit_code, int) and exit_code != 0):
            log.warning("exec: %s%s", status, suffix)
        else:
            log.info("exec: %s%s", status, suffix)
    elif item_type == "mcp_tool_call":
        status = item.get("status") or "completed"
        if status == "failed":
            log.warning("mcp: %s failed", _mcp_name(item))
        else:
            log.info("mcp: %s %s", _mcp_name(item), status)
    elif item_type == "web_search":
        log.info("web search: completed")


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
            log.info("tool: %s", _claude_tool_label(name, block.get("input")))
        return

    if kind == "user":
        message = event.get("message") or {}
        for block in message.get("content") or []:
            if not isinstance(block, dict) or block.get("type") != "tool_result":
                continue
            tool_id = str(block.get("tool_use_id") or "")
            name = tool_names.get(tool_id, "tool")
            if block.get("is_error"):
                log.warning("tool: %s failed", name)
            else:
                log.info("tool: %s completed", name)
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

        if step_type == "agent_response" and state == "DONE":
            log.info("reviewer: working")
            return

        if step_type == "tool" and state == "ACTIVE":
            tool = step.get("tool_name") or "tool"
            params = (step.get("tool_info") or {}).get("parameters") or {}
            command = params.get("CommandLine")

            if command:
                log.info("reviewer: %s %s", tool, _short(command))
            else:
                log.info("reviewer: %s", tool)
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


def build_prompt(settings: Settings, repo: str, pr: PR) -> str:
    context = (
        f"Repository: {repo}\n"
        f"Pull request: #{pr.number} ({pr.url})\n"
        f"HEAD SHA: {pr.head}\n"
        "The current directory is a checkout of exactly this HEAD.\n"
        "Review exactly this HEAD. Do not modify code, push commits, or merge."
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


# --------------------------------------------------------------------------- state


class Key(NamedTuple):
    pr: int
    head: str
    profile: str | None  # None = bundled default, different from profile "default"


@dataclass(frozen=True)
class ReviewRecord:
    pr: int
    head: str
    profile: str | None
    reviewed_at: str  # UTC ISO 8601, recorded when the reviewer completes successfully

    def key(self) -> Key:
        return Key(self.pr, self.head, self.profile)


def _instant(timestamp: str) -> datetime:
    """Parse a stored reviewed_at into a timezone-aware instant (compare instants, not strings)."""
    return datetime.fromisoformat(timestamp.replace("Z", "+00:00"))


class State:
    """State v2: completed reviews by repository, PR, HEAD and profile.

    v1 contained no review timestamps; by design it is discarded on first access
    instead of guessing review order. Call only while holding repository_lock().
    """

    def __init__(self, path: Path):
        self.path = path
        self._records = self._load()
        self._reviewed = {row.key() for row in self._records}
        if len(self._records) != len(self._reviewed):
            raise StateError(f"duplicate review records in {path}")

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
                self._write([])
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
                result.append(ReviewRecord(item["pr"], item["head"], item["profile"], ts))
            return result
        except (OSError, ValueError, KeyError, TypeError) as exc:
            raise StateError(
                f"unreadable state file {self.path} ({exc}); fix or delete it to continue"
            ) from exc

    def _write(self, records: list[ReviewRecord]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        rows = sorted(records, key=lambda r: (r.pr, r.head, r.profile or ""))
        data = {"version": 2, "reviewed": [
            {"pr": r.pr, "head": r.head, "profile": r.profile, "reviewed_at": r.reviewed_at}
            for r in rows
        ]}
        _write_atomic(self.path, json.dumps(data, indent=2) + "\n")

    def reviewed(self) -> set[Key]:
        return self._reviewed

    def records(self) -> list[ReviewRecord]:
        return list(self._records)

    def add(self, key: Key) -> None:
        if key in self._reviewed:
            return
        timestamp = datetime.now(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")
        records = [*self._records, ReviewRecord(*key, timestamp)]
        self._write(records)
        self._records = records
        self._reviewed.add(key)

    def is_latest(self, key: Key) -> bool:
        group = [r for r in self._records if (r.pr, r.profile) == (key.pr, key.profile)]
        if not group:
            return False
        return max(group, key=lambda r: _instant(r.reviewed_at)).key() == key

    def remove_latest(self, key: Key) -> bool:
        """Only remove the most recent successful review for a PR+profile."""
        if not self.is_latest(key):
            return False
        records = [r for r in self._records if r.key() != key]
        self._write(records)
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


class Watcher:
    """One watcher, one polling timer per repository.

    Timer: starts idle (1m -> 5m). A successful review switches it to active (5m -> 1m)
    from the start of the sequence. When no PR is open any more it falls back to idle.
    A batch in which every review failed waits FAILURE_WAIT and does not advance the timer.

    Reviews run in batches of at most `cats` PR HEADs, one cat worktree each, concurrently.
    The batch is a barrier: nothing new starts until every reviewer of the batch has
    finished; only then are successes recorded (here, sequentially) and PRs listed again.
    """

    def __init__(
        self,
        repo: str,
        profile: str | None,
        settings: Settings,
        state: State,
        list_prs: Callable[[str], list[PR]],
        reviewer: Callable[[Settings, str, PR, Path | None], bool],
        sleep: Callable[[float], None] = time.sleep,
        cats: int = 1,
        workspaces: Workspaces | None = None,
    ):
        self.repo, self.profile, self.s, self.state = repo, profile, settings, state
        self.list_prs, self.reviewer, self.sleep = list_prs, reviewer, sleep
        self.cats, self.workspaces = cats, workspaces
        self.mode, self.step = IDLE, 0
        self.failed: set[Key] = set()  # failed in the previous batch: they go last next time

    def run_forever(self) -> None:
        while True:
            self.cycle()

    def cycle(self) -> None:
        """One iteration: review a batch of waiting PR HEADs if any, otherwise sleep."""
        try:
            prs = self.list_prs(self.repo)
        except GhError as exc:
            log.warning("cannot list PRs: %s", exc)
            self._sleep_adaptive()
            return
        if not prs and self.mode == ACTIVE:
            self.mode, self.step = IDLE, 0
        batch = self._next_batch(prs)
        if not batch:
            self._sleep_adaptive()
        elif any(self._review_batch(batch)):
            self.mode, self.step = ACTIVE, 0  # then straight back to a fresh listing, no sleep
        else:
            log.warning("checking again in %s", _fmt(FAILURE_WAIT))
            self.sleep(FAILURE_WAIT)

    def _key(self, pr: PR) -> Key:
        return Key(pr.number, pr.head, self.profile)

    def _next_batch(self, prs: list[PR]) -> list[PR]:
        done = self.state.reviewed()
        waiting = [
            p for p in sorted(prs, key=lambda p: p.number)  # oldest PR first
            if (self.s.include_drafts or not p.draft) and not p.no_review and self._key(p) not in done
        ]
        # `no-review` PRs are never eligible, even if also `urgent`. `urgent` PRs are picked
        # first; within each priority class, HEADs that just failed go last so they cannot
        # starve the others
        ordered = [p for urgent in (True, False) for failed in (False, True) for p in waiting
                   if p.urgent == urgent and (self._key(p) in self.failed) == failed]
        return ordered[: self.cats]

    def _review(self, pr: PR) -> bool:
        return self._review_batch([pr])[0]

    def _review_batch(self, batch: list[PR]) -> list[bool]:
        """Review `batch` concurrently, wait for all of it, then record the successes."""
        tag = (lambda n: f" [cat-{n}]") if self.cats > 1 else (lambda n: "")
        _log_review_separator()
        for n, pr in enumerate(batch, 1):
            log.info("PR #%d: review start (%s)%s", pr.number, pr.head[:7], tag(n))
        started = [time.monotonic() for _ in batch]
        workspaces: list[Path | None] = [None] * len(batch)
        results: list[bool | None] = [None] * len(batch)  # None = not run yet
        elapsed: list[float | None] = [None] * len(batch)

        # Shared Git metadata (control clone fetches, worktree creation) is only touched here,
        # one step at a time; a preparation failure fails only that PR.
        try:
            if self.workspaces is not None:
                self.workspaces.sync()
        except WorkspaceError as exc:
            log.error("workspace preparation failed: %s", exc)
            results = [False] * len(batch)
        for i, pr in enumerate(batch):
            if results[i] is not None or self.workspaces is None:
                continue
            try:
                workspaces[i] = self.workspaces.prepare(pr, i + 1)
            except WorkspaceError as exc:
                log.error("PR #%d: workspace preparation failed: %s", pr.number, exc)
                results[i] = False

        def run(i: int) -> None:
            try:
                results[i] = bool(self.reviewer(self.s, self.repo, batch[i], workspaces[i]))
            except Exception:
                log.exception("PR #%d: reviewer crashed", batch[i].number)
                results[i] = False
            elapsed[i] = time.monotonic() - started[i]

        pending = [i for i, r in enumerate(results) if r is None]
        if len(pending) == 1:
            run(pending[0])
        elif pending:
            threads = [threading.Thread(target=run, args=(i,), name=f"cat-{i + 1}", daemon=True)
                       for i in pending]
            for thread in threads:
                thread.start()
            for thread in threads:  # batch barrier: nobody is replaced while others still review
                thread.join()
        for i in range(len(batch)):
            if elapsed[i] is None:  # failed before a reviewer started
                elapsed[i] = time.monotonic() - started[i]

        self.failed = set()
        for n, (pr, ok) in enumerate(zip(batch, results), 1):
            if ok:
                self.state.add(self._key(pr))  # only a successful review is recorded
                log.info("PR #%d: review done in %s%s", pr.number, _fmt_elapsed(elapsed[n - 1]),
                         tag(n))
            else:
                self.failed.add(self._key(pr))
                log.warning("PR #%d: review failed after %s, HEAD stays eligible%s", pr.number,
                            _fmt_elapsed(elapsed[n - 1]), tag(n))
        return [bool(ok) for ok in results]

    def _sleep_adaptive(self) -> None:
        seq = self.s.active if self.mode == ACTIVE else self.s.idle
        delay = seq[min(self.step, len(seq) - 1)]
        log.info("nothing to review (%s), next check in %s", self.mode, _fmt(delay))
        self.sleep(delay)
        self.step = min(self.step + 1, len(seq) - 1)


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
                result.append(("class:warning",
                    f"\nDelete PR #{r.pr} / {r.head[:10]} / {r.profile or 'default'}? [y/N] "))
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
            message = "Only the most recently reviewed HEAD for this PR/profile can be removed."
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
            if r is not None and state.remove_latest(r.key()):
                rows = ordered()
                index = max(0, min(index, len(rows) - 1))
                message = "Deleted one completed review. Only a matching current PR HEAD is re-reviewed."
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


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    parser = argparse.ArgumentParser(
        prog="misscat",
        description="Watch a GitHub repository and run an AI review on every new PR HEAD.",
        epilog=(
            "profile is a name: 'sol' loads ~/.config/misscat/sol.yml over the defaults.\n"
            "`misscat init [--force]` installs bundled profiles.\n"
            "`misscat state owner/repo` opens the state manager.\n"
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
        help="show detailed reviewer activity",
    )
    parser.add_argument(
        "--cat",
        type=_positive_int,
        default=1,
        metavar="N",
        help="review up to N waiting PR HEADs at once in a batch (default: 1); the whole "
        "batch must finish before the next PR check",
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

    if args.loud:
        log.setLevel(logging.DEBUG)

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
            except WorkspaceError as exc:  # retried before every batch, like a failed review
                log.warning("workspace not ready yet: %s", exc)
            Watcher(repo, args.profile, settings, state, gh_open_prs, run_cli_reviewer,
                    cats=args.cat, workspaces=workspaces).run_forever()
    except MissCatError as exc:
        print(f"misscat: {exc}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        log.info("stopped")
        return 130
    return 0


if __name__ == "__main__":
    sys.exit(main())

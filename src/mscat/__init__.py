"""mscat - MissCat's reviewer watcher driven by the OpenCode CLI (native V2, v2.0.32 interface).

Reuses MissCat's watcher, exact-HEAD cat worktrees, review confirmation, recovery, State and
repository lock. Configuration is always ~/.config/misscat/opencode.yml; completed reviews are
recorded under the State profile identity `mscat/opencode`.
"""
from __future__ import annotations

import argparse
import importlib.resources
import json
import logging
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

import yaml

import misscat
from misscat import (
    ConfigError, GhDiscussion, MissCatError, PR, Settings, State, WorkspaceError, Workspaces,
    build_prompt, deep_merge, gh_open_prs, log, activity_log, repository_lock, resolve_repo_arg,
    state_path,
)

__version__ = misscat.__version__

CONFIG_NAME = "opencode.yml"
PROFILE = "mscat/opencode"  # State identity; "/" keeps it apart from every MissCat profile name
PROVIDERS = ("opencode",)
# OpenCode error names that no retry can fix: stop dispatching and tell the owner what to do.
FATAL_ERRORS = {  # V2 structured error `type`
    "provider.auth": "authenticate the provider (`opencode auth login`)",
    "provider.no-route": "check reviewer.model in {path}",
}


# --------------------------------------------------------------------------- configuration


def _template() -> str:
    try:
        return importlib.resources.files("mscat").joinpath(CONFIG_NAME).read_text(encoding="utf-8")
    except (OSError, TypeError) as exc:
        raise ConfigError(f"cannot read bundled {CONFIG_NAME}: {exc}") from exc


def config_path() -> Path:
    return misscat.CONFIG_DIR / CONFIG_NAME


def ensure_config() -> None:
    """Normal runs only. Keep MissCat's first-run profile install first, then add opencode.yml
    when that one file is missing (never overwriting it, even if the directory already exists)."""
    misscat.ensure_initial_profiles()
    path = config_path()
    if path.exists():
        return
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        if misscat._write_atomic(path, _template(), overwrite=False):
            log.info("installed %s", path)
    except OSError as exc:
        raise ConfigError(f"cannot install {path}: {exc}") from exc


def load_settings() -> Settings:
    try:
        template = yaml.safe_load(_template())
    except yaml.YAMLError as exc:
        raise ConfigError(f"bundled {CONFIG_NAME}: {exc}") from exc
    cfg = deep_merge(template, misscat._load_yaml(config_path()))
    return misscat.build_settings(cfg, PROVIDERS)


# --------------------------------------------------------------------------- runner


def command(settings: Settings, prompt: str) -> list[str]:
    """V2 shape: a private per-run server, a fresh session, native args before the prompt.

    The model is `provider/model[#variant]`, passed unchanged.
    """
    return ["opencode", "run", "--standalone", "--format", "json", *settings.args,
            "--model", settings.model, prompt]


def _tool_label(name: str, tool_input) -> str:
    if isinstance(tool_input, dict):
        for key in ("command", "filePath", "file_path", "path", "pattern", "query", "url"):
            if tool_input.get(key):
                return f"{name}: {misscat._short(tool_input[key])}"
    return name


class Usage:
    """Sums per-step `step_finish` figures (OpenCode reports each step, not a running total).

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
            details.append(f"cost≈${self.cost:.4f} (CLI estimate)")
        return ", ".join(details)


class OpenCodeReviewer:
    """Watcher reviewer callback. Records a non-retryable error so the next cycle stops."""

    def __init__(self) -> None:
        self.fatal: str | None = None

    def check(self) -> None:
        if self.fatal:
            raise MissCatError(self.fatal)

    def __call__(self, settings: Settings, repo: str, pr: PR, workspace: Path | None) -> bool:
        assert workspace is not None
        cmd = command(settings, build_prompt(settings, repo, pr))
        try:
            # V2 picks its root from $PWD before the process cwd: point both at the worktree
            proc = subprocess.Popen(
                cmd, cwd=workspace, env={**os.environ, "PWD": str(workspace)}, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT, text=True, bufsize=1,
            )
        except OSError as exc:
            log.error("cannot start %s: %s", cmd[0], exc)
            return False
        assert proc.stdout is not None
        usage, tools, tail, failed = Usage(), {}, [], False
        for raw in proc.stdout:
            line = raw.strip()
            if not line:
                continue
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                event = None
            if not isinstance(event, dict):
                tail = [*tail, misscat._redact(line)][-20:]
                log.debug("opencode: %s", misscat._short(line, 200))
                continue
            failed |= self._log_event(event, usage, tools)
        returncode = proc.wait()
        if returncode != 0:
            detail = " | ".join(tail)[-500:]
            log.error("opencode exited %d%s", returncode, f": {detail}" if detail else "")
            return False
        if failed:
            return False
        log.info("reviewer: opencode done (%s)", usage.summary())
        return True

    def _log_event(self, event: dict, usage: Usage, tools: dict) -> bool:
        """Log compact progress; True when the event is a runner error."""
        kind = event.get("type")
        part = event.get("part") if isinstance(event.get("part"), dict) else {}
        if kind == "step_finish":
            usage.add(part)
        elif kind == "tool_use":
            state = part.get("state") if isinstance(part.get("state"), dict) else {}
            name = str(part.get("tool") or "tool")
            label = _tool_label(name, state.get("input"))
            if state.get("status") == "error":
                activity_log.warning("tool: %s failed: %s", label, misscat._short(state.get("error")))
            else:
                activity_log.info("tool: %s", label)
        elif kind == "error":
            error = event.get("error") if isinstance(event.get("error"), dict) else {}
            name = str(error.get("type") or error.get("name") or "error")
            message = misscat._short(error.get("message") or name)
            log.error("reviewer: %s: %s", name, message)
            if name in FATAL_ERRORS and not self.fatal:
                self.fatal = f"opencode {name}: {message}. Fix it: " + FATAL_ERRORS[name].format(
                    path=config_path())
            return True
        return False


# --------------------------------------------------------------------------- CLI


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    parser = argparse.ArgumentParser(
        prog="mscat",
        description="Watch a GitHub repository and run an OpenCode review on every new PR HEAD.",
        epilog=(
            "Configuration is always ~/.config/misscat/opencode.yml (created from a bundled\n"
            "template when missing, never overwritten). Targets OpenCode native V2 v2.0.32.\n"
            "State, worktrees and the repository lock are shared with `misscat`; reviews are\n"
            "recorded under the profile 'mscat/opencode' (see `misscat state owner/repo`).\n"
            "'.' as the repository means the current Git repository's `origin` remote."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("repo", metavar="owner/repo|.")
    parser.add_argument("--loud", action="store_true",
                        help="also show reviewer tool activity and raw CLI debug output")
    parser.add_argument("--cat", type=misscat._positive_int, default=1, metavar="N",
                        help="review up to N waiting PR HEADs at once in a batch (default: 1)")
    parser.add_argument("-v", "--version", action="version", version=f"%(prog)s {__version__}")
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
    misscat._configure_logging(args.loud)

    try:
        repo = resolve_repo_arg(args.repo)
        with repository_lock(repo):
            ensure_config()
            settings = load_settings()
            for tool in ("git", "gh", "opencode"):
                if shutil.which(tool) is None:
                    raise MissCatError(f"required CLI not found on PATH: {tool}")
            state = State(state_path(repo))
            log.info("watching %s with opencode/%s (config: %s)", repo, settings.model, config_path())
            workspaces = Workspaces(repo, args.cat)
            try:
                workspaces.sync()
            except WorkspaceError as exc:
                log.warning("workspace not ready yet: %s", exc)
            reviewer = OpenCodeReviewer()

            def list_prs(name: str) -> list[PR]:
                reviewer.check()  # a non-retryable runner error ends the watch
                return gh_open_prs(name)

            def sleep(seconds: float) -> None:
                reviewer.check()
                time.sleep(seconds)

            misscat.Watcher(repo, PROFILE, settings, state, list_prs, reviewer, sleep=sleep,
                            cats=args.cat, workspaces=workspaces,
                            discussion=GhDiscussion(repo)).run_forever()
    except MissCatError as exc:
        print(f"mscat: {exc}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        log.info("stopped")
        return 130
    return 0


if __name__ == "__main__":
    sys.exit(main())

"""Thin `gh api` client. Uses the user's existing `gh` login; stores nothing."""
from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass
from typing import Optional

from misscat import GhError

from .protocol import Review

TIMEOUT = 60
MAX_PAGES = 20


NO_REVIEW_LABEL = "no-review"  # exact label: the PR is neither reviewed nor merged


@dataclass(frozen=True)
class OpenPR:
    number: int
    head: str
    draft: bool
    url: str
    title: str
    labels: frozenset[str] = frozenset()


def pr_labels(data: dict) -> frozenset[str]:
    """Exact GitHub PR label names; absent or malformed labels cannot grant batch access."""
    return frozenset(label["name"] for label in (data.get("labels") or [])
                     if isinstance(label, dict) and isinstance(label.get("name"), str))


class TransientGhError(GhError):
    """A merge request whose outcome is unknown or temporarily refused; safe to re-verify and retry."""


def _http_head(stdout: str) -> tuple[Optional[int], dict]:
    """Status code and lower-cased headers from `gh api --include` output (None if no response)."""
    lines = stdout.lstrip().split("\n")
    first = lines[0].split() if lines else []
    if len(first) < 2 or not first[0].startswith("HTTP/") or not first[1].isdigit():
        return None, {}
    headers = {}
    for line in lines[1:]:
        line = line.rstrip("\r")
        if not line:
            break
        name, _, value = line.partition(":")
        headers[name.strip().lower()] = value.strip()
    return int(first[1]), headers


def is_transient(status: Optional[int], headers: dict, returncode: int) -> bool:
    """Retryable merge failure: a 5xx, a rate limit, or a transport failure with no HTTP response.

    Every other HTTP answer (405 draft/branch protection, 409 stale SHA, 422, 403...) is
    deterministic for the PR's current state, and so are `gh` auth/usage failures.
    """
    if status is None:
        return returncode == 1  # gh's generic error: the request never got an HTTP answer
    if status >= 500 or status == 429:
        return True
    return status == 403 and (headers.get("x-ratelimit-remaining") == "0"
                              or "retry-after" in headers)


class GhClient:
    def __init__(self, repo: str):
        self.repo = repo

    def _api(self, path: str, *args: str):
        cmd = ["gh", "api", *args, path]
        try:
            proc = subprocess.run(cmd, capture_output=True, text=True, timeout=TIMEOUT,
                                  stdin=subprocess.DEVNULL)
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise GhError(str(exc)) from exc
        if proc.returncode != 0:
            raise GhError((proc.stderr or proc.stdout).strip() or f"gh exited {proc.returncode}")
        try:
            return json.loads(proc.stdout) if proc.stdout.strip() else {}
        except ValueError as exc:
            raise GhError(f"unexpected gh output: {exc}") from exc

    def _pages(self, path: str, key: str | None = None) -> list:
        items: list = []
        sep = "&" if "?" in path else "?"
        for page in range(1, MAX_PAGES + 1):
            data = self._api(f"{path}{sep}per_page=100&page={page}")
            try:
                chunk = data[key] if key else data
            except (KeyError, TypeError) as exc:
                raise GhError(f"unexpected gh output: {exc}") from exc
            if not isinstance(chunk, list):
                raise GhError("unexpected gh output: expected a list")
            items.extend(chunk)
            if len(chunk) < 100:
                return items
        raise GhError(f"too many pages for {path}")  # fail closed rather than truncate

    def viewer(self) -> str:
        return str(self._api("user")["login"])

    def open_prs(self) -> list:
        try:
            return [OpenPR(int(d["number"]), d["head"]["sha"], bool(d.get("draft")),
                           d["html_url"], d["title"], pr_labels(d))
                    for d in self._pages(f"repos/{self.repo}/pulls?state=open")]
        except (KeyError, TypeError, ValueError) as exc:
            raise GhError(f"unexpected gh output: {exc}") from exc

    def pr(self, number: int) -> dict:
        return self._api(f"repos/{self.repo}/pulls/{number}")

    def reviews(self, number: int) -> list:
        try:
            return [Review(int(d["id"]), ((d.get("user") or {}).get("login") or ""),
                           d.get("state") or "", d.get("body") or "", d.get("commit_id") or "",
                           d.get("submitted_at") or "")
                    for d in self._pages(f"repos/{self.repo}/pulls/{number}/reviews")]
        except (KeyError, TypeError, ValueError) as exc:
            raise GhError(f"unexpected gh output: {exc}") from exc

    def check_runs(self, sha: str) -> list:
        return self._pages(f"repos/{self.repo}/commits/{sha}/check-runs", "check_runs")

    def workflow_runs(self, sha: str) -> list:
        return self._pages(f"repos/{self.repo}/actions/runs?head_sha={sha}", "workflow_runs")

    def combined_status(self, sha: str) -> dict:
        return self._api(f"repos/{self.repo}/commits/{sha}/status")

    def merge(self, number: int, sha: str) -> None:
        """Squash merge guarded by the expected HEAD; GitHub rejects it if the head moved.

        `--include` exposes GitHub's real HTTP status so a temporary failure (TransientGhError)
        is told apart from a deterministic refusal (plain GhError) without parsing stderr text.
        """
        cmd = ["gh", "api", "--include", "-X", "PUT", "-f", "merge_method=squash",
               "-f", f"sha={sha}", f"repos/{self.repo}/pulls/{number}/merge"]
        try:
            proc = subprocess.run(cmd, capture_output=True, text=True, timeout=TIMEOUT,
                                  stdin=subprocess.DEVNULL)
        except subprocess.TimeoutExpired as exc:
            raise TransientGhError(f"merge request timed out after {TIMEOUT}s") from exc
        except OSError as exc:
            raise GhError(str(exc)) from exc
        if proc.returncode == 0:
            return
        status, headers = _http_head(proc.stdout or "")
        message = (proc.stderr or "").strip() or (
            f"HTTP {status}" if status else f"gh exited {proc.returncode}")
        if is_transient(status, headers, proc.returncode):
            raise TransientGhError(message)
        raise GhError(message)

"""Thin `gh api` client. Uses the user's existing `gh` login; stores nothing."""
from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass

from misscat import GhError

from .protocol import Review

TIMEOUT = 60
MAX_PAGES = 20


@dataclass(frozen=True)
class OpenPR:
    number: int
    head: str
    draft: bool
    url: str
    title: str


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
            chunk = data[key] if key else data
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
                           d["html_url"], d["title"])
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

    def combined_status(self, sha: str) -> dict:
        return self._api(f"repos/{self.repo}/commits/{sha}/status")

    def merge(self, number: int, sha: str) -> None:
        """Squash merge guarded by the expected HEAD; GitHub rejects it if the head moved."""
        self._api(f"repos/{self.repo}/pulls/{number}/merge", "-X", "PUT",
                  "-f", "merge_method=squash", "-f", f"sha={sha}")

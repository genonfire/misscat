"""The poll/backoff loop shared by `badcat` and `badcat-host`. Subclasses decide what a PR means."""
from __future__ import annotations

import logging
import time
from typing import Callable, Iterable, Optional

from misscat import GhError

log = logging.getLogger("badcat")

POLL_INTERVAL = 60.0
ERROR_BACKOFF = (60.0, 120.0, 300.0)


class Poller:
    """Lists open PRs each cycle and hands them to `_process`; errors back off and fail closed."""

    def __init__(self, client, sleep: Callable[[float], None] = time.sleep):
        self.client, self.sleep = client, sleep
        self.errors = 0
        self.last_error: Optional[str] = None

    def _process(self, pr) -> None:
        raise NotImplementedError

    def _finished(self, number: int) -> None:
        """A tracked PR is no longer open; a GhError keeps it tracked and is retried."""
        raise NotImplementedError

    def _tracked(self) -> Iterable[int]:
        """PR numbers with remembered state, to detect PRs that disappeared from the open list."""
        raise NotImplementedError

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
            for number in list(self._tracked()):
                if number not in open_numbers:
                    ok = self._guard(number, self._finished, number) and ok
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

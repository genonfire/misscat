"""The machine-readable review protocol. Pure functions; no I/O.

A review authorizes only if its raw body starts with exactly:

    +1|+2|-1
    HEAD: <40 lowercase hex>
    <nonempty evidence>

and it is a submitted COMMENTED review whose commit_id equals the header SHA and the PR's
current HEAD, written by a trusted reviewer. Anything else never authorizes.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Optional

WAITING, PLUS1, BLOCKED, READY, INVALID = "WAITING", "+1", "BLOCKED", "READY", "INVALID"

# GitHub's web editor stores \r\n; the line contents themselves must still match exactly.
HEADER_RE = re.compile(r"\A(\+1|\+2|-1)\r?\nHEAD: ([0-9a-f]{40})\r?\n(.*)\Z", re.DOTALL)
# A body that opens like a verdict but fails the strict header: reported INVALID, never repaired.
LOOSE_RE = re.compile(r"\A\s*[*_`>#-]*\s*(?:\+1|\+2|-1)(?!\d)")


@dataclass(frozen=True)
class Review:
    id: int
    login: str
    state: str
    body: str
    commit_id: str
    submitted_at: str


@dataclass(frozen=True)
class Verdict:
    stage: str
    detail: str = ""


def parse_header(body: Optional[str]) -> Optional[tuple]:
    """(marker, sha, evidence) for a strictly valid header, else None."""
    match = HEADER_RE.match(body or "")
    return match.groups() if match else None


def evaluate(reviews: list, head: str, trusted: frozenset) -> Verdict:
    """Verdict for the current HEAD from the full submitted-review history.

    `trusted` holds lowercase logins; an empty set trusts nobody (fail closed).
    """
    events = []  # markers in submission order; None = invalid review on the current HEAD
    untrusted = []
    for review in sorted(reviews, key=lambda r: (r.submitted_at or "", r.id)):
        if review.state != "COMMENTED" or not review.submitted_at:
            continue
        parsed = parse_header(review.body)
        if parsed is None:
            if review.commit_id == head and LOOSE_RE.match(review.body or ""):
                event = None
            else:
                continue
        else:
            marker, sha, evidence = parsed
            if head not in (sha, review.commit_id):
                continue  # an old HEAD never carries forward
            ok = sha == review.commit_id == head and evidence.strip()
            event = marker if ok else None
        if (review.login or "").lower() not in trusted:
            untrusted.append(review.login or "?")
            continue
        events.append(event)

    if "-1" in events:
        return Verdict(BLOCKED, "-1 on this HEAD")
    if None in events:
        return Verdict(INVALID, "malformed review on this HEAD")
    if "+1" in events:
        first = events.index("+1")
        if "+2" in events[first + 1:]:
            return Verdict(READY)
        return Verdict(PLUS1)
    if "+2" in events:
        return Verdict(INVALID, "+2 without an earlier +1 on this HEAD")
    if untrusted:
        return Verdict(WAITING, "ignored untrusted reviewer(s): " + ", ".join(sorted(set(untrusted))))
    return Verdict(WAITING)

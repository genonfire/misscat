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


NEUTRAL_STATES = ("APPROVED", "DISMISSED", "PENDING")  # never a pass, never a hold


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
    plus1_review_id: Optional[int] = None


def parse_header(body: Optional[str]) -> Optional[tuple]:
    """(marker, sha, evidence) for a strictly valid header, else None."""
    match = HEADER_RE.match(body or "")
    return match.groups() if match else None


def initial_plus1_review_id(reviews: list, head: str, trusted: frozenset) -> Optional[int]:
    """First valid Stage 1 review, used only to baseline legacy HEAD-only delivery state.

    Legacy state cannot identify the delivered review. Suppress just the initial valid
    approval, never infer that later approvals were delivered from a file timestamp.
    """
    for review in sorted(reviews, key=lambda r: (r.submitted_at or "", r.id)):
        if evaluate([review], head, trusted).stage == PLUS1:
            return review.id
    return None


def evaluate(reviews: list, head: str, trusted: frozenset) -> Verdict:
    """Verdict for the current HEAD from the full submitted-review history.

    `trusted` holds lowercase logins; an empty set trusts nobody (fail closed).
    """
    stage, detail, plus1 = WAITING, "", None
    untrusted = set()
    for review in sorted(reviews, key=lambda r: (r.submitted_at or "", r.id)):
        if not review.submitted_at or review.state in NEUTRAL_STATES:
            continue
        parsed = parse_header(review.body)
        if parsed is None:
            if review.commit_id != head:
                continue
            event = None
        else:
            marker, sha, evidence = parsed
            if head not in (sha, review.commit_id):
                continue
            event = marker if (review.state == "COMMENTED" and
                               sha == review.commit_id == head and evidence.strip()) else None
        if (review.login or "").lower() not in trusted:
            untrusted.add(review.login or "?")
            continue
        if event == "-1":
            stage, detail, plus1 = BLOCKED, "-1 on this HEAD", None
        elif event == "+1":
            stage, detail, plus1 = PLUS1, "", review.id
        elif event == "+2" and stage in (PLUS1, READY):
            stage, detail = READY, ""
        elif event == "+2":
            stage, detail, plus1 = INVALID, "+2 without an effective +1 on this HEAD", None
        elif stage != WAITING or LOOSE_RE.match(review.body or ""):
            stage, detail, plus1 = INVALID, "invalid review on this HEAD", None
    if stage == WAITING and untrusted:
        detail = "ignored untrusted reviewer(s): " + ", ".join(sorted(untrusted))
    return Verdict(stage, detail, plus1)

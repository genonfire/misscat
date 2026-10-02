# REVIEW.md

Instructions for AI code reviewers of **MissCat itself**. Review the PR's changed HEAD and its linked issue; use `AGENTS.md` for development invariants.

## Look for blockers
- Missed new PRs/HEADs, unintended duplicate reviews, or failed/interrupted work recorded as completed.
- Silent failures in Git/`gh`/reviewer CLI invocation, permissions, polling, or retry paths.
- Damage to user-owned profiles, review-state JSON, cache, or normal development checkouts.
- Cross-provider configuration leaks (especially `reviewer.args` after merging defaults).
- Installed-package failures: missing bundled resources, broken CLI entry points, or incompatible Python behavior.

## Review discipline
- Trace affected execution paths and failure cases, not just the happy path.
- Distinguish regressions introduced by this PR from existing limitations; report the latter separately.
- Verify relevant tests where possible. Do not claim tests were run if they were not.
- Report actionable blockers with file/line, impact, and a concrete failure scenario.
- If no blockers remain, leave a concise `+1` with the HEAD reviewed. Otherwise, identify the blockers and do not give `+1`.
- Do not edit code, merge PRs, or change review state unless explicitly requested.

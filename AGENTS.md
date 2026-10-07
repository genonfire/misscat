# AGENTS.md

Guidance for coding agents working on MissCat.

## Purpose
MissCat watches a GitHub repository for new pull requests and HEAD commits, then delegates code review to a configured AI CLI. Keep it a small, predictable orchestration tool—not a code editor, review judge, or merge bot.

## Invariants
- Do not modify the user's development checkout. Reviews run only in MissCat's persistent `cat-N` worktrees under `~/.cache/misscat/cats/<owner>/<repo>/`. The control clone under `~/.cache/misscat/repos/<owner>/<repo>/` is never a reviewer workspace; it stays clean on the default branch, and only the main process touches it and the shared Git metadata.
- Track completed reviews per repository, PR, HEAD SHA, and profile. Do not mark failed or interrupted runs as reviewed.
- Preserve user-owned profiles. The intentional, documented one-time State v1 → v2 reset discards legacy review history; outside that explicit reset or a confirmed user deletion, never erase review history.
- Keep configuration, review state, and workspaces separate. Profile initialization manages profiles only, never state JSON or cached repositories.
- Maintain provider independence: Claude, Codex, and Gemini have different CLI arguments. Do not leak one provider's defaults into another.
- Keep the existing authentication boundary: use the user's `git`, `gh`, and reviewer CLI credentials; do not store new tokens or secrets.

## BadCat
`src/badcat/` is a second CLI in the same package and release. It is a deterministic review-state watcher, not an AI reviewer, and the only component that may write to GitHub (squash merge, only with `--merge`). The MissCat invariants above stay true for MissCat: BadCat has its own state (`~/.config/badcat/`), lock (`~/.cache/badcat/locks/`), no workspace, and never touches MissCat's. Its default mode must perform no GitHub writes, a merge must re-read GitHub and pass every gate with a HEAD-SHA guard, and cached state must never authorize a merge.

`src/badcat_host/` (`badcat-host`) is a separate, read-only Chrome Native Messaging host with its own poll loop and state (`~/.config/badcat-host/`). It imports only `badcat.github` and `badcat.protocol` — never `badcat.cli` or any merge code — and BadCat must not depend on it. It must never write to GitHub, never touch stdout except with protocol frames, accept only the minimal start/stop protocol, and never log or expose credentials or `gh` error text. The Chrome extension is not part of the wheel.

## Changes
- Prefer small, explicit changes over abstractions or new dependencies.
- Preserve existing CLI behavior unless the issue explicitly changes it.
- Use packaged resources for bundled configuration; do not depend on the working directory or source checkout.
- Add focused regression tests for changed behavior, including error/retry paths. For packaging changes, verify the built wheel contains the required files.
- Update README/help text when user-facing commands, defaults, or configuration change.

`README.md` is the user guide. A target repository's `REVIEW.md` defines that repository's reviewer instructions; this file governs development of MissCat itself.

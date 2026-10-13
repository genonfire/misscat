# MissCat
[![PyPI](https://img.shields.io/pypi/v/misscat.svg)](https://pypi.org/project/misscat/)
[![PyPI Downloads](https://static.pepy.tech/personalized-badge/misscat?period=total&units=INTERNATIONAL_SYSTEM&left_color=BLACK&right_color=GREEN&left_text=downloads)](https://pepy.tech/projects/misscat)

**Never miss a single commit.**

> *I’ll catch ’em all, meow.*

MissCat is a small CLI that watches a GitHub repository and automatically runs an AI code review whenever a new pull request or commit appears.

It watches the **repository**, not a single PR.

## Why MissCat?

AI reviewers are useful, but somebody still has to notice that a PR changed and ask them to review it again.

MissCat does that part.

```text
new PR
  → review

new commit
  → review again

nothing changed
  → do nothing
```

No webhook server.  
No API keys stored by MissCat.  
No repeated review of the same commit with the same profile.

## Prerequisites

Before running MissCat, ensure you have:

- **Python**: Python 3.9 or newer
- **Git**: Configured with credentials to clone and fetch the target repository (HTTPS users using GitHub CLI can run `gh auth setup-git`)
- **GitHub CLI (`gh`)**: Installed and authenticated (`gh auth login`)
- **At least one AI reviewer CLI**: Installed and authenticated:
  - [Claude Code](https://docs.anthropic.com/en/docs/agents-and-tools/claude-code) (`claude`)
  - [Codex CLI](https://github.com/openai/codex) (`codex`)
  - [Antigravity CLI](https://github.com/google/antigravity) (`agy`) for Gemini

## Installation

Install MissCat with [pipx](https://pypa.github.io/pipx/):

```bash
pipx install misscat
```

To upgrade MissCat to the latest version:

```bash
pipx upgrade misscat --pip-args="--no-cache-dir"
```

## Releasing (maintainers)

Releases are published to PyPI by `.github/workflows/publish.yml` when a `vMAJOR.MINOR.PATCH` tag is pushed. It uses PyPI [Trusted Publishing](https://docs.pypi.org/trusted-publishers/) (OIDC), so no PyPI token is stored in GitHub secrets.

### One-time setup

1. On PyPI, add a trusted publisher for the `misscat` project (Project → Publishing; for a first release use "Add a pending publisher"):
   - Owner: `genonfire`
   - Repository: `misscat`
   - Workflow name: `publish.yml`
   - Environment name: `pypi`
2. In the GitHub repository, create an environment named `pypi` (Settings → Environments). Recommended: add required reviewers so every release needs a manual approval, and add a tag ruleset (Settings → Rules) so only maintainers can create `v*` tags. A deployment tag rule alone is not enough, since it matches the tag name, not the commit.

### Release procedure

1. Check that the version is not already on PyPI (versions are immutable and cannot be re-uploaded): <https://pypi.org/project/misscat/#history>
2. Bump `version` in `pyproject.toml` **and** `__version__` in `src/misscat/__init__.py` (the workflow fails if they differ), then merge to `master`.
3. Tag the merge commit and push the tag (the tag must equal `v` + the `pyproject.toml` version):

   ```bash
   git tag v1.0.1
   git push origin v1.0.1
   ```

4. The workflow fails before publishing if the tagged commit is not on `master`, the tag is malformed, the tag does not match `pyproject.toml` or `__version__`, or `python -m build` / `twine check` fails.
5. Confirm the release:

   ```bash
   pipx upgrade misscat
   misscat --version
   pip index versions misscat
   ```

## Quick Start / First Run

On the first normal run, if `~/.config/misscat/` does not exist, MissCat creates it and copies the bundled reviewer profiles (`luna.yml`, `sol.yml`, `sonnet.yml`, `opus.yml`, `gemini.yml`) into it before loading your profile. Edit them freely. If the directory already exists, nothing is copied automatically, so existing configs are preserved across upgrades. `--help` and `--version` never touch the filesystem.

Watch a repository and review new PRs using the default profile:

```bash
misscat <owner/repo>
```

Example:

```bash
misscat genonfire/misscat
```

Use a specific reviewer profile:

```bash
misscat genonfire/misscat sol
```

MissCat resolves `sol` as:

```text
~/.config/misscat/sol.yml
```

Running `misscat` with no arguments (or `misscat --help`) shows usage help:

```bash
misscat --help
```

Repository names are canonicalized to lowercase, so `Genonfire/MissCat` and `genonfire/misscat` refer to the same local workspace and state.

### Using `.` for the current repository

`.` **always means the GitHub repository of the current Git checkout's `origin` remote**; `upstream` and other remotes are never used.

```bash
misscat .           # watch origin's repository with the default profile
misscat . luna      # same repository, luna profile
misscat state .     # manage that repository's review state
```

MissCat prints `resolved . -> owner/repo` and then behaves exactly as if you had typed that name (same state, lock and workspace). It works from subdirectories and worktrees, and understands `git@github.com:owner/repo.git`, `https://github.com/owner/repo(.git)` and `ssh://git@github.com/owner/repo(.git)`. It fails, before touching any state, when outside a Git repository, when `origin` is missing, or when `origin` is not on `github.com` (SSH host aliases are not interpreted).

## Bundled profiles: `misscat init`

```bash
misscat init          # create the config dir if needed; copy only missing bundled profiles
misscat init --force  # overwrite files named like bundled profiles with fresh copies
```

- `init` never modifies existing files. `init --force` replaces only `luna.yml`, `sol.yml`, `sonnet.yml`, `opus.yml` and `gemini.yml`; custom profiles, per-repository state JSONs and the review cache are never touched.
- After upgrading MissCat, new or updated bundled profiles are not applied automatically: run `misscat init` for new ones, or `misscat init --force` to reset the bundled ones (this discards your edits to them).
- If the automatic first-run copy is interrupted, `misscat init` restores the missing profiles.
- `default.yml` is the internal base configuration and is not copied. Each bundled profile carries its own editable copy of the default `prompt`, so you can customize a reviewer's prompt in place; `default.yml` remains the fallback.

## Review state manager

Stop the watcher before opening the terminal state manager:

```bash
misscat state genonfire/typewriter
```

The list shows the most recently completed reviews first, across all PRs and profiles (sorted by `reviewed_at`; the stored State order is unchanged). Use **↑/↓** to select any completed review, **Enter** for its full HEAD SHA, profile, and local review completion time, and **Esc/Q** to exit. **Delete** or **Backspace** only operates on the *most recently reviewed entry for that PR and profile* (by `reviewed_at`), with a `[y/N]` prompt. Older entries are read-only until newer entries in that PR/profile group have been removed. Nothing is deleted on Enter or when you decline confirmation.

The manager works from local State JSON; it does not call GitHub. Removing a record makes that PR/HEAD/profile combination eligible for review again, **but only when the HEAD is the PR's current open HEAD** at the next watcher run. Removing historical HEADs does not trigger review of historical commits.

### State v2 and process safety

- Every successful review now stores `reviewed_at` as an ISO 8601 UTC timestamp. Display uses local time. Existing PR/HEAD/profile identity and the JSON sorting convention remain unchanged.
- **Breaking state reset:** **stop all previously running MissCat watchers before upgrading to this version**; older watchers do not participate in the new OS locking scheme and may restore v1 records from memory. On first State access after upgrade, any **v1** reviewed history is intentionally discarded and the file is rewritten as an empty **v2** State. Consequently, open PR HEADs previously marked complete become eligible for a new AI review. This may consume additional reviewer tokens. Unknown versions and corrupt State files cause errors instead of being discarded.
- A per-repository OS file lock prevents a watcher and a state manager—or two watchers—from running on the same repository simultaneously, even with different profiles. Stop the watcher before editing state. The lock file lives under `~/.cache/misscat/locks/` (never in the config directory) and may remain on disk after exit; the OS releases its lock when the process exits. Do not use the file's existence as an indication of an active watcher.
- State manager needs an interactive TTY. It does not touch profile YAML files or cache workspaces.

## Configuration

MissCat ships with a bundled `default.yml` inside the package, so installed copies do not depend on the source checkout or current working directory.

User configuration and persistent data are kept entirely outside the package:

- **Profiles**: `~/.config/misscat/<profile>.yml`
- **Per-repository state**: `~/.config/misscat/<owner>__<repo>.json`
- **Persistent workspace**: `~/.cache/misscat/repos/<owner>/<repo>/`

Directory layout:

```text
~/.config/misscat/
├── sol.yml
├── sonnet.yml
├── opus.yml
├── genonfire__typewriter.json
└── genonfire__misscat.json
```

Example `default.yml`:

```yaml
reviewer:
  # provider: claude
  # model: claude-sonnet-5-5
  # args:
  #   - --allowedTools
  #   - "Bash(gh *)"

  provider: codex
  model: gpt-6-luna
  args:
    - --sandbox
    - workspace-write
    - -c
    - sandbox_workspace_write.network_access=true
    - -c
    - apps._default.enabled=false
    - -c
    - model_reasoning_effort=max

prompt: |
  Read REVIEW.md if exist and act as the 1st reviewer.
  Use the gh CLI for GitHub operations, including posting the review.

watch:
  idle: [60, 120, 180, 240, 300]
  active: [300, 240, 180, 120, 60]

review:
  include_drafts: true
```

Example `~/.config/misscat/sol.yml`:

```yaml
reviewer:
  provider: codex
  model: gpt-6.1-sol
  args:
    - --sandbox
    - workspace-write
    - -c
    - sandbox_workspace_write.network_access=true
    - -c
    - apps._default.enabled=false
    - -c
    - model_reasoning_effort=medium
```

The selected local profile overrides the bundled defaults.

When creating a local profile, use `default.yml` as the reference and specify `provider`, `model`, and `args` explicitly for that reviewer CLI. Do not rely on `args` inherited from a different provider.

The `args` field under `reviewer` is passed to the selected reviewer CLI unchanged.

Reviewed state is stored per repository and keyed by PR, HEAD SHA, and profile. The same HEAD can therefore be reviewed again with a different profile.

### Gemini (Antigravity CLI)

Gemini reviews use the Antigravity CLI (`agy`). Configure authentication and permissions before running MissCat.

For headless reviews, configure the Antigravity CLI settings file:

`~/.gemini/antigravity-cli/settings.json`

```json
{
  "toolPermission": "proceed-in-sandbox",
  "enableTerminalSandbox": true,
  "permissions": {
    "allow": [
      "command(gh)",
      "command(git)"
    ]
  }
}
```

Preserve any existing settings when adding these fields. Ensure the repository workspace is trusted as appropriate.

Example reviewer profile (`~/.config/misscat/gemini.yml`):

```yaml
reviewer:
  provider: gemini
  model: gemini-3.8-flash-low
  args:
    - --print-timeout
    - 30m
```

**Important:** In headless mode, commands requiring interactive permission approval may be automatically denied. Antigravity can still exit successfully without posting a GitHub review.

If a review finishes without appearing on GitHub, run MissCat with `--loud` to inspect reviewer activity and permission errors.

Avoid `--dangerously-skip-permissions` for routine unattended operation, as it broadly bypasses tool approval checks.

## Review workspace

MissCat keeps one persistent **control clone** per repository and one persistent Git worktree per cat:

```text
~/.cache/misscat/repos/<owner>/<repo>/        control clone (default branch HEAD, never reviewed in)
~/.cache/misscat/cats/<owner>/<repo>/cat-1/   review worktrees: cat-1, cat-2, ...
```

The repository is cloned on first use and reused. At startup and before each batch MissCat fetches the control clone, leaves it clean on the default branch, and creates any missing `cat-N` worktrees. Existing cats are reused across runs; a later run with a smaller `--cat` leaves the extra cats on disk, unused. Before each review, MissCat cleans a cat's worktree and detaches it at the exact PR HEAD. The reviewer CLI runs with that cat worktree as its working directory.

Your normal development checkout is never touched.

## Requirements and limits

- **Authentication expectations**:
  - Git must be able to authenticate to the target repository for clone and fetch. Existing SSH keys or Git credentials work seamlessly. If using HTTPS with GitHub CLI, configure Git helper via `gh auth setup-git`. MissCat never stores Git credentials.
  - GitHub CLI (`gh`) must be authenticated (`gh auth login`) with permissions to query pull requests.
  - Reviewer CLIs (`claude`, `codex`, `agy`) must be logged in and configured with their respective provider accounts or API keys. MissCat uses their existing credentials.
- **Trusted repository and PR warning**:
  - Reviewer CLIs execute within a MissCat-managed checkout of PR-controlled files.
  - MissCat should only be used with repositories and pull requests whose code and contributors you trust.
- **One process per repository**:
  - Run at most one MissCat process per repository at any given time.
  - Multiple MissCat instances or profiles pointing to the same repository would share and conflict over the same persistent workspace (`~/.cache/misscat/repos/<owner>/<repo>/` and `~/.cache/misscat/cats/<owner>/<repo>/`).

## Reviewer backends

MissCat is not tied to a specific AI reviewer.

Initial backends:

- Claude Code CLI
- Codex CLI
- Gemini via agy(Google Antigravity CLI)

MissCat runs the selected reviewer CLI from the root of the prepared PR checkout. The CLI can therefore discover and apply its own repository instructions, such as `CLAUDE.md` or `AGENTS.md`, while `REVIEW.md` defines the review behavior requested by MissCat.

MissCat does not parse or translate those instruction files.

MissCat uses the authentication already configured in the reviewer CLI.
The `args` field under `reviewer` can be used for CLI-specific execution options such as tool permissions, sandbox settings, or network access.

GitHub repository and pull request access is handled through the authenticated GitHub CLI (`gh`).

MissCat does not manage GitHub tokens, Git credentials, or AI API keys itself.

## Adaptive polling

When there are no open PRs, MissCat gradually becomes lazy:

```text
1m → 2m → 3m → 4m → 5m → 5m ...
```

After review work is complete, MissCat gives the author some time to make changes, then gets increasingly impatient:

```text
5m → 4m → 3m → 2m → 1m → 1m ...
```

By default MissCat runs one review at a time.

When a review finishes successfully, it immediately checks the repository again before sleeping. If another PR or new HEAD is waiting, it reviews that next.

If a review or workspace preparation fails, that HEAD is not marked reviewed. When every review of the batch failed (with the default single review, that one), MissCat waits 5 minutes before checking again; if at least one review succeeded, it re-lists PRs immediately (see `--cat` below).

### Backlog bursts: `--cat=N`

```bash
misscat . luna --cat=3
```

When several reviews pile up, MissCat can briefly call the neighbor cats for help. `--cat=N` is the maximum number of reviews in one **batch**, not a permanently busy worker pool; `--cat=1` (the default) is the single-review behavior described above.

On each fresh PR listing MissCat takes up to N waiting PR HEADs (same eligibility and oldest-first order), prepares one worktree per PR, runs the reviewers concurrently and **waits for the whole batch to finish**. A cat that finishes early does not pick up another PR. Successful reviews are then recorded in the state file by the main process, and PRs are listed again immediately. PRs labeled exactly `urgent` are selected first (then normal PRs fill the remaining slots). PRs labeled exactly `no-review` are never selected (even when also `urgent`), do not use a cat slot, and are not recorded in the state file; removing the label makes the current HEAD eligible again. Labels are case-sensitive, read from the same PR listing, and only affect the next batch selection, never a batch already running. Within urgent and normal PRs alike, the order is oldest-first with previously failed HEADs last. A failed review never discards the successes of its batch; failed HEADs stay eligible and go last in the next batch. If every review of a batch fails, MissCat waits 5 minutes.

With `--cat` above 1, log lines carry the cat name (`cat-1`, ...). Each cat runs a full reviewer, so tokens and rate limits are used in parallel.

## BadCat: review-state watcher and opt-in merge

`pipx install misscat` also installs **BadCat**, a second CLI from the same package and version. BadCat is not an AI reviewer: it deterministically watches *submitted GitHub PR reviews*, reports state transitions, and squash-merges only when started with `--merge`.

```bash
misscat owner/repo luna     # AI review (unchanged)
badcat owner/repo           # DEFAULT: monitor and report only; NO GitHub writes
badcat owner/repo --merge   # +1 then +2; batch-labeled PRs merge at +1
badcat . --merge=1          # merge after valid +1 (all PRs)
badcat . --merge=2          # same as --merge; batch PRs merge at +1
badcat . --merge --trusted-reviewer luna
```

- `.` resolves to the current repository's `origin` exactly as in MissCat.
- Omitting `--merge` always means **notify only**, with no GitHub writes.
  A bare `--merge` is an alias for `--merge=2`.
  `--merge=1` authorizes any eligible PR at a valid `+1`.
  `--merge=2` normally requires `+1` then `+2`, but an exactly `batch`-labeled
  PR may merge at `+1` (`urgent` takes precedence if both labels are present).
  Labels are checked again immediately before the merge. Only `1` and `2`
  are accepted. This changes the review threshold,
  **not** the required CI/mergeability checks or final HEAD protection.
- All **open** PRs are watched, Draft or Ready alike. BadCat never reads, changes or decides from Draft status; if GitHub rejects a merge of a Draft PR, the rejection is reported and BadCat keeps watching.
- No AI calls, no checkout, no clone. It uses your existing `gh` login (`gh auth login`); no tokens are stored. `--merge` needs a `gh` account with permission to merge.
- Output reports transitions only, not repeated snapshots: new PR, new HEAD, each newly submitted review, CI/merge waits and readiness, merged or closed, and API errors. Each new review is announced as one line carrying its **literal first line**, whatever it says (`PR #290: +1`, `PR #292: ## 1차 리뷰 — 변경 요청`); no `INVALID` label or explanation is added to a nonconforming review. What is printed is separate from the strict validation below. When BadCat sees a PR for the first time it records the existing reviews silently and announces only later ones. Polling is every 60 seconds, backing off to 5 minutes on errors.

```text
10:00:00 INFO Watching owner/repo (notify only; trusted reviewers: luna)
10:01:12 INFO PR #290: +1
10:03:25 INFO PR #290: -1
10:10:42 INFO PR #290: new HEAD (e4f5678)
10:15:08 INFO PR #290: +2
10:15:09 INFO PR #290: ready to merge (notify only)
```

### Review header

Only a **submitted review** (GitHub state `COMMENTED`; not issue comments, inline-only comments or APPROVE) counts, and only if its raw body starts with exactly:

```text
+1
HEAD: <40 lowercase hex characters>

<review evidence>
```

The first line is exactly `+1`, `+2` or `-1`: no leading blank or whitespace, decoration or explanation (`-1 : FAIL (blocker found)` is printed as-is but never authorizes). The second line is `HEAD: <sha>` immediately after. Evidence must follow. The header SHA, the review's `commit_id` and the PR's current HEAD must all match, so reviews of an old HEAD never carry forward.

BadCat reduces submitted, trusted reviews chronologically (review ID breaks timestamp ties). A valid `-1` blocks and resets the approval sequence. A later valid `+1` clears the older blocker and starts a fresh Stage 1 pass; it also resets any earlier Stage 2 authorization. A valid `+2` authorizes Stage 2 only after an effective `+1` in this sequence. Thus `+1 → +2 → -1 → +1` is only `+1`, while `-1 → +1 → +2` is `READY`. Older verdicts remain in GitHub's audit history but no longer authorize or veto the current sequence.

With `--merge` / `--merge=2`, the effective two-stage sequence is required except for `batch` PRs (effective `+1` suffices unless also labeled `urgent`); `--merge=1` accepts effective `+1` or `READY`. A latest trusted malformed/contradictory marker or non-pass review holds the HEAD until a new valid `+1` starts a clean sequence. `+2` alone never authorizes. `APPROVED`, `DISMISSED` and unsubmitted reviews neither pass nor hold; non-`COMMENTED` states never authorize. Reviews on an old HEAD do not carry forward.

### Trust

Only reviews by trusted logins count (fail closed). Use `--trusted-reviewer LOGIN` (repeatable); the default is the authenticated `gh` user. Stage 1 and Stage 2 may share one account.

### Merge gate (`--merge`, `--merge=1` or `--merge=2` only)

Immediately before the only write, BadCat re-reads the PR HEAD and all reviews, re-evaluates the chosen `+1`/`+2` threshold and blocks any new `-1` or malformed review, then requires:

- CI, discovered automatically from GitHub for the exact HEAD, with no check name to configure: every reported check run, workflow run and commit status must be complete and passing (pending, failed, cancelled, timed out, action-required, stale or unreadable CI fails closed), and at least one check run created by **GitHub Actions** must have *succeeded*. Check names are repository-defined and never matched (`Validate and test Typewriter`, `unittest (3.12)`...). Skipped runs, other apps' checks and plain commit statuses are not evidence that tests ran, so a HEAD with no CI, only skipped CI, or only an unrelated green check is never merged. A successful workflow that deliberately skips work (for example a documentation-only change) counts as normal success. CI is re-read immediately before the merge, and a re-run on the same HEAD is picked up on the next poll.
  - *Limitation:* the API cannot tell which checks a repository *requires*, so BadCat cannot notice a required check that never started if other Actions checks passed. Enable branch protection or rulesets for required checks: GitHub then reports the PR as not mergeable (`mergeable_state` other than clean) and rejects the merge, both of which BadCat honors,
- GitHub to report the PR mergeable with a clean `mergeable_state` (so branch protection is honored).

It then sends a **squash** merge with the expected HEAD SHA, so GitHub rejects it if the HEAD moved. There is no fallback to an unguarded merge. A rejection is logged once and not repeated until the PR's state changes; already merged or closed PRs are handled quietly. Disable any other automatic merger for the repository when using `--merge`.

### State and process safety

BadCat keeps its own notification-dedupe state in `~/.config/badcat/` and its own lock in `~/.cache/badcat/locks/`, separate from MissCat's. State never authorizes a merge (GitHub is always re-read), and unreadable state is ignored. One BadCat process per repository; MissCat and BadCat can run on the same repository at the same time.

## What MissCat does

- Watches all open PRs in a repository
- Discovers PRs created after MissCat starts
- Detects changes by PR HEAD SHA
- Reviews every new PR HEAD once per profile
- Avoids duplicate reviews
- Remembers reviewed commits across restarts
- Uses an isolated persistent review workspace
- Supports multiple reviewer backends

MissCat does **not** modify code, push commits, or merge PRs.

It watches. It catches. It reviews.

(BadCat is the separate, opt-in merge operator described above.)

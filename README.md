# MissCat
[![PyPI](https://img.shields.io/pypi/v/misscat.svg)](https://pypi.org/project/misscat/)
[![PyPI Downloads](https://static.pepy.tech/personalized-badge/misscat?period=total&units=INTERNATIONAL_SYSTEM&left_color=BLACK&right_color=GREEN&left_text=downloads)](https://pepy.tech/projects/misscat)

**Never miss a single commit.**

> *I’ll catch ’em all, meow.*

MissCat is a small CLI that watches a GitHub repository and runs an AI code review whenever a new pull request or HEAD commit appears. Each PR HEAD is reviewed once per profile. It does not modify code, push commits, or merge PRs.

## Prerequisites

- Python 3.9+
- `git` with credentials that can clone and fetch the target repository (HTTPS users: `gh auth setup-git`)
- GitHub CLI `gh`, authenticated (`gh auth login`)
- At least one authenticated reviewer CLI: [Claude Code](https://docs.anthropic.com/en/docs/agents-and-tools/claude-code) (`claude`), [Codex CLI](https://github.com/openai/codex) (`codex`) or [Antigravity CLI](https://github.com/google/antigravity) (`agy`, for Gemini)

MissCat stores no tokens or keys; it uses the credentials of `git`, `gh` and the reviewer CLI.

## Installation

```bash
pipx install misscat
pipx upgrade misscat --pip-args="--no-cache-dir"   # upgrade
```

## Usage

```bash
misscat <owner/repo>             # default profile
misscat <owner/repo> sol         # profile ~/.config/misscat/sol.yml
misscat . luna                   # `.` = the repository of the current checkout's origin remote
misscat . luna --cat=3           # up to 3 reviews in parallel per batch
misscat . luna --loud            # show reviewer activity
misscat init [--force]           # create config dir / copy bundled profiles (--force overwrites them)
misscat state <owner/repo>       # terminal review-state manager (stop the watcher first)
```

On the first run `~/.config/misscat/` is created with the bundled profiles (`luna`, `sol`, `sonnet`, `opus`, `gemini`). Existing files are never overwritten except by `init --force`.

Run one MissCat process per repository.

PRs labeled `urgent` are reviewed first; PRs labeled `no-review` are skipped.

## Configuration

- Profiles: `~/.config/misscat/<profile>.yml`
- Review state: `~/.config/misscat/<owner>__<repo>.json`
- Workspaces: `~/.cache/misscat/` (your own checkout is never touched)

A profile overrides the bundled defaults. Always set `provider`, `model` and `args` together; `args` is passed to the reviewer CLI unchanged.

```yaml
reviewer:
  provider: codex        # claude | codex | gemini
  model: gpt-6.1-sol
  args:
    - --sandbox
    - workspace-write
    - -c
    - sandbox_workspace_write.network_access=true

prompt: |
  Read REVIEW.md if exist and act as the 1st reviewer.
  Use the gh CLI for GitHub operations, including posting the review.

watch:
  idle: [60, 120, 180, 240, 300]   # seconds, when no PR is open
  active: [300, 240, 180, 120, 60] # seconds, after a review
```

The reviewer runs inside the PR checkout, so a repository's `REVIEW.md`, `CLAUDE.md` or `AGENTS.md` apply. Use MissCat only with repositories and PRs you trust.

### Gemini (Antigravity CLI)

Allow `gh` and `git` in `~/.gemini/antigravity-cli/settings.json` (keep your other settings):

```json
{
  "toolPermission": "proceed-in-sandbox",
  "enableTerminalSandbox": true,
  "permissions": { "allow": ["command(gh)", "command(git)"] }
}
```

```yaml
reviewer:
  provider: gemini
  model: gemini-3.8-flash-low
  args: [--print-timeout, 30m]
```

In headless mode a denied permission can end the run without posting a review; use `--loud` to inspect.

## BadCat

`pipx install misscat` also installs `badcat`, a deterministic watcher of submitted PR reviews (no AI). By default it only reports; it merges (squash) only with `--merge`.

```bash
badcat owner/repo                          # report only, no GitHub writes
badcat . --merge                           # merge after +1 then +2 (batch-labeled PRs at +1)
badcat . --merge=1                         # merge after +1
badcat . --merge --trusted-reviewer luna   # trust reviews by this login (default: your gh user)
```

A review counts only if it is a submitted `COMMENTED` review by a trusted login whose body starts with:

```text
+1
HEAD: <40-hex sha of the PR's current HEAD>

<review evidence>
```

The first line is `+1`, `+2` or `-1`. A later valid `-1` blocks; a new `+1` starts a fresh sequence. Before merging, BadCat re-reads GitHub and requires passing CI (including at least one succeeded GitHub Actions check) and a clean mergeable state, and merges only if the HEAD is unchanged. Use branch protection for required checks, and disable other auto-mergers. Run one BadCat process per repository.

# MissCat

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

## Usage

```bash
misscat owner/repository
```

Example:

```bash
misscat genonfire/misscat
```

Use a local reviewer profile:

```bash
misscat genonfire/misscat luna
```

MissCat resolves `luna` as:

```text
~/.config/misscat/luna.yml
```

Running `misscat` with no arguments shows usage help.

Repository names are canonicalized to lowercase, so `Genonfire/MissCat` and `genonfire/misscat` refer to the same local workspace and state.

## Configuration

MissCat ships with a bundled `default.yml`.

Local profiles and per-repository review state live in:

```text
~/.config/misscat/
├── luna.yml
├── sonnet.yml
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

prompt: |
  Read REVIEW.md if exist and act as the 1st reviewer.
  Use the gh CLI for GitHub operations, including posting the review.

watch:
  idle: [60, 120, 180, 240, 300]
  active: [300, 240, 180, 120, 60]

review:
  include_drafts: true
```

Example `~/.config/misscat/luna.yml`:

```yaml
reviewer:
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
```

The selected local profile overrides the bundled defaults.

When creating a local profile, use `default.yml` as the reference and specify `provider`, `model`, and `args` explicitly for that reviewer CLI. Do not rely on `args` inherited from a different provider.

The `args` field under `reviewer` is passed to the selected reviewer CLI unchanged.

Reviewed state is stored per repository and keyed by PR, HEAD SHA, and profile. The same HEAD can therefore be reviewed again with a different profile.

## Review workspace

MissCat reviews each repository in its own persistent workspace:

```text
~/.cache/misscat/repos/<owner>/<repo>/
```

The repository is cloned on first use and reused for later reviews. Before each review, MissCat prepares a clean checkout of the exact PR HEAD.

Your normal development checkout is never touched.

## Requirements and limits

- Git must be able to authenticate for clone and fetch. HTTPS users relying on GitHub CLI authentication should run `gh auth setup-git`.
- Reviewer CLIs run inside PR-controlled checkouts. Use MissCat only with repositories and pull requests you trust.
- Run at most one MissCat process per repository.

## Reviewer backends

MissCat is not tied to a specific AI reviewer.

Initial backends:

- Claude Code CLI
- Codex CLI

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

MissCat runs one review at a time.

When a review finishes successfully, it immediately checks the repository again before sleeping. If another PR or new HEAD is waiting, it reviews that next.

If a review or workspace preparation fails, that HEAD is not marked reviewed and MissCat waits 5 minutes before checking again.

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

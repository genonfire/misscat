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
No repeated review of the same commit.

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
misscat genonfire/misscat sol.yml
```

## Configuration

The repository contains a committed `default.yml`.

Local profiles live in `.config/`, which should be ignored by Git.

```text
misscat/
├── default.yml
└── .config/
    └── sol.yml
```

Example `default.yml`:

```yaml
reviewer:
  provider: claude
  model: sonnet

prompt: |
  Read REVIEW.md and act as the first reviewer.

watch:
  idle: [60, 120, 180, 240, 300]
  active: [300, 240, 180, 120, 60]

review:
  include_drafts: false
```

Example `.config/sol.yml`:

```yaml
reviewer:
  provider: codex
  model: gpt-6-sol
```

The selected local profile overrides `default.yml`.

## Reviewer backends

MissCat is not tied to a specific AI reviewer.

Initial backends:

- Claude Code CLI
- Codex CLI

MissCat uses the authentication already configured in those CLIs.

GitHub repository and pull request access is handled through the authenticated GitHub CLI (`gh`).

MissCat does not manage GitHub tokens or AI API keys itself.

## Adaptive polling

When there are no open PRs, MissCat gradually becomes lazy:

```text
1m → 2m → 3m → 4m → 5m → 5m ...
```

After reviewing a PR, MissCat gives the author some time to make changes, then gets increasingly impatient:

```text
5m → 4m → 3m → 2m → 1m → 1m ...
```

When a new commit is detected and reviewed, the cycle starts again.

## What MissCat does

- Watches all open PRs in a repository
- Discovers PRs created after MissCat starts
- Detects changes by PR HEAD SHA
- Reviews every new PR HEAD once
- Avoids duplicate reviews
- Remembers reviewed commits across restarts
- Supports multiple reviewer backends

MissCat does **not** modify code, push commits, or merge PRs.

It watches. It catches. It reviews.

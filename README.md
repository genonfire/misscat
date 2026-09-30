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
misscat genonfire/misscat sol
```

MissCat resolves `sol` as:

```text
~/.config/misscat/sol.yml
```

Running `misscat` with no arguments shows usage help.

Repository names are canonicalized to lowercase for local workspace and state identity, so these refer to the same repository:

```text
Genonfire/MissCat
genonfire/misscat
```

## Configuration

MissCat ships with a bundled `default.yml`.

Local profiles and per-repository review state live in:

```text
~/.config/misscat/
├── sol.yml
├── sonnet.yml
├── genonfire__typewriter.json
└── genonfire__misscat.json
```

A repository such as:

```text
genonfire/typewriter
```

uses:

```text
~/.config/misscat/genonfire__typewriter.json
```

Review state is tracked by:

```text
PR + HEAD SHA + profile
```

The same HEAD is reviewed only once with the same profile, but may be reviewed again with a different profile.

The old global `~/.config/misscat/state.json` is not used.

Example `default.yml`:

```yaml
reviewer:
  provider: claude
  model: claude-sonnet-5-5

prompt: |
  Read REVIEW.md and act as the first reviewer.

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
  model: gpt-6-sol
```

The selected local profile overrides the bundled defaults.

## Review workspace

MissCat does not review from your normal development checkout.

Each repository gets its own persistent workspace:

```text
~/.cache/misscat/repos/
└── <owner>/
    └── <repo>/
```

For example:

```text
~/.cache/misscat/repos/genonfire/typewriter/
```

The repository is cloned only on first use. Later reviews reuse the same workspace.

Before every review, MissCat:

```text
fetches the PR HEAD
→ removes leftovers from the previous review
→ checks out the fetched commit in detached mode
→ verifies the exact HEAD SHA
→ runs the reviewer
```

Fork PRs are fetched through GitHub's PR ref.

Your normal working tree is never modified, so another tool may edit files or switch branches there without affecting the review.

## Authentication

MissCat uses the authentication already configured for Git, GitHub CLI, and the selected reviewer CLI.

GitHub repository discovery uses `gh`.

Persistent review workspaces use Git for clone and fetch operations, so Git must also be able to authenticate to the repository.

If you use HTTPS and rely on GitHub CLI authentication, run:

```bash
gh auth setup-git
```

MissCat does not store Git credentials, GitHub tokens, or AI API keys itself.

## Reviewer backends

MissCat is not tied to a specific AI reviewer.

Initial backends:

- Claude Code CLI
- Codex CLI

Reviewer CLIs run inside the isolated checkout of the exact PR HEAD.

The review behavior itself is defined by your prompt and repository instructions such as `REVIEW.md`. MissCat does not interpret review results or decide what counts as an approval or blocker.

MissCat considers the review successful when the reviewer CLI exits successfully.

## Trust boundary

Reviewer CLIs run inside a checkout containing files controlled by the pull request.

Use MissCat only with repositories and pull requests whose contents you trust.

MissCat does not add a separate permission or fork-blocking layer.

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

The adaptive timer starts only when there is nothing left to review.

If a review or workspace preparation fails, that HEAD is not marked reviewed and MissCat waits 5 minutes before checking again.

## One cat per repository

MissCat is intentionally single-threaded.

Run at most **one MissCat process per repository**.

Different repositories may run independently, but running the same repository simultaneously with multiple profiles is not supported because they would share the same review workspace and state.

```text
repository
├── one MissCat process
├── one review workspace
└── one state file
```

One cat, one repository, one review at a time.

## What MissCat does

- Watches all open PRs in a repository
- Discovers PRs created after MissCat starts
- Detects changes by PR HEAD SHA
- Reviews every new PR HEAD once per profile
- Supports draft PR reviews
- Avoids duplicate reviews
- Remembers reviewed commits across restarts
- Uses a persistent isolated review workspace
- Verifies the exact PR HEAD before reviewing
- Supports multiple reviewer backends

MissCat does **not** modify code, push commits, or merge PRs.

It watches. It catches. It reviews.

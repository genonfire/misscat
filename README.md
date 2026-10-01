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
pipx upgrade misscat
```

## Releasing (maintainers)

Releases are published to PyPI by `.github/workflows/publish.yml` when a `vMAJOR.MINOR.PATCH` tag is pushed. It uses PyPI [Trusted Publishing](https://docs.pypi.org/trusted-publishers/) (OIDC), so no PyPI token is stored in GitHub secrets.

### One-time setup

1. On PyPI, add a trusted publisher for the `misscat` project (Project → Publishing; for a first release use "Add a pending publisher"):
   - Owner: `genonfire`
   - Repository: `misscat`
   - Workflow name: `publish.yml`
   - Environment name: `pypi`
2. In the GitHub repository, create an environment named `pypi` (Settings → Environments). Recommended: restrict deployments to selected branches/tags and add required reviewers, so a `v*` tag pushed from an unreviewed commit cannot publish.

### Release procedure

1. Check that the version is not already on PyPI (versions are immutable and cannot be re-uploaded): <https://pypi.org/project/misscat/#history>
2. Bump `version` in `pyproject.toml` **and** `__version__` in `src/misscat/__init__.py` (the workflow fails if they differ), then merge to `master`.
3. Tag the merge commit and push the tag (the tag must equal `v` + the `pyproject.toml` version):

   ```bash
   git tag v1.0.1
   git push origin v1.0.1
   ```

4. The workflow fails before publishing if the tag is malformed, the tag does not match `pyproject.toml` or `__version__`, or `python -m build` / `twine check` fails.
5. Confirm the release:

   ```bash
   pipx upgrade misscat
   misscat --version
   pip index versions misscat
   ```

## Quick Start / First Run

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
misscat genonfire/misscat luna
```

MissCat resolves `luna` as:

```text
~/.config/misscat/luna.yml
```

Running `misscat` with no arguments (or `misscat --help`) shows usage help:

```bash
misscat --help
```

Repository names are canonicalized to lowercase, so `Genonfire/MissCat` and `genonfire/misscat` refer to the same local workspace and state.

## Configuration

MissCat ships with a bundled `default.yml` inside the package, so installed copies do not depend on the source checkout or current working directory.

User configuration and persistent data are kept entirely outside the package:

- **Profiles**: `~/.config/misscat/<profile>.yml`
- **Per-repository state**: `~/.config/misscat/<owner>__<repo>.json`
- **Persistent workspace**: `~/.cache/misscat/repos/<owner>/<repo>/`

Directory layout:

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

MissCat reviews each repository in its own persistent workspace:

```text
~/.cache/misscat/repos/<owner>/<repo>/
```

The repository is cloned on first use and reused for later reviews. Before each review, MissCat prepares a clean checkout of the exact PR HEAD.

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
  - Multiple MissCat instances or profiles pointing to the same repository would share and conflict over the same persistent workspace (`~/.cache/misscat/repos/<owner>/<repo>/`).

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

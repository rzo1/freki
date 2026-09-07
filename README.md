# Freki

[![CI](https://github.com/rzo1/freki/actions/workflows/ci.yml/badge.svg)](https://github.com/rzo1/freki/actions/workflows/ci.yml)

<img src="docs/logo.png" alt="Freki logo" width="200" align="right">

> *Freki, "the greedy one" — one of Odin's two wolves, who devours everything
> set before him.*

Back up **everything** you can reach on a GitLab instance: full git mirrors of
all your projects — including all branches, tags, LFS objects and wikis — plus
a browsable checkout of each default branch and a Markdown export of all
issues and merge requests with their comments and attachments. Works with any
self-hosted GitLab or gitlab.com, authenticated by a personal access token
(PAT).

Runs are **incremental**: run it again (manually or via cron) and existing
mirrors are fetched instead of re-cloned, checkouts follow the branch tip, and
only issues/MRs that changed are re-exported. Nothing is ever deleted locally.

## What you get

```
<output>/
└── group/subgroup/
    ├── project.git/                      bare mirror: all branches, tags, LFS
    ├── project/                          working tree of the default branch
    ├── project.wiki.git/                 wiki mirror
    ├── project.wiki/                     wiki pages as files
    ├── project-issues/
    │   ├── 0042-fix-login-bug--jdoe.md   metadata, description, comments
    │   ├── 0042-fix-login-bug--jdoe.json raw API data
    │   └── attachments/<secret>/…        downloaded uploads, links rewritten
    └── project-merge-requests/           same structure as issues
```

## Requirements

- Python >= 3.14 and [uv](https://docs.astral.sh/uv/)
- `git` on `PATH`
- `git-lfs` on `PATH` (only needed for LFS content; see below)
- A GitLab PAT with the `read_api` scope (used for discovery via the API).
  Add `read_repository` only if you clone with `--protocol https`; with the
  default SSH transport your ssh key authorises the git access instead

## Installation

```bash
git clone https://github.com/rzo1/freki.git
cd freki
uv sync
uv run freki --help
```

Also installed under the alias `bms` (*backup my shit*) — `bms user` works too.

Or run it directly from a checkout without installing anything else —
`uv sync` creates the virtualenv and installs all dependencies.

## Configuration

Settings are read from environment variables or a `.env` file in the current
directory (see `.env.example`). CLI flags always take precedence.

| Variable                  | Default                          | Description                                  |
|---------------------------|----------------------------------|----------------------------------------------|
| `GITLAB_URL`              | *(required)*                     | Base URL of the GitLab instance              |
| `GITLAB_TOKEN`            | *(required)*                     | Personal access token                        |
| `GITLAB_OUTPUT_DIR`       | `./gitlab-export`                | Directory the mirrors are written to         |
| `GITLAB_INCLUDE_ARCHIVED` | `false`                          | Also clone archived projects                 |
| `GITLAB_CLONE_WIKI`       | `true`                           | Mirror project wikis                         |
| `GITLAB_CLONE_LFS`        | `true`                           | Fetch LFS objects (`git lfs fetch --all`)    |
| `GITLAB_CHECKOUT`         | `true`                           | Working-tree checkout of the default branch  |
| `GITLAB_EXPORT_ISSUES`    | `true`                           | Export issues (comments + attachments)       |
| `GITLAB_EXPORT_MRS`       | `true`                           | Export merge requests (comments + attachments) |
| `GITLAB_GIT_PROTOCOL`     | `ssh`                            | Git transport: `ssh` or `https`              |
| `GITLAB_CONCURRENCY`      | `4`                              | Projects processed in parallel               |

```bash
cp .env.example .env   # then set GITLAB_URL and GITLAB_TOKEN
```

## Usage

```bash
# All projects of a group, recursively including all subgroups
freki group <GROUP_ID_OR_PATH>          # e.g. 1234 or faculty/course

# A single project
freki project <PROJECT_ID_OR_PATH>      # e.g. 5678 or faculty/course/repo

# Everything the token's user can access: owned/private projects plus all
# projects in every group (and subgroup) the user is a member of
freki user

# Examples
freki group faculty/course --dry-run
freki group 1234 -o /backup/gitlab --include-archived -j 8
freki user --no-wiki --no-lfs --token glpat-...
```

Numeric ids and full paths are both accepted. Options are given after the
command (`freki group <ID> [OPTIONS]`); run
`freki <command> --help` for details.

Common options (available on every command):

| Option                                         | Description                                                    |
|------------------------------------------------|----------------------------------------------------------------|
| `--url URL`                                    | GitLab base URL (overrides `GITLAB_URL`)                       |
| `--token TOKEN`                                | Personal access token (overrides `GITLAB_TOKEN`)               |
| `--output DIR`, `-o DIR`                       | Output directory (overrides `GITLAB_OUTPUT_DIR`)               |
| `--include-archived` / `--no-include-archived` | Include archived projects (skipped by default)                 |
| `--wiki` / `--no-wiki`                         | Mirror wikis (default: on)                                     |
| `--lfs` / `--no-lfs`                           | Fetch LFS objects (default: on)                                |
| `--checkout` / `--no-checkout`                 | Keep a working-tree checkout of the default branch next to each mirror (default: on) |
| `--issues` / `--no-issues`                     | Export issues with comments + attachments to `<project>-issues/` (default: on) |
| `--mrs` / `--no-mrs`                           | Export merge requests with comments + attachments to `<project>-merge-requests/` (default: on) |
| `--dry-run`                                    | Only list what would be cloned/updated; nothing is written     |
| `--concurrency N`, `-j N`                      | Number of projects processed in parallel                       |
| `--protocol ssh\|https`                        | Git transport (default `ssh`: uses your ssh agent/keys; `https`: uses the token) |
| `--with-shared`                                | Also include projects merely *shared* with a group (`group`, `user`) |

Each run prints who the token authenticates as, a table of the projects that
will be mirrored (path, archived, wiki), a progress bar, and finally a
per-project summary (cloned / updated / unchanged / skipped / failed, plus wiki, LFS,
checkout, issues and merge-request status).

Exit codes: `0` success, `1` at least one project failed (including a
"partial" result where the mirror succeeded but its wiki or LFS step failed),
`2` configuration, authentication or "not found" errors.

## Notes

- **Transport**: the PAT is always used for the GitLab *API* (discovery). Git
  itself uses **SSH by default** (`ssh_url_to_repo`, e.g.
  `git@gitlab.example.com:group/project.git`), so your ssh agent/keys must
  have access; ssh runs with `BatchMode=yes` so a missing key fails fast
  instead of prompting. Use `--protocol https` (or `GITLAB_GIT_PROTOCOL=https`)
  to clone over HTTPS with the token instead.

- **Archived projects** are skipped unless `--include-archived` is given.
- **Re-running** is safe and intended: mirrors that already exist are not
  re-cloned but fetched (`git remote update --prune`), so new branches/tags
  arrive and deleted ones are pruned. The summary then shows `updated` with a
  ref delta (`+added ~changed -removed`) when anything changed, or `unchanged`
  when the mirror was already up to date.
- **Checkout**: besides the bare mirror `<project>.git`, a plain working tree
  of the default branch's latest commit is kept at `<project>/` (and
  `<project>.wiki/` for wikis). It is a detached `git worktree` of the mirror,
  so it needs no extra download and LFS files are populated from the mirror's
  LFS store. Treat it as a read-only export: every run resets it to the branch
  tip and local edits in it are discarded. If you move the output folder, run
  `git -C <project>.git worktree repair` (or just re-run the tool).
- **Issues & merge requests**: exported next to the mirror as
  `<project>-issues/` and `<project>-merge-requests/`. Each item becomes
  `NNNN-<slug>--<assignee>.md` (e.g. `0042-fix-login-bug--jdoe.md`; several
  assignees are joined with `+`) with a metadata table (state, author,
  assignees, reviewers, labels, milestone, branches, merge info, dates, URL),
  the description, all comments in order (diff comments show file:line) and a
  compact activity log of system events. A `.json` twin holds the raw API data
  (item + notes). Attachments referenced as `/uploads/<secret>/<file>` are
  downloaded to `attachments/<secret>/<file>` and the links are rewritten to
  those local copies. A `.index.json` per folder records `updated_at`, so
  re-runs only re-fetch issues/MRs that changed; a renamed title or changed
  assignee replaces the old file. The summary shows e.g. `12 (3 updated)` or
  `12 (unchanged)`.
- **Wikis**: if a project has its wiki enabled, the wiki repository is mirrored
  next to the project as `<path_with_namespace>.wiki.git`. An empty or
  non-existent wiki (GitLab answers with an error for those) is tolerated and
  does not mark the project as failed.
- **LFS**: after cloning/updating, `git lfs fetch --all` is run inside the
  mirror. If the repository uses no LFS or `git-lfs` is not installed, this
  step is skipped cleanly.
- **Token safety**: authentication uses an in-process git credential helper
  fed from the environment. Any system/global `credential.helper` (for example
  `osxkeychain` or `store`) is disabled for the duration of the git calls, so
  neither a stale stored credential is used nor the token persisted anywhere.
  The token is never written to `.git/config`, the remote URL, or the logs;
  the stored remote URL is the plain HTTPS URL.
- **Interrupted clones**: an empty or clearly non-git leftover directory is
  reused/removed on the next run; a partial git directory (has `objects/` but
  no `HEAD`) is reported as failed with a hint to delete it.
- Projects are deduplicated by id, so a project reachable through several
  groups is cloned only once.


## What is *not* backed up

Things living outside git and issues/MRs: CI/CD pipelines, job logs and
artifacts, releases (their descriptions/binaries; the tags are in the mirror),
container/package registries, snippets, and board/label/milestone definitions.

## Development

Contributions welcome — run the checks before opening a PR:

```bash
uv run ruff format --check .
uv run ruff check .
uv run pytest
```

Lint hooks are defined in `.pre-commit-config.yaml`; install them with
[prek](https://github.com/j178/prek) (or classic pre-commit):

```bash
uvx prek install      # or: uvx pre-commit install
```

## License

[MIT](LICENSE)

Logo generated by ChatGPT.

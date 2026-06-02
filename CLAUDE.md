# Project conventions

This is an open-source project. Write everything that ends up in the
repository or in public-facing places in **English**:

- commit messages
- pull request titles and descriptions
- code comments
- documentation and `.md` files
- identifiers, log messages, and user-facing strings

(Conversational replies to the maintainer may be in their language, but
anything committed or published must be English.)

## Python code style

Linting is done with [ruff](https://docs.astral.sh/ruff/). The configuration
lives in each component's `pyproject.toml` (`agent`, `server`, `e2e_tests`).
Run `make lint` (or `make check`) to lint the whole repository.

- Maximum line length is 150 characters.
- Keep two blank lines between the import block and the code that follows it
  (enforced via ruff's isort `lines-after-imports`).
- Prefer `from X import Y` over `import X` where it reads naturally. This is a
  convention only; ruff has no rule for it.

# Pull requests

When a PR is not based on `main` but on another PR's branch (a stacked PR),
flag this at the very top of the PR description with a blockquote in this
exact format:

> **Stacked on #N.** The base of this PR is `<base-branch>`, so the diff
> shows only the changes specific to this PR. Merge #N first, then this can
> be retargeted to `main`.

Replace `#N` with the parent PR's number and `<base-branch>` with its head
branch, and set the PR base accordingly (`gh pr create --base <base-branch>`).
Use this one style consistently; do not invent per-PR variants.

# Agent workflow

When you finish a coherent unit of work (a task, or a change you would
commit), launch a background subagent to review your changes. It runs in
the background so it does not block; report its findings in a follow-up
message once it completes.

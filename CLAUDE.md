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

# Agent workflow

When you finish a coherent unit of work (a task, or a change you would
commit), launch a background subagent to review your changes. It runs in
the background so it does not block; report its findings in a follow-up
message once it completes.

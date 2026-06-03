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

# Agent workflow

When you finish a coherent unit of work (a task, or a change you would
commit), launch a background subagent to review your changes. It runs in
the background so it does not block; report its findings in a follow-up
message once it completes.

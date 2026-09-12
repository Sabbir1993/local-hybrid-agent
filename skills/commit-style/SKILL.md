---
name: commit-style
description: Write clean conventional commits (feat/fix/chore/docs) with concise imperative subjects.
triggers: commit message, git
---

# Commit Message Style

Format: `type(scope): imperative summary`

- Types: feat, fix, chore, docs, refactor, test, perf
- Subject: under 60 chars, no trailing period, imperative mood ("add", not "added")
- Body (optional): wrap at 72 chars, explain why, not what
- Example: `fix(agent): clamp context size to 262144 to stop silent overflow`

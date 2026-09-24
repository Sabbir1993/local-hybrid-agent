---
description: Structured review of the current uncommitted changes - correctness, security, tests - ending in APPROVE or CHANGES_REQUESTED.
argument-hint: [files or focus]
---

# Review changes

Focus: $ARGUMENTS

1. **Collect** the diff with list_diff. If a focus or files were given, limit the review to those. If there are no changes, say so and stop.
2. **Reviews.** Run these; each gets the file paths and the purpose of the change:
   - `spawn_agent(role="code-reviewer", lane="main", task="Correctness, readability, error handling and test coverage review of: <paths>")`
   - `spawn_agent(role="security-reviewer", lane="executor", task="Security and PCI-DSS review (card data or secrets in logs, storage or responses; injection; authz): <paths>")`
   If either role is unavailable, use `role="multi-reviewer"`.
3. **Merge** the findings, drop duplicates, and check each one against the code yourself with read_file. Discard any finding you cannot confirm.
4. **Verdict:** a table (severity, file:line, issue, suggested fix), then one line: `VERDICT: APPROVE` if there are no critical or major issues, otherwise `VERDICT: CHANGES_REQUESTED`. Do not modify files in this command.

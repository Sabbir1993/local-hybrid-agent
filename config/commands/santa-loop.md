---
description: Dual-review convergence loop - two independent local reviewers must both PASS before the change is committed (max 3 rounds).
argument-hint: [files or scope to review]
---

# Dual-review loop

Scope: $ARGUMENTS
If no scope was given, review the current uncommitted changes (list_diff).

1. **Collect the change set** with list_diff, plus read_file on the changed files. If there are no changes, say so and stop.

2. **Round N (max 3).** Run two independent reviewers. Neither sees the other's result or this conversation.
   - Reviewer A: `spawn_agent(role="code-reviewer", lane="main", task="Review these changes for correctness, maintainability and tests: <file paths + what the change is for>. End with 'VERDICT: PASS' or 'VERDICT: FAIL' and list every blocking issue with file:line.")`
   - Reviewer B: `spawn_agent(role="security-reviewer", lane="executor", task="Review these changes for security and PCI-DSS data handling: card data or secrets logged, stored or returned in clear, injection, authz gaps: <file paths>. End with 'VERDICT: PASS' or 'VERDICT: FAIL' and list every blocking issue with file:line.")`
   If one of these roles is not available, use `role="multi-reviewer"` with the same task.

3. **Both PASS:** go to step 5.

4. **Any FAIL:** fix every blocking issue from both reviewers with edit_file. Run the tests with run_shell if the project has them (the user approves each command). Then start the next round with fresh reviewer calls. After round 3 still failing, stop and report the unresolved issues. Do not commit.

5. **Ship (only when both passed).** Show the user the proposed commit message, then run `git add` on the reviewed files and `git commit` with run_shell (each command needs the user's approval). Do not push. Tell the user to push when ready.

6. **Final answer:** a per-round table (reviewer, verdict, issues fixed), the commit hash if one was made, and anything left open.

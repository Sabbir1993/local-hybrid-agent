---
description: Gated pipeline for a new feature - plan, test-first implementation, verify, review. Nothing is committed without approval.
argument-hint: <feature description>
---

# Add feature (gated pipeline)

Feature: $ARGUMENTS

1. **Plan.** Call `spawn_agent(role="planner", task="Plan this feature for the workspace: <feature>. Return the files to touch, the ordered steps, the tests to add, and the risks.")`. Show the plan and ask the user to confirm. **Stop and wait for confirmation.**
2. **Tests first.** Once confirmed, add or extend tests that describe the feature (edit_file / write_file). Run them with run_shell and expect them to fail. The user approves each command.
3. **Implement** the smallest change that makes the tests pass, matching the surrounding code style.
4. **Verify.** Re-run the tests and the linter with run_shell until they are green. If it is still red after 3 attempts, stop and report.
5. **Review.** Call `spawn_agent(role="code-reviewer", task="Review these changes: <paths + purpose>")`. If the change touches payments, auth, secrets or user data, also call `spawn_agent(role="security-reviewer", task="Security / PCI-DSS review of: <paths>")`. Fix every blocking issue.
6. **Report:** the files changed, the test results and the review verdicts. Suggest a commit message, but do not commit or push unless the user asks.

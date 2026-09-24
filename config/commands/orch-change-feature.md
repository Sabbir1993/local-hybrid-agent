---
description: Gated pipeline for changing existing behavior - impact analysis, update tests, implement, verify, review.
argument-hint: <what should change>
---

# Change feature (gated pipeline)

Change: $ARGUMENTS

1. **Impact analysis.** Find every place the current behavior lives: code, tests, docs, config (grep, list_files, read_file). Call `spawn_agent(role="planner", task="Impact analysis and plan for this change: <change>, affected files: <paths>. List the call sites, the tests to update and the migration or compatibility risks.")`. Show the plan and ask the user to confirm. **Stop and wait for confirmation.**
2. **Update tests** so they describe the new behavior. Run them with run_shell (the user approves each command) and expect the updated ones to fail.
3. **Implement** the change across every affected site, and update the docs where they described the old behavior.
4. **Verify** with the full relevant test suite until it is green.
5. **Review.** Call `spawn_agent(role="code-reviewer", task="Review this behavior change: <paths>. Check for missed call sites and backward compatibility.")`. Add `security-reviewer` if the change touches payments, auth or data handling. Fix every blocking issue.
6. **Report:** the behavior before and after, the files changed, the tests and the verdicts. Do not commit or push unless the user asks.

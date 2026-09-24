---
description: Behavior-preserving refactor pipeline - baseline tests, simplify, re-verify, review.
argument-hint: <files or area to refine>
---

# Refine code (gated pipeline)

Target: $ARGUMENTS

1. **Baseline.** Read the target code. Run the existing tests with run_shell (the user approves each command) and record the result. If the target has no tests, add characterization tests first.
2. **Propose.** Call `spawn_agent(role="code-simplifier", task="Propose behavior-preserving simplifications for <paths>: dead code, duplication, naming, complexity. Return a list with file:line and the suggested change. Do not edit.")`. If that role is unavailable, use `role="reviewer"`.
3. **Apply** the proposals you agree with, one logical change at a time. Behavior must not change, and public APIs stay the same unless the user asked otherwise.
4. **Re-verify.** Re-run the same tests. They must match the baseline. Revert any change that breaks them.
5. **Review.** Call `spawn_agent(role="code-reviewer", task="Confirm this refactor preserves behavior: <paths>")`. Fix every blocking issue.
6. **Report:** what was simplified, the before and after test results, and the review verdict. Do not commit unless the user asks.

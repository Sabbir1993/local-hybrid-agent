---
description: Gated bug-fix pipeline - reproduce with a failing test, root-cause, minimal fix, verify, review.
argument-hint: <bug description or error message>
---

# Fix defect (gated pipeline)

Defect: $ARGUMENTS

1. **Locate.** Use grep and read_file to find where the symptom comes from. Write down your root-cause hypothesis before changing anything.
2. **Reproduce.** Add a test that fails because of the bug. Run it with run_shell (the user approves each command) and confirm it fails for the expected reason. If it can't be reproduced, report what you found and stop.
3. **Fix** the root cause with the smallest change. Don't just patch the symptom, and don't refactor unrelated code.
4. **Verify.** Run the new test and the surrounding test suite with run_shell until they are green.
5. **Review.** Call `spawn_agent(role="code-reviewer", task="Review this bug fix: <paths>, root cause: <cause>. Check for regressions and missed call sites.")`. Also call `spawn_agent(role="silent-failure-hunter", task="Check <paths> for swallowed errors related to this fix")` if that role is available. Fix every blocking issue.
6. **Report:** root cause, fix, test evidence and review verdict. Suggest a commit message, but do not commit or push unless the user asks.

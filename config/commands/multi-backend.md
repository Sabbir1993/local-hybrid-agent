---
description: Backend-focused workflow - the backend lane analyzes, plans and reviews; the main agent implements.
argument-hint: <backend task>
---

# Multi-lane backend

Task: $ARGUMENTS

1. **Context.** Find the relevant routes, services, models and tests with list_files, grep and read_file.
2. **Analyze.** `spawn_agent(role="multi-analyzer", lane="$BACKEND_LANE", task="BACKEND angle: <task + file paths>. Cover data flow, API contract, persistence, concurrency, failure modes and security.")` Present the key points. If the task is ambiguous, ask and stop.
3. **Plan.** `spawn_agent(role="multi-architect", lane="$BACKEND_LANE", task="<task> + <analysis>. Give the step-by-step plan with exact files, tests, and a diff prototype.")`
4. **Implement** the plan yourself with edit_file / write_file, matching the existing code style. Sub-agents never write.
5. **Verify.** Run the tests with run_shell (the user approves each command). Add or adjust tests for the change.
6. **Review.** `spawn_agent(role="multi-reviewer", lane="$BACKEND_LANE", task="Review these backend changes for correctness, security and PCI-DSS data handling (no card data or secrets logged, stored or returned in clear): <paths>")` Fix every critical and major issue.
7. **Final answer:** what changed, the test result, and the reviewer's verdict.

Payment and compliance rules: card data must be masked in logs, secrets come from config or the keychain and are never hard-coded, and examples use [PLACEHOLDER].

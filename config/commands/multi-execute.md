---
description: Implement a plan file with two local lanes proposing changes, the main agent as the only writer, then a dual-lane review.
argument-hint: <plans/feature.md>
---

# Multi-lane execute

Plan file: $ARGUMENTS

Rule: sub-agents only PROPOSE (analysis or a unified diff in their answer). You, the main agent, are the only one who writes files.

1. **Load the plan** with read_file. If the file doesn't exist or has no concrete steps, stop and tell the user to run /multi-plan first.

2. **Route each step.**
   - Backend / logic / data steps: `spawn_agent(role="multi-architect", lane="$BACKEND_LANE", task="Return a unified diff (in your answer, do not edit files) for: <step> Relevant files: <paths>")`
   - UI / frontend steps: the same call with `lane="$FRONTEND_LANE"`.
   - Small, obvious steps: do them yourself; no sub-agent is needed.

3. **Apply.** Read each proposed diff critically. It is a prototype, not final code. Rewrite it to match the surrounding code style, then apply it with edit_file / write_file. Never paste a diff blindly.

4. **Verify.** Run the project's tests or linters with run_shell (the user approves each command). If there is no test command, say so and name the one the user should run.

5. **Dual review.** Call both, with the changed file paths in each task:
   - `spawn_agent(role="multi-reviewer", lane="$BACKEND_LANE", task="Review the current changes for correctness, security and PCI-DSS data handling: <paths>")`
   - `spawn_agent(role="multi-reviewer", lane="$FRONTEND_LANE", task="Review the current changes for edge cases, error handling and tests: <paths>")`
   Fix every critical and major issue, then re-run only the reviewer that failed, at most 2 more rounds.

6. **Final answer:** the files changed, the test result, each reviewer's verdict, and anything left open. Do not commit or push.

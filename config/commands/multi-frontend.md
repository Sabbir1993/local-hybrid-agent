---
description: Frontend-focused workflow - the frontend lane analyzes, plans and reviews; the main agent implements.
argument-hint: <UI task>
---

# Multi-lane frontend

Task: $ARGUMENTS

1. **Context.** Find the relevant components, styles and scripts with list_files, grep and read_file.
2. **Analyze.** `spawn_agent(role="multi-analyzer", lane="$FRONTEND_LANE", task="FRONTEND angle: <task + file paths>. Cover UI/UX, component and state design, error and empty states, accessibility and responsive layout.")` Present the key points. If the task is ambiguous, ask and stop.
3. **Plan.** `spawn_agent(role="multi-architect", lane="$FRONTEND_LANE", task="<task> + <analysis>. Give the step-by-step plan with exact files and a diff prototype.")`
4. **Implement** the plan yourself with edit_file / write_file, matching the existing code style. Sub-agents never write.
5. **Verify** with run_shell if the project has a build, lint or test command (the user approves each command).
6. **Review.** `spawn_agent(role="multi-reviewer", lane="$FRONTEND_LANE", task="Review these UI changes for UX, accessibility and edge cases: <paths>")` Fix every critical and major issue.
7. **Final answer:** what changed, the verification result, and the reviewer's verdict.

Never render or log real card numbers in UI examples; use [PLACEHOLDER] values.

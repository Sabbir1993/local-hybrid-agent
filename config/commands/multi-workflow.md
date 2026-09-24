---
description: Full research, ideate, plan, execute, review workflow across two local lanes, pausing for the user between phases.
argument-hint: <task description>
---

# Multi-lane workflow

Task: $ARGUMENTS

Run the phases in order. After each phase that ends with **STOP**, give the user a short summary and wait. Continue only when their next message says so, and continue from where you stopped.

1. **Research.** Read the relevant code (list_files / grep / read_file). Score how complete the requirement is from 0 to 10: goal, scope, constraints, acceptance criteria. If the score is below 7, ask the missing questions. **STOP.**

2. **Ideation.** Call `spawn_agent(role="multi-analyzer", lane="$BACKEND_LANE", ...)` and `spawn_agent(role="multi-analyzer", lane="$FRONTEND_LANE", ...)`, each with a self-contained task and a different angle. Merge their results into at least 2 solution options with pros and cons. Ask the user to pick one. **STOP.**

3. **Plan.** Call `spawn_agent(role="multi-architect", lane="$BACKEND_LANE", ...)` for the chosen option. Write the plan to `plans/<slug>.md` with write_file. **STOP.**

4. **Execute.** Implement the plan yourself. Sub-agents may only propose diffs; you are the only writer. Run tests with run_shell (the user approves each command).

5. **Optimize and review.** Run two `multi-reviewer` sub-agents, one on each lane, over the changed files. Fix every critical and major issue.

6. **Report:** files changed, test results, the reviewers' verdicts, and follow-ups. Never commit or push unless the user asks.

Throughout: never output real card numbers, tokens or credentials. Use [PLACEHOLDER].

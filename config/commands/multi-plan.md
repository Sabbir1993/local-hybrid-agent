---
description: Two local lanes analyze the requirement independently, then the main agent merges their views into one implementation plan (read-only).
argument-hint: <feature or requirement>
---

# Multi-lane plan

Requirement: $ARGUMENTS

This is a planning run. Do NOT write or edit project source files; the only file you may write is the plan document in step 5.

1. **Context.** Use list_files / grep / read_file to find the code the requirement touches. If the requirement is too vague to plan (no clear goal or scope), stop and ask up to 3 clarifying questions in your final answer.

2. **Independent analysis (two lanes).** Call spawn_agent twice. Give each a self-contained task that includes the requirement and the relevant file paths you found. Neither can see this conversation.
   - `spawn_agent(role="multi-analyzer", lane="$BACKEND_LANE", task="BACKEND angle: <requirement + paths>. Analyze data flow, APIs, persistence, security and feasibility.")`
   - `spawn_agent(role="multi-analyzer", lane="$FRONTEND_LANE", task="FRONTEND angle: <requirement + paths>. Analyze UI/UX, client state, error states and accessibility.")`
   If the requirement has no UI, give the second analyst a "tests and edge cases" angle instead.

3. **Cross-check.** Compare the two results: where they agree, where they conflict (pick one and say why), and what only one of them noticed.

4. **Draft.** Call `spawn_agent(role="multi-architect", lane="$BACKEND_LANE", task="<requirement> + <merged analysis>. Produce the step-by-step implementation plan.")`. Fix anything in its plan that contradicts step 3.

5. **Write the plan** with write_file to `plans/<short-feature-slug>.md` in the workspace. Include:
   - Goal and scope (in / out)
   - Files to change (exact paths) and the change in each
   - Ordered steps, tests to add, risks and mitigations
   - Compliance notes: anywhere card data, credentials or merchant data are handled (PCI-DSS / Bangladesh Bank). Use [PLACEHOLDER] for any sensitive example value.

6. **Final answer:** a short summary of the plan and its path. End with: `Run /multi-execute plans/<slug>.md to implement it.` Do not start implementing.

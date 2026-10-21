# Engineering Improvement Plan (single roadmap for this repo)

> **Docker note:** the Docker/container sandbox items below are NOT planned. This project does not use Docker
> (see the no-Docker policy). The active roadmap is the competitive plan (Phases 0-6); containment is done in
> the companion with process-tree kill and native Job Objects.

> **Status note (2026-10-06): the self-assigned scores below predate the eval
> hygiene fixes and were never measured against a real model.** Read them as a
> ranking of where work was needed, not as measured facts. The live eval
> harness that can produce real numbers (`tests/eval_agent.py --live` + project
> bootstrap + `--update-live-baseline`) is the current source of truth for
> agent quality. Other planning docs were archived to `docs/history/`.

---

## 1. Diagnostic baseline (historical)
```
[Security, Auth & Governance (LDAP/MFA/PAN)] ==== 92/100  (S- Tier)
[Engineering Rigor & Test Suite]            ==== 88/100  (A Tier)
[Tool Surface & Multi-Format Ingest]        ==== 83/100  (A- Tier)
[Multi-Lane Routing & Fallback Ladders]     ==== 80/100  (B+ Tier)
[Autonomous Agent Loop & Error Recovery]    ==== 74/100  (B Tier)
[Sub-Agent Coordination & Swarm Protocols]  ==== 71/100  (B- Tier)
[Memory & Codebase Graph Intelligence]      ==== 65/100  (C+ Tier)
-------------------------------------------------------------------------
CURRENT COMPOSITE SCORE:                     ==== 79.8/100 (B)
TARGET COMPOSITE SCORE (POST-ROADMAP):       ==== 96.0/100 (S / Tier-1)
```
(Phase score deltas below â€” +4.7, +4, +4, +3.5 â€” are planning estimates, not derived measurements. Nothing in the telemetry converts "git checkpoints" into "+4.0". Read them as sequencing, not arithmetic.)

### Empirical Production Bottlenecks (from `usage.db`)
1. **Mutation fragility (CORRECTED 2026-10-02).** This section originally claimed `edit_file` fails 44% and `write_file` 45% of calls. That number was a measurement artifact: `route_events` logs a `tool_start` row *and* a completion row per call, and the failure count treated every `tool_start` (tool_ok NULL) as a failure â€” double-counting each call and counting the start of a successful call as a failure. Pairing events by `(run_id, step)` gives the real rates: **`edit_file` 6.3%** (6/95, Wilson 95% [2.9%, 13.1%]) and **`write_file` 5.2%** (3/58, [1.8%, 14.1%]); all tools together **4.8%** (60/1248, [3.8%, 6.1%]). `edit_file` is already at 93.7% success, so the ">90% edit success" target in Â§1.1 is met before any change â€” matching improvements cannot be the lever the original diagnosis assumed. Method note: `scripts/`-style pairing, not `SUM(tool_ok=0)/COUNT(*)`, is the only correct query; `perf_report.py:54` still uses the row-count form and inherits the artifact.
   By measured failure rate the real outliers are elsewhere: `spawn_agent` 45.5% (5/11), `browser_click` 41.7% (5/12), `analyze_image` 33.3% (2/6), `browser_eval` 23.3% (10/43), `read_file` 7.5% (17/227). Small n on the browser/spawn rows â€” bands are wide, so treat them as "look here first", not as ranked truth.
2. **Termination Instability:** Only **34.8%** of runs finish cleanly (`answered`). **24.2%** fail to self-terminate and require fallback synthesis, while **16.7%** exhaust step budgets or hit loop repetition guards.
3. **Semantic Code Blindness:** Grep and flat file reading fail to track cross-file definitions and call hierarchies, leading to blind token-wasting reads.
4. **Host Execution Exposure:** Application-level path checking protects file traversal, but approved shell execution runs bare on the host operating system.
5. **Unmeasurable failures (fixed in E-edit):** tool failures recorded `tool_ok` with no reason, so "edit_file fails 6.3%" could not be split into no-match / ambiguous / drift / verify-failed â€” every attribution query came back blank. `route_events.tool_err` now stores a fixed code (`core/route_log.py: classify_tool_result`, codes only, never text). Historical rows stay unattributed; new ones are.

---

## 2. Four-Phase Architectural Roadmap

```mermaid
flowchart TD
    subgraph Phase 1: Mutation Reliability
        P1A["Fuzzy Patch & Unified Diff"] --> P1B["Parallel Read-Only Tool DAG"]
    end

    subgraph Phase 2: Autonomous Recovery
        P2A["Git Ephemeral Checkpoints"] --> P2B["Hard Rollback on Death Loops"]
    end

    subgraph Phase 3: Codebase Intelligence
        P3A["Tree-Sitter AST Symbol Graph"] --> P3B["sqlite-vec Vector RAG Integration"]
    end

    subgraph Phase 4: Enterprise Sandboxing & Swarms
        P4A["Docker / Container Shell Sandbox"] --> P4B["Critic-Reviewer Multi-Agent Bus"]
    end

    Phase 1 --> Phase 2
    Phase 2 --> Phase 3
    Phase 3 --> Phase 4
```

---

## Phase 1: Robust File Mutation & Parallel Tool Dispatch
*Target Milestones: `edit_file` success > 90%, step latency cut by 40%. Projected Score: **84.5**.*

### 1.1 Replace Strict String Matching with Fuzzy Unified Diff
* **Problem:** Local models (Qwen-3B/Spark-4B/27B) frequently produce `old_string` blocks that do not match the file: wrong content, ambiguous (non-unique) matches, or line drift after earlier edits in the same run. Note the correction to earlier drafts of this plan: the edit path is `core/agent_tools/edit_engine.py` + `core/agent_tools/file_ops.py` (there is no `core/file_tools/editing.py`), and whitespace/indentation tolerance **already exists** (`core/agent_tools/schemas.py:84`, whitespace-ignored matching in `edit_engine.py:174`) â€” yet `edit_file` still fails. Whitespace is not the root cause; content mismatch is. Fuzzy tiers address drift, unified-diff input addresses model ergonomics (models write diffs more reliably than they copy blocks). **Scale correction:** the "~44% of calls" figure behind this item was a telemetry artifact (see bottleneck 1 above); the measured rate is 6.3%. The mechanisms below still help the residual failures and future tool_err data will say how much, but this is a robustness item, not the rescue the original draft assumed.
* **Target Milestone (revised):** the residual `edit_file` failures should skew toward `ambiguous`/`no_match` in `tool_err`, not "started and never completed".
* **Implementation:**
  - Modify `core/agent_tools/edit_engine.py` (matching) and `core/agent_tools/schemas.py` (diff-input tool contract).
  - Implement a 3-tier fallback matching algorithm:
    1. **Strict exact match** (fast path).
    2. **Whitespace-normalized / Indentation-agnostic match:** Strip trailing whitespace and normalize tabs/spaces.
    3. **Fuzzy Myers / Levenshtein block match:** Sliding window match with similarity threshold $\ge 0.85$.
    4. **Unified Diff Patch:** Accept standard `diff -u` / Git patches directly as tool input.

### 1.2 Parallel Tool Dispatch for Read-Only Operations
* **Problem:** In `routes/agent/run.py`, multiple tool invocations execute in a single serial queue.
* **Implementation:**
  - Classify tools into **Idempotent / Read-Only** (`read_file`, `list_files`, `grep`, `web_search`, `read_file_chunk`) vs **Mutating / Effectful** (`edit_file`, `write_file`, `run_shell`, `run_python`).
  - When the model outputs multiple read-only tool calls in one step, dispatch them concurrently using `asyncio.gather(*[run_tool(t) for t in calls])`.
  - **Hard constraint (D4 lesson):** fan-out is a bypass vector. Concurrent dispatch must preserve per-call permission and lane gating â€” the equivalent of `parallel_spawn_eligible()` in `core/agent_loop/policy.py` for the concurrent path â€” or it reopens the plan-mode bypass that phase D closed. A negative control (plan-mode + concurrent fan-out stays denied) is part of acceptance, not optional.

---

## Phase 2: Git Worktree Checkpoints & Hypothesis Rollback
*Target Milestones: Eliminate death loops (`loop_near_repeat`), reduce emergency syntheses by 60%. Projected Score: **88.5**.*

### 2.1 Pre-Flight Ephemeral Git Checkpointing
* **Problem:** When an agent edits files and the test suite or syntax checker fails, the agent attempts ad-hoc edits on already corrupted files, triggering degeneration and step exhaustion.
* **Implementation:**
  - At Step 0 of an agent session in `core/agent_loop/execution.py`:
    ```python
    # Ephemeral checkpoint snapshot
    checkpoint_ref = f"agent-checkpoint-{session_id}-{int(time.time())}"
    await git_create_checkpoint(workspace_path, checkpoint_ref)
    ```
  - Expose a native `revert_to_checkpoint` internal action when:
    - `loop_guard` detects near-identical consecutive tool failures.
    - Python execution encounters severe unrecoverable syntax errors.
    - The model explicitly requests a rollback to try an alternate approach.

### 2.2 Structured Task DAG vs Prose Plan Mode â€” PREMISE PARTLY FALSE; instrumentation shipped 2026-10-02
* **Problem:** Plan tracking exists (`create_plan` / `get_plan` / `update_plan_item` â€” all live in telemetry) but items are prose status strings. The model cannot programmatically verify progress against planned tasks.
* **Implementation:**
  - Upgrade `set_plan_context` and `db_get_plan_items` to enforce a structured JSON task graph:
    ```json
    {
      "tasks": [
        {"id": "t1", "title": "Inspect schemas in core/db", "status": "completed", "deps": []},
        {"id": "t2", "title": "Apply migration to sqlite", "status": "in_progress", "deps": ["t1"]},
        {"id": "t3", "title": "Run test_auth_invariant.py", "status": "pending", "deps": ["t2"]}
      ]
    }
    ```
  - Invalidate and fail step completions if the model claims completion while dependent task nodes remain in `pending` or `failed` state.
- **Verification result:** "the model cannot verify progress" is only half true, and the second bullet is **already implemented**. `plan_guard.check_update` refuses to mark a step done while an earlier one is open (`plan_guard.py:76-80`) and refuses two steps in progress at once (`:71-75`); `tool_update_plan_item` refuses out-of-range and bad statuses; `focus_message` re-injects the current step each turn. That is a **stricter** constraint than a DAG with `deps` â€” a linear chain cannot express parallel branches, it only guarantees order. Pinned by `tests/test_plan_adherence.py::ExistingEnforcementIsRealTests` so a future DAG change cannot quietly drop it.
- **What was genuinely missing: any measurement of whether runs finish their plans.** `route_events.outcome` records what a turn *did*; nothing recorded whether the plan was completed. So "should plans become a DAG?" had no number on either side.
- **Shipped instead** (`plan_guard.run_summary` + `routes/agent/run.py`): a run-level code in `route_runs.detail` â€” `plan:done`, `plan:open:N/M`, `plan:failed:N`, `plan:failed:N/open:K/M`. Counts only, never step text (a step can quote a path); an unrecognised status counts as open so a new status can never read as done. `detail` now **composes** codes (`synth:â€¦|plan:open:1/3|identical_result:â€¦`) instead of first-wins, so a synthesis and an unfinished plan can both be visible.
- **Validation:** `tests/test_plan_adherence.py` (13 tests: every code shape, bounded/no-text, unknown-status, existing enforcement, DB write) plus an end-to-end probe over the real mock harness â€” 28 tasks, `run_end` spied: `write_and_verify â†’ plan:done`, `plan_complete â†’ plan:done`, and `append_build` / `forbidden_name_blocked` / `plan_guard_violation â†’ plan:open:2/2`. 1588 tests OK, ruff clean, JS 16/16, both CI eval gates green.
- **Honest caveat:** those three `plan:open:2/2` results are artifacts of *scripted* turns that simply never call `update_plan_item` â€” they are not evidence that real runs abandon plans. The measurement exists now; the number needs live traffic.
- **Still open, deliberately:** the `deps` graph itself. Adding it would mean a DB column plus API/tool/UI surface, and it would *relax* the ordering that currently guarantees correctness. Do it only if the live `plan:open` rate says linear order is what is holding runs back â€” and not before.

---

## Phase 3: Code Intelligence via Tree-Sitter & Vector RAG
*Target Milestones: Memory score 65 -> 90. Match Cursor/Claude Code repository comprehension. Projected Score: **92.5**.*

### 3.1 Tree-Sitter Abstract Syntax Tree (AST) Symbol Engine
* **Problem:** Relying on `grep` forces the agent to read 5-10 full files to trace a single function call, rapidly exhausting context windows.
* **Supply-chain note:** tree-sitter wheels are a new native dependency. They go through the same process `ldap3` did: version floor in `requirements.txt`, hash-locked (`requirements.lock`), `pip-audit` clean, `--check-lock` green â€” before any code lands.
* **Implementation:**
  - Create `core/code_intel/ast_index.py` using `tree-sitter` (Python, TypeScript, JavaScript, Go, Rust, C++).
  - Register two high-leverage tools:
    1. `find_symbol_definition(name, path_hint=None)`: Returns exact file, line number range, and docstring of class/function/struct definitions.
    2. `find_symbol_callers(name)`: Returns all call sites and import statements across the repository.
    3. `get_file_outline(path)`: Returns the high-level skeleton (classes, methods, signatures) without loading file implementation bodies into context.

### 3.2 Native Vector Indexing with `sqlite-vec`
* **Problem (corrected):** earlier drafts of this plan stated the `:8093` embedder was "not wired into an indexed vector database" and that search was "raw keyword counting". Both are false in the current tree: chunk vectors ARE stored (`vec BLOB` + `dim`, embedded at ingest via `ensure_knowledge_vectors`) and `search_knowledge_hybrid` IS hybrid cosine+lexical (measured: real-vector recall 1.0). The actual gaps: brute-force cosine with no ANN index, character chunking (900/150) instead of semantic boundaries, and an open `min_cos=0.45` gate (measured real-vector fallout 0.771).
* **Implementation:**
  - Integrate `sqlite-vec` extension into `projects.db` / `knowledge.db` (same supply-chain bar as 3.1: native extension, hash-locked, audited).
  - On workspace upload or file change:
    - Chunk files by AST boundaries (functions/classes) rather than arbitrary character splits.
    - Generate embeddings and write directly to an indexed `vec0` virtual table.
    - Add Reciprocal Rank Fusion over the existing hybrid scores; calibrate `min_cos` against the fallout metric (R4), not by feel.

---

## Phase 4: Containerized Sandboxing & Swarm Consensus
*Target Milestones: Host security score 92 -> 98, Subagents 71 -> 94. Final Score: **96.0+ (S-Tier)**.*

### 4.1 True OS-Level Execution Isolation
* **Problem:** Approved shell and Python scripts execute directly on the developer's physical machine. A hallucinated or malicious command has host access.
* **Platform realism note:** this box is Windows + dual-Arc Vulkan. GPU passthrough for local inference through Docker Desktop is unresolved, and several companion/devtools flows assume host paths â€” containerizing execution breaks them until they are abstracted. As written (weeks 7â€“8 alongside swarms + browser agent), this phase is not credible. Re-scoped below as an investigation spike first, build second.
* **Implementation:**
  - Add an execution backend abstraction in `core/shell_tools.py`:
    - `HostRunner` (development default, permission-gated).
    - `DockerRunner` (production default: mounts workspace volume read-write, caps RAM at 4GB, drops `NET_ADMIN`, strips root privileges).
    - `WindowsSandboxRunner` / `AppContainer` (native Windows isolated container).

### 4.2 Multi-Agent Critic/Reviewer Verification Loop
* **Problem:** Single-agent generations go straight to the user without peer validation.
* **Proof standard (F2 rule):** a 3B reviewer checking a 4B coder's diffs at 3x token cost must prove it is not theater. Report-only first (log agreements/disagreements + downstream outcome), arm the reject path only when disagreement predicts real defects better than chance. No gate without the measurement.
* **Implementation:**
  - In `core/subagent/runner.py`, implement a **Critic-Actor Protocol**:
    ```
    User Prompt 
        --> [Planner Agent] (Decomposes task into sub-tasks)
            --> [Coder Agent] (Applies file edits & runs tests)
                --> [Reviewer Agent] (Inspects git diff & verification logs)
                    --> Approved: Present to User
                    --> Rejected: Provide targeted feedback & re-dispatch to Coder
    ```

### 4.3 Playwright Headless Browser Agent
* **Problem:** `web_fetch` only parses static text/HTML. It cannot handle modern SPA interfaces or authenticate behind web forms.
* **Implementation:**
  - Build `core/browser_tools/playwright_runner.py` with Chrome DevTools Protocol (CDP) connectivity.
  - Return accessibility tree snapshots (`accessibility_tree`) and screenshot diffs for UI verification.

---

## 3. Prioritized Implementation Timeline

| Phase | Duration | Primary Files Touched | Key Deliverable |
| :--- | :---: | :--- | :--- |
| **Phase 1** | Week 1â€“2 | `core/agent_tools/edit_engine.py`, `core/agent_tools/file_ops.py` | Fuzzy Myers diff patching; `edit_file` failure dropped < 10% |
| **Phase 2** | Week 3â€“4 | `core/agent_loop/execution.py`, `core/agent_loop/loop_guard.py` | Git worktree snapshot & rollback; Structured Task DAG |
| **Phase 3** | Week 5â€“6 | `core/code_intel/`, `core/memory/`, `core/agent_tools/` | Tree-Sitter AST indexer; `sqlite-vec` hybrid semantic search |
| **Phase 4** | Week 7â€“8 | `core/shell_tools.py`, `core/subagent/runner.py` | Docker container sandbox; Critic-Reviewer multi-agent bus |

---

## 4. Verification & Acceptance Test Suite

To verify that the system has attained S-Tier status, each milestone must pass strict automated criteria:

```bash
# NOTE: unittest has no -k filter flag (that is pytest). Run whole files,
# or address a single test by its full node id.
# 1. Verify mutation robustness on messy whitespace & indentation
#    (test name prospective - red test first, then implement)
python -m unittest tests.test_file_tools -v

# 2. Verify git checkpoint rollback on simulated failure loop
#    (no tests/test_agent_loop.py exists yet - this test is to be written
#    alongside the checkpoint implementation, red first)
python -m unittest tests.test_agent_loop -v

# 3. Verify AST symbol search speed and accuracy (<50ms over 1,000 files)
#    (to be written with the tree-sitter indexer, R9 below)
python -m unittest tests.test_code_intel -v

# 4. Measure live production telemetry shift
python scripts/perf_report.py --since <deployment_date>
# Target criteria (point estimates over usage.db, Wilson-banded, n reported):
# - edit_file success rate: >= 90%
# - answered vs synthesized ratio: >= 75% answered
# - loop_near_repeat count: 0
```

---

## 5. Integration into the R/V track sequencing

Salvageable items from this plan, mapped onto the measurement-first sequencing already in motion (R1â€“R3 done: real-vector slice, fallout gating, lexical normalization). Nothing below starts without its stated prerequisite. Item codes (R9, V4 â€¦) continue the existing track numbering.

### R9 â€” Code search via tree-sitter (Â§3.1, best item in this plan) â€” DONE 2026-10-02
- **Prerequisite:** R4 grid complete (scoring knobs settled â€” a second ranker on top of moving weights measures noise).
- New slice, same harness pattern as the KB slice: a staged code corpus (renames, cross-file callers, overloads) with symbol-lookup questions; `find_symbol_definition` / `find_symbol_callers` / `get_file_outline` as the tools under test.
- New dep through the ldap3 process (floor + hash-lock + audit + `--check-lock`) before code.
- **Acceptance:** symbol recall@3 â‰¥ 0.9 on the staged corpus, p95 < 50ms over 1,000 files, red-first tests in `tests/test_code_intel.py`, baselined + regression-gated like the KB slice.
- **Result (measured):** 8-question fixture slice recall = 1.0 (gate, baseline-merged); scale probe 1,000 files: cold index 2.2s / 5,399 symbols, warm query p95 10.4ms (acceptance <50ms PASS; the first implementation re-parsed per query at 329ms â€” fixed by a stamp-keyed in-process cache, invalidation pinned by `tests/test_code_intel_cache.py`: identity on repeat, edit/add/delete/dual-root staleness). Baseline update merge fixed to also preserve `offline` (a `--no-offline` update had nulled it, which would have left CI's offline gate comparing against nothing) and `code` sections. 1508 tests OK, both CI eval gates green. TS (.ts/.tsx) intentionally skipped (no grammar locked).

### R10 â€” AST chunking experiment (Â§3.2 remainder)
- **Prerequisite:** R5 (char-chunking evidence) complete, so AST boundaries have a measured baseline to beat.
- A/B AST-boundary chunks vs the R5 winner on the KB slice (recall + fallout, both modes). sqlite-vec/ANN enters ONLY if brute-force latency shows up in measurement first â€” no index without a measured latency problem.
- **Acceptance:** recall up or fallout down with no regression in the other metric, else AST chunking stays an experiment, not the default.

### E-edit â€” Unified-diff input + fuzzy tiers (Â§1.1, corrected diagnosis) â€” DONE 2026-10-02
- Red-first tests in `tests/test_file_tools.py` (drifted blocks, ambiguous matches fail loudly rather than fuzzy-applying wrong).
- **Acceptance (two levels, both required):** unit tests green AND live `usage.db` edit_file fail rate drops with Wilson bands that exclude the old rate. Unit-proof without telemetry shift is theater.
- **Result:** both tiers implemented and unit-proven â€” `apply_diff` (unified-diff input: `diff` arg on `edit_file`, hunks located + verified, applied **atomically** so a failed hunk leaves the file untouched; content drift and ambiguous hunks refused loudly) and tier-3 fuzzy block matching (`FUZZY_THRESHOLD=0.85`, â‰¥2-line blocks, applied only at exactly one candidate location). 1537 tests OK, ruff clean, both CI eval gates green.
- **Threshold priced, not asserted** (drift probe over 60 real functions from `core/`, injecting controlled drift): tiers 1â€“2 applied 60/227 drifted blocks, tiers 1â€“3 applied 123/227 â€” **+63 correct edits, 0 wrong edits** (negative controls: 0/118 wrong/adjacent blocks matched). Per class: rename-local 0â†’30/60, reword-string 0â†’26/49, drop-line 0â†’7/58. Sweep table: thr=0.70 â†’ positives 0.744, thr=0.85 â†’ 0.551, thr=0.95 â†’ 0.498, **negatives stayed 0.000 at every threshold** â€” 0.85 kept as the conservative pick since the negatives have no measured near-miss headroom to justify the looser gate.
- **Telemetry fix (prerequisite for ever closing the live half):** `route_events.tool_err` now records a fixed failure code (`classify_tool_result`, codes never text). Without it the acceptance was unprovable â€” the 6 real failures in `usage.db` carry no reason and cannot be replayed (tool args were never persisted).
- **Live half still open:** needs fresh traffic to compare `edit_file` fail rate and its `tool_err` mix against the corrected 6.3% [2.9%, 13.1%] baseline. With nâ‰ˆ95 calls per era, a 6â†’2 improvement will not clear the Wilson bands â€” the next measurement needs either more traffic or per-class counts, not a bigger point estimate.

### E-dispatch â€” Parallel read-only fan-out (Â§1.2, with the D4 constraint) â€” PREMISE FALSIFIED, not built (2026-10-02)
- Per-call permission/lane gating preserved; negative control (plan-mode + concurrent fan-out stays denied) in the same test file as the feature.
- **Acceptance:** 40% step-latency cut claim becomes a measured number on multi-read tasks, zero permission bypasses, no new flake in the mock suite.
- **Measured before building, and the population isn't there:** pairing completion events per `(run_id, step)`, **91.9% of tool steps issue exactly one call**; only 8.1% issue â‰¥2, and only **5.2% are multi-call *and* all read-only** (63 steps, 148 calls, all single-lane). Tool time is also a rounding error against model time: an executor step averages 11.5s (perf_report) while a local companion read is ~0.1s, so overlapping the entire addressable set saves well under 1% of step latency â€” not 40%. The 40% figure assumed tool calls dominate step latency, which is true of cloud APIs and false of a loopback companion.
- **And the risk is on the wrong side:** the other 35 multi-call steps are **mixed read-only + mutating** (e.g. `read_file` + `edit_file` in one step). Concurrency there would race a read against the write it is supposed to inform. Serial execution is the safer default, and the D4 lesson (fan-out as a bypass vector) applies to us here, not just to sub-agents.
- **Kept for the record, not scheduled:** if step latency ever becomes dominated by tool time (remote companion, high-latency devices, MCP tools), revisit with the same per-call gating requirement.

### E-checkpoint â€” Git checkpoints + rollback (Â§2.1) and structured DAG (Â§2.2, on existing plan tools) â€” PREMISE FALSIFIED for Â§2.1, DAG part still open
- Loop-guard hook + `revert_to_checkpoint` internal action; DAG validation on top of `create_plan`/`update_plan_item`, not greenfield.
- **Acceptance:** red-first rollback test; live synthesized/outcome shift measured in `usage.db` with bands (this is what moves the 24.2% synthesized number, and only measurement proves it).
- **Â§2.1 premise not supported by the data.** The claim is that agents corrupt files via ad-hoc repair and then degenerate. Measured: degeneration markers (`identical_result` / near-repeat codes in `route_runs.detail`) appear in **1 of 67 runs**. Runs that mutated files end *answered* at 35% (12/34) versus 33% (11/33) for runs that mutated nothing â€” mutating files is not associated with failure at all. So rollback-on-corruption has almost nothing to catch.
- **What the same data does show (bigger, unexamined):** the run outcomes are two populations, not one. `error` runs have **p50 = 1 step** (10 runs, 14.9%) and `synthesized` runs **p50 = 2 steps** (16 runs, 23.9%) â€” together **27% of all runs die almost immediately**, with the model request returning HTTP 200 and the final step being plain model text (no tool call), mostly in `main-cloud-rest-local`. The long-run ceilings are a separate, smaller group (`max_steps` p50=60, `budget` p50=91, `timeout` p50=55, `loop_near_repeat` p50=28, 11 runs total).
- **Cause was unmeasurable, so it is now measured:** `outcome='error'` runs recorded no cause at all â€” `_classify_step_error` tells the user, but `route_runs.detail` only ever carried loop detail. Added `_error_class` (`routes/agent/run.py`): the exception **type** as a bounded code (`err:ValueError`), never the message (messages carry local paths). Historical rows stay unattributed; new ones are, which is the precondition for investigating the 15% step-1 crash population at all.
- **Still open:** the Â§2.2 structured-DAG validation is independent of the falsified Â§2.1 premise and remains unbuilt.

### Investigate next: the step-1/2 early-exit population (27% of runs) â€” instrumentation DONE, diagnosis pending traffic
- Evidence so far: no failed HTTP requests; the router passes to the main lane at step 0 in 8 of 18 early exits versus 1 of 23 answered runs, and the final step is model text with `category=action`. Both point at the main-lane first-turn path rather than at tools, permissions, or the model server.
- **Attribution built (2026-10-02):** `validate_and_finalize_response` (`core/agent_loop/sandbox.py`) already names the cause â€” `synthesized response from successful tool actions`, `synthesized web search results`, `synthesized execution output`, `extracted answer from model reasoning`, `extracted full model reasoning`, `fallback confirmation`, `PASSIVE_REFUSAL_NOTE` â€” and `routes/agent/run.py` streams it to the client as `event: validated`, but it never reached `route_runs.detail`. All 16 recorded synthesized runs were therefore one indistinguishable bucket: a stall the model refused to do, a `fallback confirmation` that invented an answer, and a real synthesis from tool output all looked alike. Now mapped to a fixed vocabulary by `_synth_detail` (`routes/agent/run.py`) and stored: `synth:tool_actions`, `synth:web_results`, `synth:execution`, `synth:reasoning_answer`, `synth:reasoning_full`, `synth:fallback_confirmation`, `stall:passive_refusal`, with `synth:other` for anything unrecognized. Looked up by value, never copied, so a future note carrying user text cannot reach retained storage; `validated` records nothing so the interesting rows stay visible. Both outcome paths record it (plan path and main path).
- **Known undercount, deliberately not "fixed":** on the main path a synthesis keeps `outcome = stop_reason`, so synthesized answers there are recorded as `max_steps`/`loop`/`timeout`, not `synthesized`. The `synth:` detail code is the only record of them. Changing the outcome vocabulary would move `BAD_OUTCOMES`, the router tuner's baseline, and the quality rollup â€” a telemetry decision, not a bug fix, so it is left alone and recorded here.
- **Next:** the `err:`, `synth:` and `stall:` codes need fresh traffic before any diagnosis is possible. `python scripts/early_exit_report.py` is the diagnostic: it pairs calls correctly (completion rows only, `tool_start` reported separately as dangling), splits the populations, attaches cause codes, prints Wilson bands on every rate, and labels the step-0 router split as a hypothesis. It currently reports 18/67 early exits, all 18 unattributed, and says so rather than implying a diagnosis. Verified against the live database; the report's own `attribution_ready` check is what told us the codes have not been recorded yet.
- **To actually collect it** (needs a human at the keyboard â€” no credentials are stored in the repo, deliberately):
  1. `python server_manager.py --models-dir <models> --port 8000` with a profile loaded (see Â§14 of `PROJECT_KNOWLEDGE.md`),
  2. create the dedicated `eval` user (no MFA â€” the runner cannot answer TOTP) owning a throwaway workspace, activate it,
  3. `python tests/eval_agent.py --live --live-user eval --live-password '...'`,
  4. `python scripts/early_exit_report.py`.
  Cloud keys are unset in `config/app.json` (`cloud.main: null`), so live runs will be local-only unless a provider is configured â€” that changes which lane the runs exercise, so record the mode when comparing.

### Explicitly NOT scheduled
- **Critic-reviewer swarm (Â§4.2):** gated behind report-only evidence per the F2 rule. No build until disagreement predicts defects.
- **Container sandbox (Â§4.1):** re-scoped as an investigation spike (Docker Desktop + Vulkan passthrough feasibility for llama-server lanes; inventory of host-assuming flows). Build phase only if the spike resolves.
- **Playwright agent (Â§4.3):** after the sandbox question is answered â€” an authenticated browser without isolation is new attack surface, not a feature.

### C-snip â€” narration snip layer (P1, shipped 2026-10-20, DEFAULT-OFF) â€” DONE, unmeasured live
- **Premise (verified in-tree):** clearing.py stubs old *tool results* and compaction.py digest-rolls whole turns at `compaction_threshold` (0.70 of the window), but old *assistant narration* stayed verbatim until full compaction and re-sent on every step â€” the temporal-depth gap vs Claude Code's snip layer (5-layer pipeline, arXiv:2604.14228).
- **Shipped:** `core/agent_loop/snip.py` â stubs old assistant prose (content only; `tool_calls`, pairing, system/user/tool results untouched), newest `context.snip.keep_recent_narration` messages kept verbatim, batch discipline identical to clearing (`trigger_tokens` + `at_least_tokens` cache-prefix protection), idempotent via `SNIP_PREFIX`, squeeze mode halves the keep. Wired into `routes/agent/stream.py` main lane between clearing and compaction; `route_events` gains a free-form `snip` event with `outcome=snipped:<tokens>`. Config block `context.snip` in `config/app.json` with `enabled: false`.
- **DEFAULT-OFF guarantee:** `tests/test_context_snip.py::NoOpGuarantee` pins byte-identical history with the flag absent/false/broken. Nothing changes until the flag is armed in config â no code change needed to roll back.
- **Verification at merge (all green):** ruff clean; 15 new tests + the 15 clearing tests OK; full discover run has the same pre-existing environment-dependent failures as a clean tree (confirmed by `git stash -u` A/B: test_runner/test_file_tools etc. fail identically without this change); eval gates `--mock --no-offline --regression --repeats 3` and `--regression` both report "no regressions vs baseline"; branch coverage `core/*,routes/*` TOTAL 74% (floor 65), `core/agent_loop/snip.py` 86%.
- **Still open (the live half, standard for this repo):** the offline A/B long-run slice (`tests/eval_mock.py` grammar, flag on vs off, compare est. prompt tokens/step + answered/synth rates) must pass before `context.snip.enabled` is armed; then fresh `usage.db` traffic confirms the freed tokens actually landed. Do not flip the flag before that.
- **C-deferred (deferred MCP tool schemas) â PREMISE PARTLY FALSIFIED, not built.** The plan-item assumed every MCP tool schema shipped in every request. Measured in-tree: `prompt_scope.hide_unmentioned_mcp` (stream.py, S1) already hides schemas of servers the request does not name, and `tool_surface.hidden_families` withholds situational families (the 14 browser/device schemas alone were several thousand tokens of the executor's 32k window, already handled). The residual population â *many* schemas across several *named* servers â is an edge, and ToolSearch-style indirection is unproven on small local executors. Not scheduled: revisit only with usage.db evidence that named-server schema cost actually shows up, and then only on the main lane, default-off, by the same gate as E-dispatch.


### P2-session-fork ? branch a session from an earlier point (shipped 2026-10-20, DEFAULT-OFF)
- **Why fork, not truncate-in-place:** a destructive rewind can lose work the user asked to keep; a fork copies, so retrying from an earlier point is strictly additive. The head "moves" by continuing the new branch, which is what the OpenHands movable-head model buys in practice.
- **Shipped:** `core/db/sessions.py::db_fork_session` ? a new session (project/user/agent carried, `fork_of` recorded on `sessions` via a `PRAGMA table_info` migration in `core/db/common.py`, title prefixed with an arrow), messages copied up to and including `upto_message_id` (all when omitted), plan items copied **with statuses** so a retry resumes the standing plan, `session_working_memory` copied, ids re-issued, message count returned. Ownership enforced via `db_session_owner`; a `upto_message_id` outside the session is a loud ValueError, not a silent full fork. All of it lands inside one `core.sqlite_util.transaction`.
- **Surface:** `POST /control/sessions/{sid}/fork` in `routes/projects/session_endpoints.py`, body `{"upto_message_id": int|null}` (`ForkReq` in `routes/projects/models.py`), exported through `core/db/__init__.py`. **DEFAULT-OFF:** the endpoint answers 403 (`sessions.fork_enabled`) until armed in `config/app.json`; config ships with `sessions: {"fork_enabled": false}` and no UI change at merge ? the surface appears only when the operator arms it, and a rollback is the flag, not a revert. Fork/rewind anchors to the messages table, the only *persisted* history (the run-manager journal is deliberately in-memory and code-only), so a branch survives restarts and directly benefits the "Continue" path (plan re-injection reads `db_get_plan_items(session_id)`, which the copy satisfies).
- **Deliberately same-shape safety notes:** copied messages keep the at-rest PAN-masked content already stored in projects.db (nothing new reaches disk unmasked); forked sessions do not pre-allocate memory-search chunks (ingest happens downstream, unchanged); `db_delete_session` needs no new cascade rules because `fork_of` carries no FK.
- **Verification at merge (all green):** red-first 9 tests in `tests/test_session_fork.py` (copy boundary + `fork_of`, plan/working-memory carry, count, ownership refusal, unknown-message refusal, legacy-DB migration, endpoint flag gate + wiring); `tests/test_session_paging.py`, `tests/test_step_outcome.py`, `tests/test_working_memory.py` green (31 combined); ruff clean; both eval gates "no regressions vs baseline"; full discover run hit failures=23 where the 23rd WAS `tests/test_doc_refs.py` (a bare snip.py filename reference my own P1 entry had written; the meta-test caught it, full path fixed) — the remaining 22 failures + 2 errors are the same pre-existing environment-dependent set confirmed on a clean tree. app.json parses; the fork route is registered.

### P4-tool-duration - remote/MCP read-only batching: PREMISE FALSIFIED FOR NOW; the reopen instrument shipped (2026-10-20)
- **Census first (usage.db route_events, 3621 recorded tool calls, 2977 tool steps):** multi-call steps: 460; read-only multi-call steps: 240, and ALL 240 are already served by the existing all-read gather fast path in `routes/agent/stream.py` (web_search/web_fetch are among the batchable reads - remote latency already hides for them). Named mcp-prefixed tool calls found in telemetry: ZERO (only 4 plugin__devtools__report_file_stats calls, a different prefix). Population for MCP batching on this machine: none.
- **Verdict:** batch-parallel dispatch for read-only MCP tools is speculative infrastructure for a population of zero - not built, per the measurement-before-features rule and the same treatment the C-deferred entry got. The web_* tools stay covered by the existing fast path; read-only MCP would join it only if its population appears.
- **The real gap the census exposed - shipped as P4 (red-first, pure telemetry):** route_events reason=tool rows never recorded duration_s, so the documented reopen condition (tool time dominating step latency) was permanently unmeasurable. Now the tool event in `routes/agent/stream.py` carries duration_s for both dispatch paths: the all-read gather fast path times each call individually (new per-call timing wrapper around run_tool; calls finish out of order), and the sequential gauntlet is timed around its run only (approval-card and permission-card user wait time is deliberately excluded - user thinking latency is not tool latency). The schema column existed since day one in `core/route_log.py`; only the call site fed it.
- **Verification at merge (all green):** red-first 4 wiring tests in `tests/test_tool_duration.py` (event carries duration_s; sequential timer sits between the device-approval line and the parallel_results branch; per-call timing wrapper plus per-id storage in the gather; the duration expression covers the fast path); `tests/test_doc_refs.py` clean; ruff clean; both eval gates no-regressions; full discover run 3421 tests with only the pre-existing 22 failures + 2 errors confirmed on a clean tree.
- **Reopen condition (now measurable):** once a real population of MCP usage lands, read scripts/perf_report.py data from route_events: if median tool duration_s meaningfully exceeds the read-only fast-path dispatch cost, extend the all_reads eligibility predicate to read-only MCP tools (their read_only metadata) behind a config flag with its D4 negative control. Until then this stays closed-by-measurement, not by fiat.
- **Safety notes checked, not just assumed:** read-only MCP tools raise no permission cards even in tainted runs (`mcp_call_needs_approval` returns False for read_only=True), so extending the fast path would bypass no card; the all-spawn fan-out gating in `core/agent_loop/policy.py` already checks plan mode; fast_sandbox_check silently drops calls it cannot clear, which for tool names it does not recognize would double-announce the tool card - one more reason the extension is not a five-line change and stays closed until there is a population to justify handling that edge properly.

### P5-ACDI - Windows AppContainer isolation spike: PROVEN FEASIBLE 8/8 on this machine; NOT wired (2026-10-20)
- **Census first (usage.db tool schema events, the whole 2026-10/2026-11 window):** `run_sandbox_python` was called ZERO times ever (the WSL2 runner from `core/sandbox/runners.py` sits unexercised), while `run_shell` hit n=541 (535 ok) and `run_python` n=412 (408 ok) executing directly on the host - only the ask-first card stands between the model and the machine. Phase 4 of the plan names AppContainer exactly for this; the code-only spike (no wiring, no flag) was the gate before any build.
- **Shipped for the spike:** `core/sandbox/appcontainer.py` (pure SID derivation + userenv/advapi32/kernel32 ctypes bindings; profile create/delete; ACE-append grants; a `run()` launcher with attribute list + inherited log handles + timeout kill) and `scripts/appcontainer_probe.py` (the 8-case gauntlet). The derived AC profile SID formula is pinned LIVE natively: SHA-256 of the UTF-16LE name truncated to 28 bytes as 7 little-endian dwords under `S-1-15-2-` (11 dash-separated parts).
- **The gauntlet, all 8 green on the local machine:** profile created with zero capabilities; grants appended idempotently without replacing any DACL; a REAL python child runs inside the container and writes only the granted scratch dir; the NETWORK control - the contained child cannot complete a TCP connect to a live host-side listener (WFP drops, prints TimeoutError, never the connect-success marker) while the HOST child on the same listener succeeds; the workspace dir lists through the read grant; an un-granted sibling dir refuses the child (PermissionError, no leak); spawn latency host=25ms container=26ms (3-round avg) - the overhead is about one millisecond.
- **Hard-won facts this machine taught (each was a dead end before the fix):**
  1. `PROC_THREAD_ATTRIBUTE_SECURITY_CAPABILITIES` is 131081 (0x20009), not the 0x20014 shape seen in third-party code - a wrong value is accepted and silently spawns an UNCONTAINED child (is_appcontainer=0).
  2. Grant masks must be SPECIFIC rights, never GENERIC_*. The auto-remap at inherit time turned a parent `GENERIC_READ|GENERIC_EXECUTE` ACE into file entries at 0x001200A0 (FILE_GENERIC_EXECUTE, losing FILE_READ_DATA 0x1), and every image load then died with 0xC0000022. Specific 0x001200A9 (read+execute) and 0x001F01FF (file all) pass through unchanged.
  3. The lowbox token KEEPS Everyone / Users / Authenticated Users (verified by `whoami /groups` INSIDE the container), so path traversal and reading system installs (git under C:/Program Files) work with NO custom ACE at all; Administrators rides along deny-only and Local-integrity is Low (4096).
  4. DACL writes go only to objects the caller OWNS: the grant function resolves ancestors leaf-first and returns a skip list for the rest. Setting ANY inherit-flagged ACE on a shared ancestor (C:/Users) makes Windows re-propagate the ACL across the whole subtree (multi-minute), and un-owned ancestors reject SetNamedSecurityInfoW with error 5 outright.
  5. A directory ACE does NOT refresh inheritance on files created BEFORE it: git status inside the container failed reading .git objects with "Unable to read current working directory" style denies. Granting a whole tree to a container would need file-level ACEs or a refresh pass - explicitly not done in the spike.
- **Known debt, recorded so it is never a surprise:** the failed probe runs of the session left a handful of read-side ACEs for one-off derived SIDs on user-dir and repo DACLs (the names are unrecorded, so revoke-by-name is impossible; they grant read/list only). A `revoke_dirs` helper was deliberately not built before wiring is decided.
- **Decision:** NOT wired. `config/app.json` unchanged; `routes/agent/stream.py` imports nothing from the module (a scan test pins this in `tests/test_appcontainer.py`). The wiring question (which of run_shell/run_python calls go through the container, the file-refresh question for tree grants, the revoke story, and the flag) is a separate build decision for the user, exactly like P1 and P2.
- **Verification at merge (all green):** 13 tests in `tests/test_appcontainer.py` (pure core: the native-pinned derivation vector, parts/prefix, name rules, quoting round-trips, the not-wired scan; windows-only: win32 binding shape, SID binary form, and ONE live containment smoke that spawns cmd inside a zero-capability container and asserts the echo lands). ruff clean; `tests/test_doc_refs.py` green; the probe prints 8/8; appcontainer spawn cost measured negligible next to any model step.


## P6 entry - P6-B skill trust tiers (done) + P6-A ACP adapter spike (done).

### P6-B: skill provenance + trust levels - SHIPPED.
- module: core/skills.py. Constant: PROVENANCE_NAME = ".provenance.json".
  install_from_catalog now writes a sidecar INSIDE the installed skill dir:
  {"source": "skill_catalog", "catalog_sha256": _tree_hash(src), "installed_at": <iso>}.
- trust_of(name) -> "reviewed" (tree hash matches the recorded one) / "modified"
  (tree moved since install) / "handwritten" (sidecar absent or corrupt) / "" (not
  installed, or name would escape). - _tree_hash must EXCLUDE the sidecar file
  and __pycache__ or every install immediately reads back as "modified" and the
  reinstall guard trips on its own artifact.
- load_skills() entries now carry a "trust" key; the prompt fragment stays
  byte-identical (pinned in tests/test_skill_tiers.py) so nothing drifts.
- routes/customize/listing.py::_skills_items adds the same "trust" field for the
  customize page.
- TRUST IS INFORMATIVE ONLY: nothing gates on it; the agent's file tools still
  cannot reach .agents/skills (core/agent_tools/workspace.py _ws_resolve raises
  "path escapes workspace"), so a handwritten skill stays structurally
  un-loadable by the model. read_skill telemetry n=31 (21 ok) at spike time.
- tests: tests/test_skill_tiers.py - 9 tests green.

### P6-A: ACP (Agent Client Protocol) adapter spike - SHIPPED, NOT WIRED.
- new files: core/acp/__init__.py, core/acp/protocol.py,
  scripts/acp_agent.py, tests/test_acp_adapter.py.
- core/acp/protocol.py: AcpConnection - PURE framing layer. parse_lines() turns
  newline-delimited JSON-RPC 2.0 into frames (bad line -> None and the caller
  answers -32700 with id null); handle_frames() dispatches:
    initialize -> echoes the client protocolVersion (default 4),
    session/new -> session id from backend.new_session(cwd) when it exists
      (async-safe), else a local uuid; meta (cwd, mcpServers) stored privately.
    session/prompt -> streams session/update pushes BEFORE the response;
      converts backend dicts {"content":...} / {"tool_call":...} /
      {"tool_call_update":...} into ACP update frames; text-only prompts;
      stopReason end_turn / cancelled / error.
    session/cancel -> cooperative notification, honored between prompts.
  Errors: -32601 unknown method, -32602 unknown session, -32700 parse error.
  Async backends are driven with asyncio.run(); running the layer inside an
  existing event loop raises ValueError (documented, pinned by test).
- scripts/acp_agent.py: stdio wrapper. DEFAULT backend = SubagentBackend
  bridging core/subagent.runner.run_subagent (tool_allowlist=[], role=None):
  one prompt = one subagent task, the FINAL reply becomes ONE
  agent_message_chunk; StreamingBehaviour SPIKE LIMITS: no incremental
  step/tool streaming (run_subagent returns a single string - faithful wiring
  is a core contract change = the wiring decision), no cancellation, no
  fs/* agent-to-client requests, no auth (the process IS the operator).
  --echo: offline scripted backend, no model needed.
- LIVE PROBE (one process, --echo): initialize echo -> session id -> 3 streamed
  updates (agent_message_chunk, tool_call_update, agent_message_chunk) ->
  stopReason end_turn -> unknown session -32602 -> parse error -32700 ->
  unknown method -32601 -> cancel then prompt -> stopReason cancelled. Exit 0.
- tests: tests/test_acp_adapter.py - 11 tests green.
- LESSON (Windows pipe protocol servers): `for line in sys.stdin` uses buffered
  read-ahead and STALLS a pipe client until EOF; scripts/acp_agent.py must use
  sys.stdin.readline() per line (comment in place).
- NOT DONE on purpose: no wired agent route (fusion_mode stays single), no
  revocation story, no flag. Rollback = delete core/acp/, scripts/acp_agent.py,
  tests/test_acp_adapter.py (+ the P6-B files keep independently).

### P6 battery.
- ruff clean (core/acp, scripts/acp_agent.py, tests/test_acp_adapter.py added);
  tests/test_doc_refs OK; eval gates: mock (SUITE: ALL OK) and offline
  (SUITE: ALL OK); full discover: 3454 tests, failures=23, errors=2, skipped=2.
  Baseline was 22F+2E in the same environment-dependent families
  (test_runner/test_file_tools/test_diagnostics/test_guard_persistence/
  test_kb_policy/test_kb_residency/test_oversize_write/test_plan_guard/
  test_project_builder/test_tenant_isolation) - the +-1 is an environment
  flake inside those families; no new-family failure appeared.

### Up next (per roadmap, unchanged).
- P0 = LIVE EVAL, needs the user at the keyboard. P3 = model-swap hot lane.
  P7 (critic) stays UNSCHEDULED behind F2 evidence. Owed flag arming:
  context.snip.enabled (offline A/B), sessions.fork_enabled (anytime).
- Wiring decisions the user owns after this entry: (1) fuse P5 flag into
  routes (armed), (2) skill-trust surfaced in the UI or telemetry, (3) ACP
  rollout = streaming bridge rework of core/subagent.runner.py or buffered
  single-chunk mode first.


### P3-SH swap hygiene - SHIPPED 2026-10-21. (P3 slice 1; the swap path itself stays a restart, that is llama-server's single -model reality and is not a bug.)
- **Census first (whole-tree sweep, subagent-verified file:line):** a local main-lane
  model swap = `POST /control/switch` -> `core/state.py _load_locked`: kill the
  llama-server process, spawn a new one (or the watchdog does the same after a death);
  three kinds of learned per-model state silently carried onto the NEW process:
  (1) `core/context_budget.py` `_STATE` (prompt window/anchor) + `_TPS_STATE` (tok/s
  EMA) - keyed by lane name only; `reset()` even has "useful on a model (re)load"
  in its docstring with NO caller, and `routes/agent/setup.py` comments admit the
  switched-to model stays pinned to the previous window;
  (2) `routes/common/llm_stream.py` `_TOOL_CHOICE_UNSUPPORTED` - keyed
  `local:<base_url>` where the base_url is the FIXED port 8090, so a swap to a
  tool-capable model inherits the old model's rejected-tool_choice verdict forever;
  (3) lane trackers like `core/lane_health.py` are keyed by (lane, category) and
  deliberately untouched (lane-level history is not per-model).
- **Shipped:** a swap-hook seam in `core/state.py` (`_SWAP_HOOKS` +
  `register_swap_hook` + `notify_swap()` called at the end of `_load_locked`
  only after a HEALTHY load; each hook guarded so a broken one can never fail
  the load). Owners register at module import (dependency direction: hooks
  point INTO state, no import cycle):
  - `core/context_budget.py::_on_model_swap` -> `reset(None)` + `reset_tps(None)`
    (both fully-pruned states, all lanes - a new process invalidates everything
    learned about the old one).
  - `routes/common/llm_stream.py::_purge_local_tool_choice_verdicts` -> difference-
    update away every `local:` verdict; cloud verdicts survive (their provider key
    bound a different endpoint untouched by the swap).
- **Verification at merge (all green):** red-first 7 tests in
  `tests/test_swap_hygiene.py`: registration dedup; `notify_swap()` pinned in the
  `_load_locked` source; a REAL `_load_locked` (faked spawn/health/VRAM preflight)
  fires the hooks exactly once on success, ZERO on a failed load, and a raising
  hook never breaks the load; owner-reset tests for both registrants. The tests
  restore a `_BASELINE_HOOKS` snapshot rather than clearing the global list (their
  first draft destroyed the canonical registrations for later test modules in the
  same process - caught immediately by running the three files together). ruff
  clean; tests/test_doc_refs OK; both eval gates no-regressions; full discover
  3461 tests, failures=22, errors=2, skipped=2 - the PRE-EXISTING baseline exactly.
- Firing on the FIRST boot load (no process before it) is deliberate: a fresh
  process has nothing stale and the hook work is one dict clear.

### Snip A/B slice - SHIPPED 2026-10-21, measured, gate lives in the eval suite. FLAG STILL UNARMED.
- Where: `tests/eval_mock.py Part 6c` (`run_snip_scenario`) wired into
  `run_mock_suite` (include_snip), `_summarize_suite`, `compare_baseline`
  (router-style: was-ok -> now-failing is a REGRESSION), and `tests/eval_agent.py`
  (`--no-snip`, print, results, raw-verdict gate).
- The scenario killed two confounders it had to design around, each of which
  produced a misleading A/B before it was found:
  1. **The snip layer runs on the MAIN lane only.** `routes/agent/stream.py`
     keeps the executor on a fresh view (`build_executor_view(msgs)`,
     `executor_fresh_context` default on) and never snips `msgs` there - an
     all-executor run made the A/B halves byte-identical. The slice pins tool
     steps to main via `role_map.agent.tool_step = main`;
     `core/cloud/config.py` role_map is read FROM DISK (`_read_json(_get_config_file())`,
     cached in `_CACHE`) and ignores the live `APP_CONFIG` dict, so the scenario
     monkeypatches `core.cloud.config._read_json` for the duration and restores it
     in `finally` (never writes to the real app.json).
  2. **The runaway guard eats repetitive synthetic filler.** A 4000-char body of a
     repeated unit phrase trips `core/agent_loop/degeneration.py::find_runaway`
     (a 40+ char phrase 6x in the tail window) mid-stream: the cut happens BEFORE
     the tool-call deltas arrive, the step escalates, tools never run. Narration
     is now position-stamped (`alpha0 beta1 gamma2...`, one token per position) -
     unique prose ~4000 chars that no guard can legally object to.
- Measurement: the mock transport records every chat POST body length
  (`_current['prompt_chars']`) and `T(..., narrate=True)` streams narration
  deltas + tool-call deltas in the SAME assistant turn, the way real models
  emit. 60 steps x ~4200 unique chars (~84k estimated tokens) crosses the
  50000-token snip trigger around step 37; n_ctx is raised to 262k during the
  scenario so COMPACTION (0.70 threshold) can never pre-empt the A/B, and
  `context_budget.reset(None)` afterwards keeps the 120s /slots probe cache from
  leaking into later slices. Both halves must complete the same 60 tool calls
  and the marker answer; the gate demands >= 15% fewer payload bytes ON; the
  verdict is rounded to 0.1%.
- **Result: 36.6% fewer request bytes over the identical run**
  (ON 5,976,614B vs OFF 9,430,742B, same 62 model turns, both halves ok) - way
  past the 15% gate and stable across two consecutive runs.
- NOT DONE on purpose: `context.snip.enabled` stays FALSE in `config/app.json`.
  This slice proves the transport saving but is NOT the live answered/synth
  A/B the C-snip entry demanded before arming; the remaining precondition is
  the P0 live-traffic comparison (armed on/off over real runs' answered/
  synthesized rates), then flip the flag. Rollback before arming = delete the
  flag block; after arming = flip the flag back (no code path).

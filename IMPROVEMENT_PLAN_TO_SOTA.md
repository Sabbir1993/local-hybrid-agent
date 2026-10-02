# Engineering Improvement Plan: Advancing to Tier-1 SOTA (79.8 -> 95+)

> **Target:** Elevation of `a770-dual-runtime` from local workstation agent (Composite Score: **79.8 / 100**, Grade B) to commercial frontier grade (**95+ / 100**, Grade S), directly competitive with **Claude Code**, **Devin / OpenHands**, and **Cursor / Windsurf**.

---

## 1. Executive Summary & Diagnostic Baseline

A rigorous empirical audit combining full unit test execution (1,474 tests, 0 failures, 59.9s) and production telemetry from `usage.db` establishes the baseline. Sample-size caveat, applied from this repo's own standard: the run-level stats rest on **n=66 runs from a single user** (tool-level n=158 for `edit_file`, n=103 for `write_file`), so every percentage below carries roughly ±8–12 points of Wilson uncertainty. The problems are real; the precision is not.

```
[Hardware Orchestration (Vulkan/Dual Arc)]  ==== 94/100  (S- Tier)
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
(Phase score deltas below — +4.7, +4, +4, +3.5 — are planning estimates, not derived measurements. Nothing in the telemetry converts "git checkpoints" into "+4.0". Read them as sequencing, not arithmetic.)

### Empirical Production Bottlenecks (from `usage.db`)
1. **Mutation fragility (CORRECTED 2026-10-02).** This section originally claimed `edit_file` fails 44% and `write_file` 45% of calls. That number was a measurement artifact: `route_events` logs a `tool_start` row *and* a completion row per call, and the failure count treated every `tool_start` (tool_ok NULL) as a failure — double-counting each call and counting the start of a successful call as a failure. Pairing events by `(run_id, step)` gives the real rates: **`edit_file` 6.3%** (6/95, Wilson 95% [2.9%, 13.1%]) and **`write_file` 5.2%** (3/58, [1.8%, 14.1%]); all tools together **4.8%** (60/1248, [3.8%, 6.1%]). `edit_file` is already at 93.7% success, so the ">90% edit success" target in §1.1 is met before any change — matching improvements cannot be the lever the original diagnosis assumed. Method note: `scripts/`-style pairing, not `SUM(tool_ok=0)/COUNT(*)`, is the only correct query; `perf_report.py:54` still uses the row-count form and inherits the artifact.
   By measured failure rate the real outliers are elsewhere: `spawn_agent` 45.5% (5/11), `browser_click` 41.7% (5/12), `analyze_image` 33.3% (2/6), `browser_eval` 23.3% (10/43), `read_file` 7.5% (17/227). Small n on the browser/spawn rows — bands are wide, so treat them as "look here first", not as ranked truth.
2. **Termination Instability:** Only **34.8%** of runs finish cleanly (`answered`). **24.2%** fail to self-terminate and require fallback synthesis, while **16.7%** exhaust step budgets or hit loop repetition guards.
3. **Semantic Code Blindness:** Grep and flat file reading fail to track cross-file definitions and call hierarchies, leading to blind token-wasting reads.
4. **Host Execution Exposure:** Application-level path checking protects file traversal, but approved shell execution runs bare on the host operating system.
5. **Unmeasurable failures (fixed in E-edit):** tool failures recorded `tool_ok` with no reason, so "edit_file fails 6.3%" could not be split into no-match / ambiguous / drift / verify-failed — every attribution query came back blank. `route_events.tool_err` now stores a fixed code (`core/route_log.py: classify_tool_result`, codes only, never text). Historical rows stay unattributed; new ones are.

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
* **Problem:** Local models (Qwen-3B/Spark-4B/27B) frequently produce `old_string` blocks that do not match the file: wrong content, ambiguous (non-unique) matches, or line drift after earlier edits in the same run. Note the correction to earlier drafts of this plan: the edit path is `core/agent_tools/edit_engine.py` + `core/agent_tools/file_ops.py` (there is no `core/file_tools/editing.py`), and whitespace/indentation tolerance **already exists** (`core/agent_tools/schemas.py:84`, whitespace-ignored matching in `edit_engine.py:174`) — yet `edit_file` still fails. Whitespace is not the root cause; content mismatch is. Fuzzy tiers address drift, unified-diff input addresses model ergonomics (models write diffs more reliably than they copy blocks). **Scale correction:** the "~44% of calls" figure behind this item was a telemetry artifact (see bottleneck 1 above); the measured rate is 6.3%. The mechanisms below still help the residual failures and future tool_err data will say how much, but this is a robustness item, not the rescue the original draft assumed.
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
  - **Hard constraint (D4 lesson):** fan-out is a bypass vector. Concurrent dispatch must preserve per-call permission and lane gating — the equivalent of `parallel_spawn_eligible()` in `core/agent_loop/policy.py` for the concurrent path — or it reopens the plan-mode bypass that phase D closed. A negative control (plan-mode + concurrent fan-out stays denied) is part of acceptance, not optional.

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

### 2.2 Structured Task DAG vs Prose Plan Mode — PREMISE PARTLY FALSE; instrumentation shipped 2026-10-02
* **Problem:** Plan tracking exists (`create_plan` / `get_plan` / `update_plan_item` — all live in telemetry) but items are prose status strings. The model cannot programmatically verify progress against planned tasks.
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
- **Verification result:** "the model cannot verify progress" is only half true, and the second bullet is **already implemented**. `plan_guard.check_update` refuses to mark a step done while an earlier one is open (`plan_guard.py:76-80`) and refuses two steps in progress at once (`:71-75`); `tool_update_plan_item` refuses out-of-range and bad statuses; `focus_message` re-injects the current step each turn. That is a **stricter** constraint than a DAG with `deps` — a linear chain cannot express parallel branches, it only guarantees order. Pinned by `tests/test_plan_adherence.py::ExistingEnforcementIsRealTests` so a future DAG change cannot quietly drop it.
- **What was genuinely missing: any measurement of whether runs finish their plans.** `route_events.outcome` records what a turn *did*; nothing recorded whether the plan was completed. So "should plans become a DAG?" had no number on either side.
- **Shipped instead** (`plan_guard.run_summary` + `routes/agent/run.py`): a run-level code in `route_runs.detail` — `plan:done`, `plan:open:N/M`, `plan:failed:N`, `plan:failed:N/open:K/M`. Counts only, never step text (a step can quote a path); an unrecognised status counts as open so a new status can never read as done. `detail` now **composes** codes (`synth:…|plan:open:1/3|identical_result:…`) instead of first-wins, so a synthesis and an unfinished plan can both be visible.
- **Validation:** `tests/test_plan_adherence.py` (13 tests: every code shape, bounded/no-text, unknown-status, existing enforcement, DB write) plus an end-to-end probe over the real mock harness — 28 tasks, `run_end` spied: `write_and_verify → plan:done`, `plan_complete → plan:done`, and `append_build` / `forbidden_name_blocked` / `plan_guard_violation → plan:open:2/2`. 1588 tests OK, ruff clean, JS 16/16, both CI eval gates green.
- **Honest caveat:** those three `plan:open:2/2` results are artifacts of *scripted* turns that simply never call `update_plan_item` — they are not evidence that real runs abandon plans. The measurement exists now; the number needs live traffic.
- **Still open, deliberately:** the `deps` graph itself. Adding it would mean a DB column plus API/tool/UI surface, and it would *relax* the ordering that currently guarantees correctness. Do it only if the live `plan:open` rate says linear order is what is holding runs back — and not before.

---

## Phase 3: Code Intelligence via Tree-Sitter & Vector RAG
*Target Milestones: Memory score 65 -> 90. Match Cursor/Claude Code repository comprehension. Projected Score: **92.5**.*

### 3.1 Tree-Sitter Abstract Syntax Tree (AST) Symbol Engine
* **Problem:** Relying on `grep` forces the agent to read 5-10 full files to trace a single function call, rapidly exhausting context windows.
* **Supply-chain note:** tree-sitter wheels are a new native dependency. They go through the same process `ldap3` did: version floor in `requirements.txt`, hash-locked (`requirements.lock`), `pip-audit` clean, `--check-lock` green — before any code lands.
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
* **Platform realism note:** this box is Windows + dual-Arc Vulkan. GPU passthrough for local inference through Docker Desktop is unresolved, and several companion/devtools flows assume host paths — containerizing execution breaks them until they are abstracted. As written (weeks 7–8 alongside swarms + browser agent), this phase is not credible. Re-scoped below as an investigation spike first, build second.
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
| **Phase 1** | Week 1–2 | `core/agent_tools/edit_engine.py`, `core/agent_tools/file_ops.py` | Fuzzy Myers diff patching; `edit_file` failure dropped < 10% |
| **Phase 2** | Week 3–4 | `core/agent_loop/execution.py`, `core/agent_loop/loop_guard.py` | Git worktree snapshot & rollback; Structured Task DAG |
| **Phase 3** | Week 5–6 | `core/code_intel/`, `core/memory/`, `core/agent_tools/` | Tree-Sitter AST indexer; `sqlite-vec` hybrid semantic search |
| **Phase 4** | Week 7–8 | `core/shell_tools.py`, `core/subagent/runner.py` | Docker container sandbox; Critic-Reviewer multi-agent bus |

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

Salvageable items from this plan, mapped onto the measurement-first sequencing already in motion (R1–R3 done: real-vector slice, fallout gating, lexical normalization). Nothing below starts without its stated prerequisite. Item codes (R9, V4 …) continue the existing track numbering.

### R9 — Code search via tree-sitter (§3.1, best item in this plan) — DONE 2026-10-02
- **Prerequisite:** R4 grid complete (scoring knobs settled — a second ranker on top of moving weights measures noise).
- New slice, same harness pattern as the KB slice: a staged code corpus (renames, cross-file callers, overloads) with symbol-lookup questions; `find_symbol_definition` / `find_symbol_callers` / `get_file_outline` as the tools under test.
- New dep through the ldap3 process (floor + hash-lock + audit + `--check-lock`) before code.
- **Acceptance:** symbol recall@3 ≥ 0.9 on the staged corpus, p95 < 50ms over 1,000 files, red-first tests in `tests/test_code_intel.py`, baselined + regression-gated like the KB slice.
- **Result (measured):** 8-question fixture slice recall = 1.0 (gate, baseline-merged); scale probe 1,000 files: cold index 2.2s / 5,399 symbols, warm query p95 10.4ms (acceptance <50ms PASS; the first implementation re-parsed per query at 329ms — fixed by a stamp-keyed in-process cache, invalidation pinned by `tests/test_code_intel_cache.py`: identity on repeat, edit/add/delete/dual-root staleness). Baseline update merge fixed to also preserve `offline` (a `--no-offline` update had nulled it, which would have left CI's offline gate comparing against nothing) and `code` sections. 1508 tests OK, both CI eval gates green. TS (.ts/.tsx) intentionally skipped (no grammar locked).

### R10 — AST chunking experiment (§3.2 remainder)
- **Prerequisite:** R5 (char-chunking evidence) complete, so AST boundaries have a measured baseline to beat.
- A/B AST-boundary chunks vs the R5 winner on the KB slice (recall + fallout, both modes). sqlite-vec/ANN enters ONLY if brute-force latency shows up in measurement first — no index without a measured latency problem.
- **Acceptance:** recall up or fallout down with no regression in the other metric, else AST chunking stays an experiment, not the default.

### E-edit — Unified-diff input + fuzzy tiers (§1.1, corrected diagnosis) — DONE 2026-10-02
- Red-first tests in `tests/test_file_tools.py` (drifted blocks, ambiguous matches fail loudly rather than fuzzy-applying wrong).
- **Acceptance (two levels, both required):** unit tests green AND live `usage.db` edit_file fail rate drops with Wilson bands that exclude the old rate. Unit-proof without telemetry shift is theater.
- **Result:** both tiers implemented and unit-proven — `apply_diff` (unified-diff input: `diff` arg on `edit_file`, hunks located + verified, applied **atomically** so a failed hunk leaves the file untouched; content drift and ambiguous hunks refused loudly) and tier-3 fuzzy block matching (`FUZZY_THRESHOLD=0.85`, ≥2-line blocks, applied only at exactly one candidate location). 1537 tests OK, ruff clean, both CI eval gates green.
- **Threshold priced, not asserted** (drift probe over 60 real functions from `core/`, injecting controlled drift): tiers 1–2 applied 60/227 drifted blocks, tiers 1–3 applied 123/227 — **+63 correct edits, 0 wrong edits** (negative controls: 0/118 wrong/adjacent blocks matched). Per class: rename-local 0→30/60, reword-string 0→26/49, drop-line 0→7/58. Sweep table: thr=0.70 → positives 0.744, thr=0.85 → 0.551, thr=0.95 → 0.498, **negatives stayed 0.000 at every threshold** — 0.85 kept as the conservative pick since the negatives have no measured near-miss headroom to justify the looser gate.
- **Telemetry fix (prerequisite for ever closing the live half):** `route_events.tool_err` now records a fixed failure code (`classify_tool_result`, codes never text). Without it the acceptance was unprovable — the 6 real failures in `usage.db` carry no reason and cannot be replayed (tool args were never persisted).
- **Live half still open:** needs fresh traffic to compare `edit_file` fail rate and its `tool_err` mix against the corrected 6.3% [2.9%, 13.1%] baseline. With n≈95 calls per era, a 6→2 improvement will not clear the Wilson bands — the next measurement needs either more traffic or per-class counts, not a bigger point estimate.

### E-dispatch — Parallel read-only fan-out (§1.2, with the D4 constraint) — PREMISE FALSIFIED, not built (2026-10-02)
- Per-call permission/lane gating preserved; negative control (plan-mode + concurrent fan-out stays denied) in the same test file as the feature.
- **Acceptance:** 40% step-latency cut claim becomes a measured number on multi-read tasks, zero permission bypasses, no new flake in the mock suite.
- **Measured before building, and the population isn't there:** pairing completion events per `(run_id, step)`, **91.9% of tool steps issue exactly one call**; only 8.1% issue ≥2, and only **5.2% are multi-call *and* all read-only** (63 steps, 148 calls, all single-lane). Tool time is also a rounding error against model time: an executor step averages 11.5s (perf_report) while a local companion read is ~0.1s, so overlapping the entire addressable set saves well under 1% of step latency — not 40%. The 40% figure assumed tool calls dominate step latency, which is true of cloud APIs and false of a loopback companion.
- **And the risk is on the wrong side:** the other 35 multi-call steps are **mixed read-only + mutating** (e.g. `read_file` + `edit_file` in one step). Concurrency there would race a read against the write it is supposed to inform. Serial execution is the safer default, and the D4 lesson (fan-out as a bypass vector) applies to us here, not just to sub-agents.
- **Kept for the record, not scheduled:** if step latency ever becomes dominated by tool time (remote companion, high-latency devices, MCP tools), revisit with the same per-call gating requirement.

### E-checkpoint — Git checkpoints + rollback (§2.1) and structured DAG (§2.2, on existing plan tools) — PREMISE FALSIFIED for §2.1, DAG part still open
- Loop-guard hook + `revert_to_checkpoint` internal action; DAG validation on top of `create_plan`/`update_plan_item`, not greenfield.
- **Acceptance:** red-first rollback test; live synthesized/outcome shift measured in `usage.db` with bands (this is what moves the 24.2% synthesized number, and only measurement proves it).
- **§2.1 premise not supported by the data.** The claim is that agents corrupt files via ad-hoc repair and then degenerate. Measured: degeneration markers (`identical_result` / near-repeat codes in `route_runs.detail`) appear in **1 of 67 runs**. Runs that mutated files end *answered* at 35% (12/34) versus 33% (11/33) for runs that mutated nothing — mutating files is not associated with failure at all. So rollback-on-corruption has almost nothing to catch.
- **What the same data does show (bigger, unexamined):** the run outcomes are two populations, not one. `error` runs have **p50 = 1 step** (10 runs, 14.9%) and `synthesized` runs **p50 = 2 steps** (16 runs, 23.9%) — together **27% of all runs die almost immediately**, with the model request returning HTTP 200 and the final step being plain model text (no tool call), mostly in `main-cloud-rest-local`. The long-run ceilings are a separate, smaller group (`max_steps` p50=60, `budget` p50=91, `timeout` p50=55, `loop_near_repeat` p50=28, 11 runs total).
- **Cause was unmeasurable, so it is now measured:** `outcome='error'` runs recorded no cause at all — `_classify_step_error` tells the user, but `route_runs.detail` only ever carried loop detail. Added `_error_class` (`routes/agent/run.py`): the exception **type** as a bounded code (`err:ValueError`), never the message (messages carry local paths). Historical rows stay unattributed; new ones are, which is the precondition for investigating the 15% step-1 crash population at all.
- **Still open:** the §2.2 structured-DAG validation is independent of the falsified §2.1 premise and remains unbuilt.

### Investigate next: the step-1/2 early-exit population (27% of runs) — instrumentation DONE, diagnosis pending traffic
- Evidence so far: no failed HTTP requests; the router passes to the main lane at step 0 in 8 of 18 early exits versus 1 of 23 answered runs, and the final step is model text with `category=action`. Both point at the main-lane first-turn path rather than at tools, permissions, or the model server.
- **Attribution built (2026-10-02):** `validate_and_finalize_response` (`core/agent_loop/sandbox.py`) already names the cause — `synthesized response from successful tool actions`, `synthesized web search results`, `synthesized execution output`, `extracted answer from model reasoning`, `extracted full model reasoning`, `fallback confirmation`, `PASSIVE_REFUSAL_NOTE` — and `run.py` streams it to the client as `event: validated`, but it never reached `route_runs.detail`. All 16 recorded synthesized runs were therefore one indistinguishable bucket: a stall the model refused to do, a `fallback confirmation` that invented an answer, and a real synthesis from tool output all looked alike. Now mapped to a fixed vocabulary by `_synth_detail` (`routes/agent/run.py`) and stored: `synth:tool_actions`, `synth:web_results`, `synth:execution`, `synth:reasoning_answer`, `synth:reasoning_full`, `synth:fallback_confirmation`, `stall:passive_refusal`, with `synth:other` for anything unrecognized. Looked up by value, never copied, so a future note carrying user text cannot reach retained storage; `validated` records nothing so the interesting rows stay visible. Both outcome paths record it (plan path and main path).
- **Known undercount, deliberately not "fixed":** on the main path a synthesis keeps `outcome = stop_reason`, so synthesized answers there are recorded as `max_steps`/`loop`/`timeout`, not `synthesized`. The `synth:` detail code is the only record of them. Changing the outcome vocabulary would move `BAD_OUTCOMES`, the router tuner's baseline, and the quality rollup — a telemetry decision, not a bug fix, so it is left alone and recorded here.
- **Next:** the `err:`, `synth:` and `stall:` codes need fresh traffic before any diagnosis is possible. `python scripts/early_exit_report.py` is the diagnostic: it pairs calls correctly (completion rows only, `tool_start` reported separately as dangling), splits the populations, attaches cause codes, prints Wilson bands on every rate, and labels the step-0 router split as a hypothesis. It currently reports 18/67 early exits, all 18 unattributed, and says so rather than implying a diagnosis. Verified against the live database; the report's own `attribution_ready` check is what told us the codes have not been recorded yet.
- **To actually collect it** (needs a human at the keyboard — no credentials are stored in the repo, deliberately):
  1. `python server_manager.py --models-dir <models> --port 8000` with a profile loaded (see §14 of `PROJECT_KNOWLEDGE.md`),
  2. create the dedicated `eval` user (no MFA — the runner cannot answer TOTP) owning a throwaway workspace, activate it,
  3. `python tests/eval_agent.py --live --live-user eval --live-password '...'`,
  4. `python scripts/early_exit_report.py`.
  Cloud keys are unset in `config/app.json` (`cloud.main: null`), so live runs will be local-only unless a provider is configured — that changes which lane the runs exercise, so record the mode when comparing.

### Explicitly NOT scheduled
- **Critic-reviewer swarm (§4.2):** gated behind report-only evidence per the F2 rule. No build until disagreement predicts defects.
- **Container sandbox (§4.1):** re-scoped as an investigation spike (Docker Desktop + Vulkan passthrough feasibility for llama-server lanes; inventory of host-assuming flows). Build phase only if the spike resolves.
- **Playwright agent (§4.3):** after the sandbox question is answered — an authenticated browser without isolation is new attack surface, not a feature.

# Agent Gap Analysis — A770-Dual Runtime

> **What this document is:** an evidence-based review of the codebase answering
> three questions — (1) how close is this to being a real AI *agent* today,
> (2) what's still missing, and (3) what's good and bad about the current design.
> Every claim is verified against the code (branch `excel_support`, HEAD `2cd8162`,
> with the uncommitted working tree). Companion docs: `README.md`,
> `PROJECT_KNOWLEDGE.md`, `TESTING_LOG.md`.

---

## 1. Executive summary

The repo is **two products stacked in one FastAPI process**:

| Layer | What it is | Status |
|---|---|---|
| **A. Hardware orchestration** | Launches / tunes / health-checks `llama-server` (Vulkan) on 2× Intel Arc A770 | Mature, measured, documented |
| **B. Agent platform** | Autonomous multi-step tool-using agent with SSE streaming, tool registry, multi-lane model routing, plan mode, permissions, persistence | **~70% of a real agent** — the loop, tools, and safety gates exist; memory, planning structure, sandboxing, concurrency, and testing are missing or stubbed |

**Verdict:** this already *is* an agent (perceive → decide → act → observe loop
with 10+ builtin tools, web access, MCP, skills, shell, vision). What keeps it
from being a *complete, trustworthy* one is concentrated in five areas:

1. **Memory is fake** — the embedding model is configured but never called; `search_memory` is word counting.
2. **No structured planning** — plan mode emits prose; there's no task list, progress tracking, or re-planning.
3. **No sandbox or auth** — `run_python`/`run_shell` execute on the host; every HTTP endpoint is unauthenticated.
4. **Brittle heuristics everywhere** — escalation/refusal/loop detection is English keyword matching; JSON repair is regex surgery.
5. **Zero tests, single-process global state** — nothing catches regressions, and two concurrent agent runs would corrupt each other.

Everything else (context compaction, parallel tool calls, browser automation,
sub-agents, packaging) is incremental on top of that foundation.

---

## 2. What the agent already has (verified inventory)

| Capability | Implementation | Where |
|---|---|---|
| Agent loop (max 30 steps) | SSE `text/event-stream` with per-step lane selection | `routes/agent.py::agent_run` |
| Builtin file/code tools | `list_files`, `read_file`, `grep`, `write_file`, `edit_file`, `run_python`, `read_file_chunk`, `list_diff`, `revert`, `analyze_image`, `search_memory` | `core/agent_tools.py`, `core/file_tools.py` |
| Tool registry | Uniform `register/schemas/async_run` across all sources (builtin/web/skill/mcp/plugin/shell) | `core/registry.py` |
| Web tools | `web_fetch` (HTML→text extractor), `web_search` (DuckDuckGo scrape) | `core/web_tools.py` |
| Skills | `skills/<name>/SKILL.md` convention; token-cheap name injection + on-demand `read_skill`; hot reload | `core/skills.py` |
| MCP client | stdio + streamable-HTTP transports; tools bridge as `mcp__<server>__<tool>` | `core/mcp.py` |
| Plugins | `plugins/<name>/plugin.py` with `add_tool/add_system_prompt/on_event`, fault-isolated | `core/plugins.py` |
| Shell | `run_shell` with permission modal (allow / always / project / deny), persisted allow patterns, forbidden-command sanity list | `core/shell_tools.py`, `routes/agent.py` |
| Plan mode | Read-only tool set (`PLAN_MODE_TOOLS`) + plan prompt | `routes/agent.py:130` |
| Multi-lane routing | main (27B) / executor (3B) / vision (VL-3B) / needle (CPU router, 0 VRAM); greeting short-circuit; auto-escalation with `delta_reset` | `core/small_model.py`, `routes/agent.py` |
| Vision | `analyze_image` via Qwen2.5-VL; attachment image support | `core/small_model.py::describe_image_file` |
| Document intelligence | Excel/CSV/PDF/PPT/Word extraction at upload + `read_file_chunk` pagination | `core/file_tools.py` |
| Persistence | projects / sessions / messages in `projects.db`; per-request token+tps metrics in `usage.db` (with orchestrator attribution + cache-token tracking) | `core/db.py` |
| Ops | OpenAI-compatible proxy (any client works), on-demand auto-start, watchdog restart, orphan cleanup, keepalive (WDDM demotion fix), GPU telemetry | `server_manager.py`, `core/state.py`, `core/gpu.py` |
| UX agent surface | Workspace rail + diff viewer, permission modal, abort button, `[DOWNLOAD:]` markers, slash/`@` menus, interactive question cards (grill-me) | `static/js/app.js`, `ui.html` |
| Degeneration defense | repeated-line/sentence/phrase detection, tool-call scrubbing from user-facing text, response validation/synthesis | `core/agent_loop.py` |

### The classic agent loop, scored

A general-purpose agent is usually judged on: **perceive → plan → act →
observe → reflect → remember → (learn)**. Where this project stands:

| Phase | Score | Evidence |
|---|---|---|
| Perceive | ✅ strong | Document extraction, image analysis, web fetch/search, workspace tree |
| Act | ✅ strong | 11 builtin tools + shell + MCP + plugins + skills, sequential execution |
| Observe | ⚠️ partial | Tool outputs fed back (20k char cap) but no streaming tool output, no live shell view |
| Plan | ⚠️ weak | Plan mode = read-only exploration + a text plan; no structured plan object, no progress tracking, no re-planning |
| Reflect | ⚠️ weak | Loop detection + escalation exist, but both are keyword heuristics, not model-based judgment |
| Remember | ❌ missing | No semantic memory; embedder configured but never called; no cross-session recall; no conversation compaction |
| Learn | ❌ missing | Nothing persists lessons/preferences/facts between sessions |

---

## 3. What's missing to make it a full AI agent

### A. Memory & retrieval — the biggest gap

1. **The embedder is dead weight.** `config.json` configures
   `nomic-embed-text-v1.5` on port 8093 and `core/small_model.py:143` even
   launches it with `--embedding` — but **no code anywhere calls the
   embeddings endpoint**. It's VRAM reserved for a capability that doesn't exist.
2. **`search_memory` is not memory.** `core/agent_tools.py:237` scores files by
   counting query words (`text.lower().count(w)`) over workspace files ≤2MB.
   Its tool description claims it searches "past session titles" — it doesn't.
3. **No vector store / RAG pipeline.** No FAISS / Chroma / sqlite-vec, no
   chunk→embed→retrieve over project docs or code, no relevance-ranked context
   injection. Attachment content is injected raw at 12k chars.
4. **No cross-session or long-term memory.** `projects.db` stores messages, but
   nothing ever summarizes or retrieves them; each session starts from zero
   knowledge of prior work.
5. **No conversation compaction.** The agent loop replays the full `messages`
   list every step with no trimming/summarization; on long runs this overflows
   the executor's 24k context. `MAX_TOOL_OUTPUT` (20k chars) is the only lever.
6. **No workspace index.** `tool_grep`/`search_memory` do a fresh `rglob` scan
   of every call (capped at 2MB/file, 100 hits) — fine for small projects,
   unusable for real codebases.

**Build order:** wire the already-running embedder into a small vector store →
index workspace + past-session summaries → replace `search_memory` with hybrid
(lexical + semantic) recall → add auto-compaction when messages exceed a token
budget.

### B. Planning & task decomposition

1. **No structured plan object.** Plan mode (`routes/agent.py:130-141`) only
   restricts tools and asks for a numbered prose plan. There's no todo list the
   loop tracks, ticks off, or re-derives; "proceed" just re-runs without a plan.
2. **No progress tracking / re-planning.** A failed step triggers keyword-based
   escalation to the main model — the only recovery mechanism. There is no
   "step 3 failed, revise the plan" path, no partial-result summaries.
3. **No sub-agents / delegation.** One monolithic loop; no child agents for
   research/coding/verification roles, no context isolation between them.
4. **No parallelism.** Tool calls within a step are executed sequentially; no
   parallel tool calls, no background tasks, no futures.
5. **No autonomy triggers.** The agent only ever runs when a user sends a
   message — no scheduler/cron, no webhooks, no file-watch or event triggers,
   no queued jobs.

### C. Safety, sandboxing & access control

1. **Host-level code execution, unauthenticated.** `run_python` executes
   arbitrary Python directly on the host; `run_shell` uses `subprocess` with
   `shell=True`. There is **no authentication or API key on any endpoint** —
   anyone on the LAN can `POST /agent/run`, answer shell-permission prompts
   (`/agent/permission`), or flip capabilities.
2. **The forbidden-command list is decorative.** `core/shell_tools.py:30`
   blocks exactly four hardcoded substrings (e.g. `format `, `rd /s /q c:\\`).
   `rd /s /q d:\\`, `Remove-Item -Recurse -Force`, or any PowerShell equivalent
   sails through; the allow patterns are plain fnmatch wildcards.
3. **No resource or egress limits.** `config.json` ships
   `capabilities.shell.timeout_s: 0` (**unlimited**) and
   `agent.exec_timeout_s: 0`; `run_python` has no network-egress control, so
   generated code can exfiltrate workspace files.
4. **No sandbox runtime.** No Docker/container/VM isolation, no job objects,
   no per-tool user scoping, no audit log of tool invocations.
5. **Permission modal is UI-level only.** `_pre_approved` is injected after the
   modal; the fallback `permission_callback` in `core/shell_tools.py:27` is
   declared, documented as "installed by the SSE loop", and **never assigned**
   (confirmed in `PROJECT_KNOWLEDGE.md` §15.2). Direct tool calls outside the
   SSE loop always get denied — the safety net exists only on the happy path.

**Build order:** single-user bearer token + scope flags → make `timeout_s` a
real default (not 0) → replace substring blocklist with deny-pattern matching →
optional container runner for `run_python` → audit log table.

### D. Agent-loop robustness

1. **English keyword heuristics decide control flow.** Escalation, refusal
   detection, and "creation intent" are literal substring lists
   (`"i cannot"`, `"make/create/write/build"`, `"run/install/test"` …) in
   `routes/agent.py` (~386-400) and `core/agent_loop.py` (~230-244). They
   misfire on non-English input, on code containing those words, and on
   legitimate refusals phrased differently.
2. **Regex JSON surgery instead of constrained decoding.** ~100 lines of
   bracket-append/regex-repair (`core/agent_loop.py::safe_parse_and_repair_args`)
   exist because the 3B executor drifts out of the tool-call format. llama.cpp
   supports GBNF/grammar-constrained generation — using it for tool calls would
   delete most of this machinery and the failure class with it.
3. **`max_steps` is a cliff.** Reaching the step cap yields
   `event: done {note: 'max steps reached', text: ''}` — no summary of what was
   completed, what failed, or what to try next.
4. **No retries/backoff.** A transient HTTP error or an empty completion from a
   llama-server child just fails the step; nothing re-queues it.
5. **Loop detection is shallow.** Repeated lines/sentences/phrases only; there
   is no detection of *semantic* cycles (same tool+args attempted repeatedly,
   oscillating between two states).
6. **Response synthesis can mask failure.** `validate_and_finalize_response`
   can replace a non-answer with "Task completed. All requested operations have
   been processed in the workspace." — a fabricated success signal.
7. **No per-step token accounting feeding decisions.** The loop never checks
   context pressure before calling the LLM; overflow shows up as degraded
   generation rather than a planned compaction.

### E. Tools & integrations still missing

1. **Search backend is a scrape.** `web_search` scrapes DDG's HTML page — it
   breaks if the markup changes and gets rate-limited; the
   `capabilities.web_search_api_key` slot is an `if api_key: pass` placeholder
   (`core/web_tools.py:132`). Brave/Tavily swap-in is unimplemented.
2. **No browser automation.** No Playwright/headless rendering — JS-heavy
   pages, SPAs, and screenshot-driven verification are out of reach.
3. **No native git/code tools.** Git is only reachable through `run_shell`
   strings; no structured `git_diff`, `git_commit`, or repo-aware search tool.
4. **No streaming tool output.** Shell/Python tools return only after
   completion — a 10-minute build silently blocks one agent step with no
   interim output, and with `timeout_s: 0` there's no cutoff either.
5. **Plugin hooks are half-wired.** `core/plugins.py` defines `before_tool` and
   `session_start` and dispatches them via `fire_hook()`, but the loop only
   ever fires `after_tool` (`PROJECT_KNOWLEDGE.md` §15.7).
6. **MCP lifecycle is static.** Servers connect once at startup (background
   task); a crashed MCP server is not reconnected, and per-server restart
   requires a manager restart.
7. **No artifact/rendering tools.** No screenshot-the-workspace, no chart
   generation, no notebook-style output rendering; only the `[DOWNLOAD:]`
   marker convention.

### F. Context engineering

1. **One hardcoded mega-prompt.** `AGENT_SYSTEM_PROMPT` (`core/agent_loop.py:16`)
   is a 16-rule blob written to constrain a small model; it's static for every
   task — no dynamic assembly based on task type, workspace contents, or lane.
2. **No token budgeting per lane.** The UI shows context usage for the main
   model via llama-server `/slots`, but the agent loop itself never counts
   tokens or estimates cost before a call.
3. **No few-shot tool-call examples.** The executor lane gets rules but no
   worked examples, which is exactly why the format-repair machinery exists.
4. **Attachment context is raw injection.** 12k chars of extracted text pushed
   into the last user message; no ranking, no deduplication against the
   workspace copy, no header/section awareness.

### G. Concurrency & state management

1. **Module-level singletons everywhere.** `_active_project`, `_ws_changes`,
   `permission_callback`, `registry`, `state`, and both SQLite connections are
   process globals. Two simultaneous `/agent/run` requests would share one
   "active project", overwrite each other's change snapshots, and race on the
   same workspace — nothing queues or isolates concurrent agent runs.
2. **Session state is not per-request.** The notion of "current project" is a
   server-wide variable set by the UI, not a property of the conversation.
3. **Shared SQLite connections.** Both DBs are opened at import time with
   `check_same_thread=False` and manual commits from any coroutine/thread; safe
   today only because traffic is effectively single-user serial.
4. **Workspace change tracking is volatile.** `_ws_changes` (what powers
   `list_diff`/`revert`) is in-memory only — cleared on project switch and lost
   on restart, so "revert" silently stops working after a restart mid-project.

### H. Evaluation, testing & observability

1. **Zero automated tests.** No pytest in `requirements.txt`; `scratch/`
   ad-hoc scripts are gitignored; no CI. For ~4k lines of Python and ~2.7k of
   JS, every agent-behavior change is verified by hand.
2. **No linting / type checking / formatting config.** No `pyproject.toml`,
   ruff/mypy, or ESLint for the 2,675-line `app.js` (which already contains a
   duplicate function definition bug — `submitGrillAnswers` is defined twice).
3. **Print-based logging.** All diagnostics go to stderr via `print(...)`;
   no structured logging, log files, or levels.
4. **No agent trace persistence.** Step timelines, tool calls, and lanes stream
   to the UI but aren't stored in a queryable form; debugging a bad run means
   scrolling a chat transcript.
5. **No eval harness.** No task suite to score agent success rate, no
   regression tests for the escalation heuristics, no before/after comparison
   when swapping the executor model. `usage.db` tracks *throughput* well but
   says nothing about *quality*.

### I. Portability, packaging & ops

1. **Machine-bound.** `core/small_model.py:127` hardcodes
   `E:\AI\llama-vulkan` (small models ignore the profile's `llama_bin_dir`),
   `core/small_model.py:28` hardcodes the `E:\AI\workspace` fallback,
   `core/config.py:24` hardcodes this box's iGPU LUIDs, and every profile
   carries absolute model paths and `gpu_devices: [1, 2]`.
2. **Windows-only.** PowerShell scripts, `taskkill`, `shell=True` semantics,
   and a Windows-specific forbidden list; no POSIX path.
3. **No packaging.** No `pyproject.toml`, no Dockerfile, no installer, no
   version/release process; deps are `>=` ranges with no lockfile.
4. **Repo hygiene.** `server_manager.py.bak` (101KB) is committed to git; the
   README is stale twice over (documents only the orchestration layer, and its
   "honesty check" claims the code never ran on real hardware — `TESTING_LOG.md`
   disproves that); `.claude/skills/frontend-design/` is an empty dir that
   makes git emit warnings.
5. **Config handling is try/except-pass.** Malformed `config.json` silently
   falls back to defaults with one stderr line — no schema validation.

### J. Agent UX gaps

1. **Edits have no review gate.** File writes/edits apply immediately; only
   shell commands get the permission modal. There's no "review diff → approve"
   flow, and revert doesn't survive a restart (see G.4).
2. **No mid-run user interaction.** The interactive question cards (grill-me)
   are a one-shot pattern; the agent cannot pause and ask a clarifying
   question mid-loop the way modern coding agents do.
3. **No run cost/budget visibility.** Steps stream to the UI, but there's no
   token-per-step budget meter or "you're at 80% of context" warning.
4. **No live tool output view.** Long shell commands show nothing until done
   (tied to E.4).

---

## 4. Good sides (what's genuinely done well)

1. **The autotune philosophy is right.** `autotune.py` doesn't guess — it
   empirically sweeps `tensor-split` / `--n-cpu-moe` with `llama-bench` on the
   actual hardware and writes the winners back into the profile. The README's
   reasoning about layer-split vs row-split on a PCIe 3.0 bottleneck is
   correct and cites the upstream bug reports it depends on.
2. **Real measured performance work.** `TESTING_LOG.md` documents a genuine
   WDDM VRAM-demotion investigation (14.44 t/s → 7.7 t/s after idle, ruled out
   thermal throttling with evidence) and a working keepalive fix. Most hobby
   LLM stacks never get this far.
3. **Smart multi-lane economics.** Greetings short-circuit to a plain chat
   (no tools), the needle router can serve a confident tool call on CPU with
   zero VRAM, the 3B executor handles simple steps, and escalation re-runs a
   step on the 27B with a `delta_reset` so the UI never shows garbage. This is
   real cost/latency engineering for consumer GPUs.
4. **A clean tool-registry architecture.** One `ToolRegistry` with
   `source`-tagged tools (builtin/web/skill/mcp/plugin/shell), per-source
   enable/disable from the UI at runtime, and schema-only "deferred"
   registration for slow MCP servers. Plugins are fault-isolated by design
   ("one broken plugin never breaks the server").
5. **A real, hand-rolled MCP client** covering stdio *and* streamable HTTP,
   with proper JSON-RPC session handling and `mcp__server__tool` bridging —
   interoperability with the wider tool ecosystem for ~280 lines of code.
6. **The skills convention is token-elegant.** Only skill *names* go into the
   system prompt; bodies load on demand via `read_skill`; skills hot-reload
   with no restart; installing a skill via `npx skills` auto-consolidates into
   `skills/`.
7. **A permission system that actually ships.** Shell commands pause the SSE
   loop for a modal decision (allow once / always / this project / deny),
   patterns persist to `config.json` or the project row, and there's a
   hardcoded always-forbidden sanity list.
8. **Plan mode exists** with a genuinely read-only tool set, not just a prompt
   asking nicely.
9. **Document intelligence is a differentiator.** Excel/CSV/PDF/PPT/Word
   extraction at upload, `read_file_chunk` pagination for big files, and the
   `[DOWNLOAD: file]` marker → UI download button round-trip make this a
   practical office-doc agent, not just a code agent.
10. **The SSE event contract is well-designed.** `step / lane / thought /
    delta / tool_call / tool_result / delta_reset / validated / done` gives
    the UI everything it needs, including mid-run model swaps and abort.
11. **Ops maturity beyond its size.** On-demand model auto-start, watchdog
    restarts, orphan-process cleanup at boot, idempotent shutdown wrapped in
    `asyncio.shield`, per-request tps logging with orchestrator attribution
    and KV-cache token tracking in `usage.db`.
12. **Defense-in-depth hygiene in the tool layer.** Workspace path-escape
    checks (`_ws_resolve`), 20k tool-output caps, 2MB file-scan caps,
    non-interactive env for child shells (`CI=1`, stdin=DEVNULL), tool-call
    text scrubbed out of user-facing content.
13. **Documentation culture.** `PROJECT_KNOWLEDGE.md` is an honest,
    architect-level doc that even lists its own bugs; `TESTING_LOG.md` records
    raw measurements. Rare and valuable.
14. **Fully local and dependency-light.** Zero cloud calls, zero agent
    framework — just FastAPI + httpx. The agent stack works with the network
    unplugged (web tools are optional and toggleable).

---

## 5. Bad sides (weaknesses, risks & debt)

1. **Single-process global-state architecture.** One "active project", one
   change-tracking dict, one shared SQLite connection. Concurrent agent runs,
   or even a second browser tab, can corrupt each other's state (see G.1).
   This caps the product at "one user, one workspace, serially".
2. **Heuristic spaghetti as agent intelligence.** Escalation, refusal,
   creation-intent, and action-intent detection are scattered substring
   lists across `server_manager.py`/`routes/agent.py`/`core/agent_loop.py`.
   They're English-only, brittle, untestable, and grow linearly with every
   new failure mode — the classic path to an unmaintainable agent.
3. **The memory story is a mislabel.** A tool named `search_memory` doing
   word-count scoring, plus an embedder burning config/VRAM for an
   integration that was never written, is worse than having neither — it
   misleads the model and the maintainer.
4. **Security posture is trust-based.** No auth, host-level `run_python`,
   `shell=True`, four-substring blocklist, unlimited timeouts by default,
   LAN-open permission answering. Fine for a hardbox single-user setup;
   unsafe the moment it's exposed or shared.
5. **The 3B executor is the load-bearing wall.** Its format drift is why
   ~200 lines of repair/sanitization exist; every capability added to the
   system prompt taxes a model that can barely hold the rules. Capability
   ceiling is model-shaped, not code-shaped.
6. **Zero automated testing.** The most complex, heuristic-heavy component
   (the agent loop) has no tests at all; regressions in escalation,
   validation, or repair are invisible until a user hits them.
7. **Dead and duplicated code paths.** `server_manager.py.bak` committed,
   `permission_callback` imported but never assigned, `before_tool`/
   `session_start` hooks never fired, `AGENT_CORE_TOOLS` bypassed by the
   lane filter, `submitGrillAnswers` defined twice, tool dispatch split
   between `TOOL_IMPLS` and the registry. Two sources of truth for several
   behaviors.
8. **Machine-bound and Windows-locked.** Hardcoded `E:\` paths, box-specific
   iGPU LUIDs, Vulkan indices `[1, 2]`, PowerShell-only scripts. The repo
   will not run unmodified on any other machine.
9. **Fragile external dependency.** `web_search` scrapes DuckDuckGo's HTML —
   one markup change kills the feature; the paid-API escape hatch is a
   placeholder.
10. **No packaging or deployment story.** No Docker, no installer, no pinned
    dependencies; getting this running on a second machine means archaeology.
11. **Observability stops at throughput.** `usage.db` answers "how many
    tokens/sec" but not "why did the agent fail" — no persisted traces, no
    structured logs, no eval scores.
12. **Volatile safety net.** Diff/revert state lives in RAM only; a crash or
    restart mid-task erases the agent's undo history while files stay
    modified on disk.

---

## 6. Priority roadmap (highest agent-impact first)

| Priority | Item | Why first |
|---|---|---|
| **P0** | Wire the embedder → vector store → real `search_memory` + session recall | The hardware is already paid for; biggest capability jump |
| **P0** | Auth token on all endpoints + sane shell defaults (`timeout_s`, deny patterns) | Trust prerequisite for anything else |
| **P0** | Conversation compaction / token budget in the agent loop | Long tasks currently die by context overflow |
| **P1** | Structured plan tracking (todo list in DB + UI progress) | Turns "prose plan mode" into real task execution |
| **P1** | GBNF-constrained tool calls for the executor lane | Deletes the repair machinery; biggest reliability win |
| **P1** | pytest suite for the agent loop (mock LLM lane like `scratch/test_agent_e2e.py`) | Makes every other change safe |
| **P1** | Graceful `max_steps` wrap-up + retries/backoff | Removes the failure cliffs |
| **P2** | Parallel tool calls + per-session state (drop global singletons) | Concurrency + speed |
| **P2** | Brave/Tavily search key path; git-native tools; fire plugin hooks | Round out the tool surface |
| **P2** | Diff-approval gate for file edits; persistent change journal | Agent UX parity with modern coding agents |
| **P3** | Sub-agents, scheduler/webhook triggers, browser automation | Autonomy beyond single chat turns |
| **P3** | Packaging (pyproject + Docker), unbind from `E:\`/LUIDs, delete `.bak` | Distribution |

---

## 7. Bottom line

As a **hardware-orchestrated local inference stack with a genuinely working
agent on top**, this project is far ahead of typical local-LLM setups: real
benchmarking, real multi-lane routing, real tool ecosystem (MCP/plugins/
skills), real permission UX, and honest documentation.

To graduate from "agent-shaped" to "trustworthy autonomous agent", the work is
concentrated: **give it memory (the embedder is already running), give it a
plan it can track, put walls around code execution, stop deciding control
flow with keyword lists, and write tests for the loop that does all of it.**
Everything else in section 3 is polish by comparison.

---

*Generated from a full read of the working tree at commit `2cd8162`
(branch `excel_support`). Line numbers reference the current working tree and
may shift as it evolves.*






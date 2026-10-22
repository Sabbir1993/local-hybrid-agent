# Improvement Plan — Firecrawl-Aligned Web Tooling, Taskboard, and Web-App Self-Checking

**Status:** Plan only — no code written.
**Repo:** `e:\AI\vulkan-arc\a770-dual-runtime` (branch `2026_10_21`, base commit `fa0d9bb`)
**Audience:** any implementation agent (Claude Code, Cline, Codex). Every step names real files, real functions and real line anchors in this repo, so it can be executed without re-deriving the architecture.

---

## 0. How to use this document

This plan supersedes the earlier ad-hoc plan for the same feature area. It is organised as **four required phases**, one optional phase, and **cross-cutting concerns**. Each phase has:

- a goal and a non-goal,
- the exact files to create / modify (paths are repo-relative),
- the data shapes (JSON schemas) that must be honoured,
- the tests to write,
- an explicit **exit gate** — the observable condition that says the phase is done.

**Execution order matters:** Phase 1 (Taskboard) must land before Phase 2 (Jobqueue) because the Taskboard is the aggregator that renders jobs. Phase 3 (browser audit) is independent of 1 and 2 and can be done in parallel. Phase 4 (scraper) depends on Phase 2 for async crawl jobs, but the single-page `scrape` part does not.

**Definition of "global standard" used in this plan:** Firecrawl's MCP tool surface is treated as the wire-compatible target for web tooling (tool names, argument names, result shapes), and Claude Code's `/tasks` + background-work model is treated as the target for background concurrency. Rationale and evidence are in §2.

---

## 1. Verified current state (audit)

Everything in this table was read in this repo at the commit above. Line anchors are the ones to open first.

### 1.1 Browser automation — exists, runs on the user's machine

| Fact | Anchor |
|---|---|
| Browser tools registered as `browser_navigate`, `browser_snapshot`, `browser_click`, `browser_type`, `browser_press`, `browser_select`, `browser_screenshot`, `browser_console`, `browser_eval`, `browser_wait`, `browser_close` | `core/browser_tools/registry_ops.py:30` |
| `browser_navigate` already supports headless via `show:false` → `headless:true` | `core/browser_tools/registry_ops.py:39` (the `show` schema prop), `core/browser_tools/tools.py:14` (`_headless`) |
| Device presets: desktop, iphone-14, iphone-se, pixel-7, galaxy-s9, ipad | `core/browser_tools/constants.py:7` |
| Playwright-core, Edge/Chrome channel, fresh throwaway context per session, never the user's profile | `companion/browserops.js:38` (`launch`), `:77` (`newContext`) |
| Console + network failures already collected per session | `companion/browserops.js:91-98` (`sess.console`, `sess.network`, `MAX_LOG=200`) |
| ARIA snapshot via `page.ariaSnapshot({mode:"ai"})`, capped 30 000 chars | `companion/browserops.js:127` |
| Ops dispatched from the companion op table | `companion/main.js:583-593` |
| Origin / path safety is decided on the **device**: local dev hosts open freely, other origins need a one-time Allow | `companion/browserops.js:53-70` (`isLocalHost`, `checkUrl`), `companion/policy.js` (`confirmOrigin`, `ensurePath`) |
| `browser.eval` and `browser.navigate` are confirm-ops on the device | `core/companion_bridge/constants.py:24` (`CONFIRM_OPS`) |
| Screenshots are saved to `.agent/screens` and described by the local vision model | `core/browser_tools/bridge.py:65` (`handle_screenshot`), `core/browser_tools/constants.py:2` (`SCREEN_DIR`) |
| Tool output cap for browser results | `core/browser_tools/constants.py:1` (`MAX_RESULT_CHARS = 24000`) |

### 1.2 Web fetch / search / extraction — exists, but single-URL only

| Fact | Anchor |
|---|---|
| Tools: `web_search`, `web_fetch`, `web_search_images` | `core/web_tools.py` (module docstring line 1) |
| Search pipeline: engines `searxng`, `duckduckgo`, `bing`; merge; rerank; page extraction; SQLite cache | `core/web_search/backends.py` (per-engine fetchers), `core/web_search/service.py:57` (`async def search`) |
| Per-host politeness interval + engine intervals already exist | `core/web_search/health.py:47` (`wait_turn`), `core/web_tools.py:14` (`min_interval_s`) |
| Host blocklist for automated access | `core/web_search/blocklist.py` |
| PCI guards: `_pan_egress` (refuse card numbers in outbound URLs/queries), `_mask_inbound` (mask card numbers on inbound page text) | `core/web_tools.py:44`, `core/web_tools.py:51` |
| SSRF guard | `core/net_guard.py` (`BlockedURLError`) |
| Prompt-injection fence for third-party content | `core/prompt_fence.py`, `core/injection_guard.py` |

### 1.3 Firecrawl — exists **only as a remote MCP catalog entry**

| Fact | Anchor |
|---|---|
| `core/mcp_catalog.py:231` registers the hosted Firecrawl MCP server (`https://mcp.firecrawl.dev/v2/mcp-oauth`), scope `user`, with a note that pages are fetched by Firecrawl and not to scrape cardholder data | `core/mcp_catalog.py:231-234` |
| No native scrape / crawl / map / extract implementation exists anywhere in `core/` | confirmed by repo-wide search for `scrape|crawl` |

### 1.4 Background work — partial, and *not* a job system

| Fact | Anchor |
|---|---|
| Run journal with `begin / stage / end / get / open_runs / interrupted / reconcile_interrupted / purge` | `core/runs.py` |
| Concurrent agent runs capped at `max_active_runs_per_user` (default 3), over-cap → `TooManyRuns` | `routes/agent/run_manager.py:209` (limit), `:47` (`TooManyRuns`) |
| Detached run: `/agent/run` runs the loop as a server-side detached task; client can re-attach | `routes/agent/run.py:245` (`agent_run`), `:257` (the detached-task comment); `routes/agent/run_manager.py:206` |
| Parallel sub-agents: `run_parallel_subagents(agent_specs, max_concurrency=4)` | `core/subagent/runner.py:412` |
| Sub-agent shared scratchpad + file locks + summary | `core/subagent/blackboard.py` (`blackboard_write/read`, `acquire_file_lock`, `blackboard_summary`) |
| Parallel tool dispatch inside one step — **read-only allow-list only**; browser/mobile deliberately excluded because sessions are stateful and self-ordered | `routes/agent/constants.py` (`PARALLEL_READ_TOOLS`) |
| Knowledge ingest queue — **serial by design** (one worker; embedding shares the GPU) | `core/kb_queue.py` (`_ensure_worker`, `_run`, `enqueue`, `resume_interrupted`, `sweep_orphans`, `progress`, `snapshot`) |
| Executor lane already carries browser tools for the "look at what you built" loop | `routes/agent/constants.py:74` (`EXECUTOR_TEST_TOOLS`) |
| Tool visibility families + on-demand gating by request wording | `core/tool_surface.py` (`FAMILIES`, `WEB_NAMES`, `filter_tools`, `hidden_families`) |
| Model tool implementations registry | `core/agent_tools/definitions.py` (`TOOL_IMPLS`), `core/agent_tools/__init__.py` |
| Tunable limits table (per-lane token caps, retries, plan caps) | `core/agent_tools/limits.py` (`AGENT_LIMITS`, `CONTEXT_LIMITS`) |
| A working local stdio MCP server exists to copy the pattern from (JSON-RPC 2.0, newline-delimited, protocol `2025-03-26`) | `core/echo_mcp_server.py` |

### 1.5 The gaps this plan closes

1. **No Firecrawl-compatible native surface.** `web_fetch` is one URL → text. There is no `scrape` (markdown/JSON), no `crawl` (site-wide), no `map` (URL structure), no `parse` (documents), no `interact` (live session), no `monitor` (change tracking), no `agent` (autonomous multi-source research).
2. **No unified background-task view.** There are three independent background-ish mechanisms (runs, sub-agents, KB queue) with **no single place** to list them, see progress, attach, or stop them. Frontier harnesses expose exactly this as `/tasks`.
3. **No durable job queue.** Nothing survives a restart as a re-runnable unit of work with progress, logs, artifacts, retries, and cancellation.
4. **Web-app self-checking is model-stepped.** The browser can do everything needed, but the *model* must issue 6-10 separate calls, re-read the snapshot after each, and assemble the finding itself. There is no deterministic one-call audit.

---

## 2. Standard alignment — why these two references

### 2.1 Firecrawl MCP tool surface (web tooling standard)

From `https://docs.firecrawl.dev/mcp-server/tools` (fetched for this plan), the server exposes:

| Tool | Purpose |
|---|---|
| `firecrawl_scrape` | one URL → content; JSON format for structured extraction |
| `firecrawl_map` | discover site URLs |
| `firecrawl_search` | web search; `sources: ["web","alexandria"]` |
| `firecrawl_crawl` + `firecrawl_check_crawl_status` | traverse a site; **async, returns an id, polled by a separate status tool** |
| `firecrawl_agent` + `firecrawl_agent_status` | autonomous multi-source research; async + status |
| `firecrawl_interact` + `firecrawl_interact_stop` | operate a live page (clicks, form fills, navigation); released via `..._stop` |
| `firecrawl_parse` | PDF / document / spreadsheet → LLM-ready text |
| `firecrawl_monitor_*` | create / list / update / run / inspect / delete recurring page-change monitors |
| `firecrawl_find_tools` | semantic discovery over the Alexandria catalogue |
| `firecrawl_research_*`, `firecrawl_developer_search`, `firecrawl_gov_search` | specialised indexes |
| `firecrawl_feedback`, `firecrawl_search_feedback` | optional, env-gated (`FIRECRAWL_NO_SEARCH_FEEDBACK`) |

Key behaviours to copy: **crawl and agent are async with a poll-by-id status tool**; **interact sessions must be explicitly stopped**; **optional tools can be disabled by policy**; **search returns tool descriptors in `data.tools`**.

### 2.2 Claude Code background-work model (harness standard)

From `https://code.claude.com/docs/en/agents` (fetched for this plan), Claude Code has **five** parallel mechanisms, not one: **subagents**, **agent view** (dispatch background sessions, one screen, state at a glance, attach when needed), **agent teams** (shared task list + inter-agent messaging), **projects** (cloud threads that survive the machine being off), and **dynamic workflows** (a script holds the plan; the run list shows phase and how many workers finished). Supporting pieces: `/tasks` lists background work and lets you **check, attach to, or stop** it; `/agents` prints notices pointing at sub-agent files; named background subagents show status in the `@`-mention typeahead.

### 2.3 Gap table — frontier harness vs this repo

| Frontier capability | Claude Code | This repo today | Plan phase |
|---|---|---|---|
| Dispatch background work | agent view / `/tasks` | detached runs only, no aggregate view | **Phase 1** |
| Check status of background work | `/tasks` shows state | per-run endpoints only | **Phase 1** |
| Attach to background work | agent view attach | client can re-attach a detached run, but not from a unified list | **Phase 1** |
| Stop background work | `/tasks` stop | run cancel exists, job cancel does not | **Phase 1 + 2** |
| Durable, resumable jobs | dynamic workflows resume | none | **Phase 2** |
| Shared task list between agents | agent teams | blackboard (scratchpad), not a task list | **Phase 1 (deferred slice)** |
| Inter-agent messaging | agent teams | none (blackboard only) | out of scope here; noted |
| Crawl a site | — | none | **Phase 4** |
| Scrape a page to markdown/JSON | — | `web_fetch` → text only | **Phase 4** |
| Map a site's URLs | — | none | **Phase 4** |
| Parse a document | — | `pdf_read` exists, no doc/xlsx parse | **Phase 4 (parse slice)** |
| Live browser interaction | — | `browser_*` tools exist (step-by-step) | **Phase 3** |
| One-call web-app audit | — | none (model must step through) | **Phase 3** |

### 2.4 Naming decision (this is a contract)

Native tools are named **exactly** like Firecrawl's MCP tools so a client can swap the remote server for the native one without changing its prompt or its code:

`firecrawl_scrape`, `firecrawl_map`, `firecrawl_search`, `firecrawl_crawl`, `firecrawl_check_crawl_status`, `firecrawl_agent`, `firecrawl_agent_status`, `firecrawl_interact`, `firecrawl_interact_stop`, `firecrawl_parse`, `firecrawl_monitor_create`, `firecrawl_monitor_list`, `firecrawl_monitor_update`, `firecrawl_monitor_run`, `firecrawl_monitor_delete`.

Two constraints on this decision:

1. **Tool names must not collide with the remote MCP server.** When the hosted Firecrawl MCP is enabled, its tools are exposed as `mcp__<server>__firecrawl_scrape` (see `core/tool_surface.py` — `mcp__` names pass the family filter untouched). Native tools keep the bare `firecrawl_*` name. If both are enabled, the native one is disabled for that session and the remote one is used; the tool description says so. **Never register two tools with the same bare name in the same registry.**
2. **`firecrawl_search` already exists in spirit** as `web_search` (`core/web_tools.py`). Keep `web_search` as-is — it is a *different* product (SearXNG-backed multi-engine search with rerank and caching, already integrated with the KB). Implement `firecrawl_search` as a thin adapter over `web_search` that adds the Firecrawl response envelope (`data`, `sources`, `tools`) rather than replacing it.

---

## 3. Phase 1 — Taskboard (`core/taskboard/`)  ← build first

**Goal:** one place that lists *everything* running in the background, shows live progress, supports check / attach / stop, and is exposed both as an HTTP API and as model-facing tools.

**Non-goal:** durable job execution (that is Phase 2). Phase 1 *adapts* existing background mechanisms; it does not add a new kind of worker.

### 3.1 Why first

Phase 2 introduces durable jobs. If the Taskboard lands first, jobs get a home for free, and the "does the frontier harness do this?" question is answered incrementally rather than in one big risky change. Phase 1 also fixes a real usability gap **today**: a user cannot see the three detached runs and the KB queue at the same time.

### 3.2 Files to create

```
core/taskboard/__init__.py      # public API re-exports
core/taskboard/registry.py      # the in-process registry of live task handles
core/taskboard/model.py         # TaskRecord dataclass + JSON serialisation
core/taskboard/sources.py       # adapters: runs, sub-agents, kb queue, jobs
core/taskboard/service.py       # list/get/stop/attach orchestration
core/taskboard/tools.py         # model-facing tools (task_list, task_check, task_stop)
routes/taskboard/endpoints.py   # HTTP: GET /tasks, GET /tasks/{id}, POST /tasks/{id}/stop, POST /tasks/{id}/attach
routes/taskboard/__init__.py
```

### 3.3 The unified record

```json
{
  "id": "run-7f3a91",
  "kind": "run" | "subagent" | "job" | "kb_ingest",
  "title": "Check the login flow",
  "state": "queued" | "running" | "done" | "error" | "cancelled" | "interrupted",
  "progress": {"done": 3, "total": 10, "stage": "clicking submit"},
  "created_at": 1730000000.0,
  "updated_at": 1730000060.0,
  "user_id": 12,
  "session_id": "s-abc",
  "parent_id": null,
  "can_attach": true,
  "can_stop": true,
  "detail": {"kind_specific": "..."},
  "logs_tail": ["..."],
  "artifacts": [{"name": "shot.png", "path": ".agent/screens/shot.png"}]
}
```

### 3.4 Source adapters (each is a small class with `list()` / `get(id)` / `stop(id)`)

| Source | Backing API | Notes |
|---|---|---|
| `RunSource` | `core/runs.py` (`open_runs`, `get`) + `routes/agent/run_manager.py` (cancel) | state from `runs`; `can_attach=True` (the client already re-attaches to detached runs) |
| `SubagentSource` | `core/subagent/runner.py` (`run_parallel_subagents`) | sub-agents currently have no registry — **add one**: a module-level dict in `core/subagent/runner.py` populated on spawn and cleared on completion, so the Taskboard can see live ones |
| `KbIngestSource` | `core/kb_queue.py` (`progress`, `snapshot`) | state vocabulary already exists (`queued`/`extracting`/`indexing`) |
| `JobSource` | Phase 2 (`core/jobqueue/queue.py`) | stub in Phase 1 returning empty; wired in Phase 2 |

**Design rule:** the Taskboard owns **no** execution state of its own. Every field is read from the owning subsystem. That keeps Phase 1 additive and reversible, and means Phase 2 only needs to add one source.

### 3.5 Stop semantics

`POST /tasks/{id}/stop` must not be a no-op for any source:

- run → the existing run-cancel path in `routes/agent/run_manager.py`;
- subagent → set the cancellation flag the sub-agent loop already checks (find it in `core/subagent/runner.py` — `run_subagent` polls a cancel/abort flag; if none exists, add `core/subagent/runner.py:_CANCEL.set()` checked at each step boundary);
- kb_ingest → remove the queued item; note in the response that a running embedding cannot be interrupted mid-batch;
- job → Phase 2 (`cancel` flag checked between stages).

### 3.6 Model-facing tools

```
task_list {kind?, state?, limit?}   -> the unified list, rendered as compact text
task_check {id}                    -> one record with progress, logs tail, artifacts
task_stop {id}                     -> stop it
```

Register them the way `core/web_tools.py` does (`registry.register` + a schema table) and add them to `core/tool_surface.py`:

- add a new family `"tasks"` with prefix `task_` and keywords `["background", "task", "jobs", "running", "progress", "background task", "in the background"]`;
- keep them inside that family so the schema cost is paid only when the request actually names background work (consistent with the existing on-demand policy in `core/tool_surface.py`).

Also expose the executor-lane subset if the executor needs to poll its own sub-agents.

### 3.7 HTTP API

Mount under the existing agent router conventions (see `routes/agent/base.py` for the router pattern):

```
GET  /tasks?kind=&state=&limit=
GET  /tasks/{id}
POST /tasks/{id}/stop
POST /tasks/{id}/attach     -> returns the re-attach handle (for runs: the run id + stream url)
```

All endpoints must be user-scoped: a task belonging to another user returns 404, never 403 (no existence leak).

### 3.8 Tests

- `tests/test_taskboard.py` — aggregation across sources; ordering; state mapping.
- `tests/test_taskboard_stop.py` — stop reaches each source; cross-user access returns 404.
- `tests/test_taskboard_tools.py` — `task_list`/`task_check`/`task_stop` produce the expected text and respect the tool-output cap.
- Extend `tests/test_tool_surface.py` — the `tasks` family is hidden when the request does not name background work, and revealed when it does.

### 3.9 Exit gate

1. With one detached run, one live sub-agent and one queued KB source active, `GET /tasks` returns three records with correct kinds and states.
2. `POST /tasks/{id}/stop` on a detached run causes that run to finish as cancelled, verifiable in `core/runs.py`.
3. A model turn that says "what is running in the background?" can call `task_list` and read the answer.
4. No existing test regresses (`pytest tests/ -x -q`).

---

## 4. Phase 2 — Durable job queue (`core/jobqueue/`)

**Goal:** a persistent, cancellable, retryable, resumable unit of work with progress, logs and artifacts — the substrate that Phase 4's async crawl and Phase 3's long audits run on.

**Non-goal:** replacing `core/kb_queue.py` (the KB queue is serial on purpose because embedding shares the GPU). The job queue is a *separate* lane for CPU/network work.

### 4.1 Files to create

```
core/jobqueue/__init__.py
core/jobqueue/constants.py     # MAX_WORKERS, DEFAULT_RETRIES, KINDS, STATES
core/jobqueue/models.py        # Job dataclass, JobSpec, JobResult
core/jobqueue/store.py         # SQLite persistence (jobs, job_logs, job_artifacts)
core/jobqueue/queue.py         # enqueue, claim, complete, fail, cancel, retry, resume
core/jobqueue/worker.py        # N workers, per-kind concurrency, cancel polling
core/jobqueue/handlers.py      # registry: kind -> async callable
core/jobqueue/api.py           # service layer used by routes and tools
routes/jobqueue/endpoints.py   # POST /jobs, GET /jobs, GET /jobs/{id}, POST /jobs/{id}/cancel, /retry, GET /jobs/{id}/result, GET /jobs/{id}/events
```

### 4.2 Job kinds

`scrape`, `crawl`, `map`, `extract`, `parse`, `web_check`, `agent_research` — the first four are Phase 4, `web_check` is Phase 3, `agent_research` is Phase 4's `firecrawl_agent`.

### 4.3 Persistence

Use the existing SQLite pattern (see `core/runs.py` `_init/_exec/_rows` and `core/web_search` cache). One file, e.g. `config/jobs.sqlite`.

```sql
CREATE TABLE IF NOT EXISTS jobs (
  id TEXT PRIMARY KEY,
  user_id INTEGER,
  kind TEXT NOT NULL,
  state TEXT NOT NULL,            -- queued|running|done|error|cancelled
  stage TEXT,
  done INTEGER DEFAULT 0,
  total INTEGER DEFAULT 0,
  params TEXT,                   -- JSON
  result TEXT,                   -- JSON summary
  result_ref TEXT,               -- path to artifact / KB source id
  error TEXT,
  cancel_requested INTEGER DEFAULT 0,
  attempts INTEGER DEFAULT 0,
  max_retries INTEGER DEFAULT 2,
  resumable INTEGER DEFAULT 1,
  checkpoint TEXT,               -- JSON: where to resume (e.g. crawl frontier)
  created_at REAL, updated_at REAL, started_at REAL, ended_at REAL
);
CREATE TABLE IF NOT EXISTS job_logs (
  job_id TEXT, ts REAL, level TEXT, message TEXT
);
CREATE TABLE IF NOT EXISTS job_artifacts (
  job_id TEXT, name TEXT, path TEXT, kind TEXT
);
```

### 4.4 Worker rules

- `MAX_WORKERS` default 4, read from `config/app.json` `jobs.max_workers` (mirror `core/agent_tools/limits.py` so a stale config never breaks a run).
- **Per-kind concurrency caps:** network jobs (scrape/crawl/map) may run 4-wide; **browser jobs (`web_check`) are serialised per user** because the companion's Playwright session is shared per machine (`companion/browserops.js` `sessions` map is per companion process, keyed by session id).
- **Cancellation** is checked between stages and between crawl pages (not mid-page); `cancel_requested` flips the state to `cancelled` and the worker returns.
- **Retries** with exponential backoff on transient network errors only (`httpx.TransportError`, timeouts). Programming errors fail immediately.
- **Resume**: on process start, `running` jobs whose worker died are flipped to `queued` (like `core/kb_queue.resume_interrupted` does), and if `checkpoint` is set the handler resumes from it rather than restarting.

---

## 5. Phase 3 — Web-app self-checking (`browser.audit` + `browser_check`)

**Goal:** the harness can check a web app it just built **in one call**, deterministically, and return a machine-readable report the model can act on (and re-run after a fix).

**Non-goal:** replacing the existing step-by-step `browser_*` tools. They stay; the audit is an additional, batch-oriented capability.

### 5.1 Why this is the differentiator

Firecrawl's `firecrawl_interact` operates a live page, but it runs on *Firecrawl's* infrastructure and cannot reach the user's `localhost:5173`. This repo's browser runs **on the user's own machine**, next to the dev server — which is exactly what is needed to test the app that was just built. The audit is therefore something the hosted Firecrawl cannot do at all, and it is the strongest differentiator of the whole plan.

### 5.2 Companion side — new op `browser.audit`

Add to `companion/browserops.js` (and dispatch it in `companion/main.js` next to the existing `browser.*` ops at line ~583).

The op runs inside the existing Playwright session (`getSession(p)`) and returns a **structured report**:

```json
{
  "url": "http://localhost:5173/login",
  "title": "Login",
  "device": "desktop",
  "issues": [
    {"kind": "console_error", "severity": "error", "message": "TypeError: x is not a function",
     "source": "app.js:12", "selector": null},
    {"kind": "http_error", "severity": "error", "message": "GET /api/user 500", "url": "...", "selector": null},
    {"kind": "broken_image", "severity": "warning", "message": "img#logo failed to load",
     "selector": "img#logo"},
    {"kind": "missing_alt", "severity": "warning", "message": "image without alt text", "selector": "img.hero"},
    {"kind": "unnamed_control", "severity": "warning", "message": "button with no accessible name",
     "selector": "button.submit"},
    {"kind": "unlabelled_field", "severity": "warning", "message": "input#email has no label",
     "selector": "input#email"},
    {"kind": "horizontal_overflow", "severity": "warning", "message": "page scrolls sideways by 42px"},
    {"kind": "empty_render", "severity": "error", "message": "page body is empty"}
  ],
  "summary": {"errors": 2, "warnings": 5, "checked": {"images": 8, "controls": 14, "fields": 3}},
  "screenshot_png_b64": "..."
}
```

Checks to implement (all inside one `page.evaluate` for speed, except console/network which are already collected in `sess.console` / `sess.network` at `companion/browserops.js:91-98`):

1. **Console errors / page errors** — already collected; filter `type === "error"` and `type === "pageerror"`.
2. **Failed requests / HTTP ≥ 400** — already collected in `sess.network`; ignore third-party analytics noise via a configurable ignore list.
3. **Broken images** — `img.complete && img.naturalWidth === 0`.
4. **Missing alt text** — `img` without a non-empty `alt`.
5. **Unnamed controls** — `button`, `a[href]`, `[role=button]` with empty accessible name (`aria-label`, `textContent`, `title`).
6. **Unlabelled fields** — `input/select/textarea` with no associated `<label for>`, no wrapping label, no `aria-label`/`aria-labelledby`; placeholder-only is a warning, not a label.
7. **Horizontal overflow** — `document.documentElement.scrollWidth > innerWidth + 2`.
8. **Empty render** — body text length < 20 and no images.

**Implementation rules:**
- The op must run **headless** by default (audit is not interactive) but accept `headless:false` if the user wants to watch.
- It must **not** require a confirm-op (`CONFIRM_OPS` in `core/companion_bridge/constants.py:24`) — an audit only reads the page. `browser.eval` needs confirmation because it runs arbitrary JS; `browser.audit` must use a **fixed, in-file** evaluation (no user-supplied code) so it is safe to auto-approve. Add an explicit `AUDIT_OPS` set if the bridge needs to distinguish it, and keep `browser.audit` out of `CONFIRM_OPS`.
- Respect `MAX_SNAPSHOT_CHARS` and `MAX_RESULT_CHARS` (`core/browser_tools/constants.py:1`) — cap the issue list (e.g. 100 issues) and say how many were dropped.

### 5.3 Server side — `core/webcheck/`

```
core/webcheck/__init__.py
core/webcheck/constants.py     # MAX_PAGES, MAX_FLOWS, issue severities, default device list
core/webcheck/audit.py         # calls companion browser.audit, normalises the report
core/webcheck/flows.py         # flow definition + execution (click/type/expect/navigate)
core/webcheck/report.py        # markdown + JSON report rendering, diff between runs
core/webcheck/tools.py         # browser_audit, browser_check, browser_check_report
core/webcheck/registry_ops.py  # tool schemas
```

### 5.4 Tools

```
browser_audit {
  url: string,
  device?: string,              # default "desktop"; allow ["desktop","iphone-14","pixel-7"]
  screenshot?: boolean,         # default true
  describe?: boolean            # use the local vision model on the screenshot
}

browser_check {
  base_url: string,
  pages?: string[],             # paths to visit; default: ["/"]
  device?: string,
  flows?: [{name, steps: [...]}],
  screenshot?: boolean,
  describe?: boolean
}

browser_check_report {run_id}  # fetch a previous report, optionally with the diff
```

A flow step is one of:

```
{"do": "navigate", "url": "/dashboard"}
{"do": "click",    "ref": "e12"}            # ARIA ref from the snapshot
{"do": "click",    "selector": "#submit"}
{"do": "type",     "ref": "e7", "text": "a@b.com"}
{"do": "press",    "key": "Enter"}
{"do": "expect",   "selector": ".toast", "contains": "Saved"}
{"do": "expect",   "ref": "e3", "contains": "Welcome"}
```

Flows reuse the existing companion ops (`browser.navigate`, `browser.click`, `browser.type`, `browser.press`) — do **not** reimplement interaction in the companion. Only `browser.audit` is new on the device.

**Flow semantics:**
- After each step, re-read the ARIA snapshot (the existing ops return it) so `ref=eN` handles stay valid.
- `expect` failures are **recorded as issues, not raised** — the flow continues, so one broken assertion does not hide the rest.
- A flow that throws (navigation failed, element missing) is recorded as a failed flow with the error message and that flow stops — the remaining flows still run.
- Every flow gets its **own browser session id** so two flows do not share page state. The session id must be derived from the job/task id, not the chat session, so parallel audits do not collide (`core/browser_tools/bridge.py:39` `_session_id()` currently derives from the chat plan context — extend it to accept an explicit override).

### 5.5 The report

Markdown for the model, JSON for the machine:

```markdown
# Web check — http://localhost:5173

**2 errors, 5 warnings** across 3 pages and 1 flow (device: desktop)

## Errors
- [console_error] TypeError: x is not a function  (app.js:12)
- [http_error] GET /api/user → 500

## Warnings
- [broken_image] img#logo failed to load
...

## Flows
- ✗ login: expected "Welcome" in `.greeting` — not found
```

Persist reports under `.agent/webcheck/<run_id>/report.json` and `report.md`; screenshots under `.agent/screens/`.

### 5.6 Re-run / regression

`browser_check` on a previous `run_id` compares against the stored report and adds a **regression section**:

```
## Since last run
+ 2 new errors (http_error on /api/user)
- 1 fixed (console_error on /login)
```

This is the fix→re-verify loop that makes the feature worth having: the model fixes the code, re-runs the same check, and sees whether the error is gone.

### 5.7 Wiring

- Register the tools in `core/agent_tools/definitions.py` (`TOOL_IMPLS`) and expose the schemas via `core/webcheck/registry_ops.py`.
- Add `browser_audit`, `browser_check`, `browser_check_report` to `routes/agent/constants.py` `EXECUTOR_TEST_TOOLS` (the executor lane is exactly where "I built it, check it" belongs).
- **Naming decision:** name them `browser_audit` / `browser_check` / `browser_check_report` (not `web_check_*`) so they join the existing `browser` family in `core/tool_surface.py` (`FAMILIES["browser"]`, prefix `browser_`) and inherit its on-demand gating with **no new family and no new keyword list**. The existing browser keywords already cover "check the app", "does it work", "test the site".
- Update `.agents/skills/webapp-testing/SKILL.md` to prefer `browser_check` for the "check the whole app" case and keep the step-by-step tools for exploration.

### 5.8 Tests

- `tests/test_webcheck_audit.py` — fixture HTML → expected issue list (broken image, unnamed button, unlabelled input, overflow).
- `tests/test_webcheck_flows.py` — a flow with a failing `expect` records the failure and continues to the next flow.
- `tests/test_webcheck_regression.py` — two runs of the same check produce a correct diff section.
- Extend `tests/test_browser_test_flow.py` — `browser_audit` returns the structured report through the bridge.
- A companion-side JS test (the repo has `tests/js/`) for the `browser.audit` evaluation against a local HTML fixture.

### 5.9 Exit gate

1. Against a deliberately broken local page, `browser_audit` reports the planted console error, the 404 request and the broken image.
2. `browser_check` on a multi-page local app returns one report with per-page issues and flow results.
3. A second run of `browser_check` on the same run id shows the regression section.
4. The executor lane can call `browser_check` (it is in `EXECUTOR_TEST_TOOLS`).

---

## 6. Phase 4 — Native scraper, Firecrawl-aligned (`core/scraper/`)

**Goal:** a native implementation that exposes the **exact** Firecrawl MCP tool names and argument names, so the same client code works against the hosted server and against this repo. Tools return the Firecrawl-shaped envelope.

**Non-goal:** re-implementing an HTTP fetcher. `web_fetch` (`core/web_tools.py`) already fetches, guards and extracts. Phase 4 builds the **Firecrawl-shaped layer on top of it**, plus the site-level operations (map, crawl) that do not exist yet.

### 6.1 Files to create

```
core/scraper/__init__.py
core/scraper/constants.py       # formats, defaults, limits, host politeness
core/scraper/convert.py         # HTML -> markdown / html / links / screenshot / rawHtml
core/scraper/scrape.py          # single-page scrape (the core)
core/scraper/map.py             # site URL discovery
core/scraper/crawl.py           # BFS crawl over a site
core/scraper/extract.py         # structured extraction (JSON) from a page
core/scraper/parse.py           # document -> text (pdf/docx/xlsx)
core/scraper/monitor.py         # change detection for a URL
core/scraper/agent.py           # autonomous multi-source research (firecrawl_agent)
core/scraper/tools.py           # the firecrawl_* model tools
core/scraper/registry_ops.py    # tool schemas
core/scraper/client.py          # remote Firecrawl client (optional passthrough mode)
```

### 6.2 The tool surface (exact names)

| Tool | Args | Returns |
|---|---|---|
| `firecrawl_scrape` | `url` (req), `formats?` (`["markdown","html","links","screenshot","rawHtml"]`), `onlyMainContent?` (default true), `includeTags?`, `excludeTags?`, `headers?`, `waitFor?`, `timeout?`, `mobile?`, `actions?` (browser-backed), `location?`, `jsonOptions?` (schema for extraction) | `{success, data:{markdown, html, metadata:{url,title,sourceURL,statusCode}, links, screenshot, json}}` |
| `firecrawl_map` | `url` (req), `search?`, `limit?` (default 5000), `sitemap?` ("include"/"skip"/"only") | `{success, data:{links:[{url, title, description}], sitemapUrl}}` |
| `firecrawl_crawl` | `url` (req), `prompt?`, `limit?`, `maxDepth?`, `includePaths?`, `excludePaths?`, `allowExternalLinks?`, `ignoreQueryParameters?`, `scrapeOptions?` | `{id, status:"queued"}` — **async**, poll with `firecrawl_check_crawl_status` |
| `firecrawl_check_crawl_status` | `id` (req) | `{status:"scraping"|"completed"|"failed", total, completed, credits, data:[...]}` |
| `firecrawl_search` | `query` (req), `sources?` (`["web"]`), `limit?`, `scrapeOptions?`, `categories?`, `tbs?` | `{success, data:{web:[{url,title,description}], tools:[...]}}` |
| `firecrawl_parse` | `file` / `url` (req), `contentType?`, `ocr?` | `{success, data:{content, metadata}}` |
| `firecrawl_agent` | `prompt` (req), `sources?`, `urls?` | `{id, status:"queued"}` — **async** |
| `firecrawl_agent_status` | `id` (req) | `{status, ...}` |
| `firecrawl_interact` | `url` (req), `actions?`, `browser?` | `{session_id, ...}` — must be released with `firecrawl_interact_stop` |
| `firecrawl_interact_stop` | `session_id` (req) | `{success}` |
| `firecrawl_monitor_create/list/update/run/delete` | per Firecrawl | per Firecrawl |

### 6.3 The response envelope

Every tool returns (or renders as) this shape, so a client written against Firecrawl parses it unchanged:

```json
{
  "success": true,
  "data": {
    "markdown": "# Title\n\nbody...",
    "html": "<html>...</html>",
    "metadata": {"url": "...", "title": "...", "sourceURL": "...", "statusCode": 200},
    "links": ["https://..."],
    "screenshot": "https://... or data URI"
  }
}
```

On failure:

```json
{"success": false, "error": "blocked by host policy: example.com"}
```

### 6.4 Reuse map — do not re-implement

| Need | Reuse |
|---|---|
| HTTP GET with SSRF guard | `core/net_guard.py:45` (`check_url`), `:87` (`guarded_get`), `:28` (`BlockedURLError`) |
| Page text/markdown extraction | `core/web_search/` extraction helpers (the `extract` used by `core/web_search/service.py:57`) |
| PCI guard on outbound requests | `core/web_tools.py:44` `_pan_egress` |
| PCI mask on inbound content | `core/web_tools.py:51` `_mask_inbound` |
| Prompt-injection fence on third-party content | `core/prompt_fence.py`, `core/injection_guard.py` |
| Host blocklist | `core/web_search/blocklist.py` |
| Per-host politeness interval | `core/web_search/health.py:47` (`wait_turn(key, min_interval_s)`) |
| Browser rendering (JS pages, `actions`, `screenshot`) | the companion `browser.*` ops — **on the user's machine** |
| Structured JSON extraction | the local model via the existing extraction path (find the module that turns a document into fields) |

### 6.5 `scrape` implementation

1. Validate the URL → `core/net_guard` (SSRF) → `core/web_tools._pan_egress` (PCI).
2. Host blocklist check → `core/web_search/blocklist.py`.
3. Politeness: honour the per-host interval from `core/web_search/health.py`.
4. Fetch. Two paths:
   - **plain HTTP** (default, fast) → HTML → convert.
   - **browser** (when `actions` are given, `waitFor` is set, or the plain fetch yields a JS shell) → companion `browser.navigate` + optional steps + DOM extraction.
5. Convert to the requested formats:
   - `markdown` — reuse the existing HTML→markdown converter (search `core/` for the renderer; `core/doc_ops/md_ops.py` and `core/agent_tools/pdf_tools.py` are the likely homes; if none exists, write `core/scraper/convert.py:html_to_markdown` and reuse it in both places).
   - `html` — cleaned HTML (strip script/style/noscript).
   - `links` — absolute URLs found in `a[href]`.
   - `rawHtml` — the original HTML (capped).
   - `screenshot` — from the browser path only; otherwise omit.
6. Apply `onlyMainContent` (strip `nav`, `header`, `footer`, `aside`, `[role=navigation]`) and `includeTags`/`excludeTags`.
7. Apply the **prompt-injection fence** to the returned content, and the PCI mask.
8. Cap the output (e.g. 100 000 chars for `markdown`) and report what was dropped.

### 6.6 `map` implementation

- Try `sitemap.xml` / `sitemap_index.xml` at the origin (with the same guards).
- Else BFS the site up to `maxDepth` (default 2) and `limit` (default 5000), collecting links, never leaving the origin unless `allowExternalLinks`.
- Return `{url, title, description}` per link (title from the fetched page is expensive — fetch only the first N links, or parse from the link text).
- Politeness and blocklist apply here too.

### 6.7 `crawl` implementation (async, on the job queue)

- `firecrawl_crawl` → `core/jobqueue.api.enqueue(kind="crawl", params=...)` → returns `{"id", "status": "queued"}` **immediately**.
- The job handler runs the BFS and, for each page, calls the scrape path (respecting politeness), updating `done`/`total` on the job so `firecrawl_check_crawl_status` can report progress.
- `includePaths` / `excludePaths` are glob patterns matched against the path.
- `ignoreQueryParameters` strips the query string when de-duplicating.
- The frontier is written to `checkpoint` after every page so a restart resumes instead of restarting (§4.4).
- `limit` (default 100) and `maxDepth` (default 10) are hard caps; `MAX_CRAWL_PAGES` in `core/scraper/constants.py` caps it regardless.
- `firecrawl_check_crawl_status` reads the job and returns the Firecrawl-shaped status object.

### 6.8 `extract` (structured JSON) and `jsonOptions`

- `firecrawl_scrape` with `jsonOptions: {"schema": {...}, "prompt": "..."}` returns `data.json` matching the schema.
- Implementation: fetch → clean → extract the readable text → ask the local model (`core/small_model/` package — see `describe_image_bytes` at `core/small_model/vision.py:19`, used by `core/browser_tools/bridge.py:88`) to produce JSON matching the schema → validate against the schema → return.
- If the model is unavailable, return `data.json = null` and a `warning`, **never** a fabricated object.
- Cap the input text sent to the model (`core/agent_tools/limits.py` `CHARS_PER_TOKEN = 3.5`).

### 6.9 `parse` (documents)

- `file` may be a local path in the workspace or a URL.
- `pdf` → reuse `core/agent_tools/pdf_tools.py` (`pdf_read`).
- `docx` → reuse `core/doc_ops/docx_ops/`.
- `xlsx/csv` → reuse `core/doc_ops/csv_ops.py` / the sheet tools.
- `ocr` → reuse the OCR path from `core/agent_tools/pdf_tools.py` (`pdf_ocr`).
- Output: `{success, data:{content, metadata:{fileName, fileType, pages?}}}`.

### 6.10 `interact`

- Maps onto the **companion** browser ops (`browser.navigate`, `browser.click`, `browser.type`, `browser.press`, `browser.snapshot`) — not on a remote browser.
- `actions` is a list of `{type: "click"|"type"|"press"|"wait"|"scroll"|"screenshot"|"scrape", ...}`.
- Returns `{session_id}`; the session is released by `firecrawl_interact_stop` (mirroring Firecrawl's release semantics) **and** by the companion's idle close (`IDLE_CLOSE_MS = 15 min`, `companion/browserops.js:20`) as a backstop.
- Add an explicit leak guard: a test that opens an interact session and does not stop it, then asserts the session is closed within the idle window.

### 6.11 `monitor`

- `firecrawl_monitor_create {url, ...}` stores a monitor row (SQLite, reuse the job store file or a new `config/monitors.sqlite`).
- A periodic task (a job of kind `monitor_run`, scheduled by a lightweight ticker in `core/jobqueue/worker.py` — **do not** use the OS scheduler) fetches each monitor and compares a content hash.
- `firecrawl_monitor_run {id}` runs it immediately; on change it records a diff summary and an artifact.
- `firecrawl_monitor_delete {id}` removes it.

### 6.12 `firecrawl_agent` (autonomous research)

- Async job of kind `agent_research` (Phase 2).
- Given a `prompt` and optional `urls`/`sources`, the job runs a small loop: search (`web_search`) → scrape the top results → write findings to a scratchpad → repeat until the prompt is answered or the budget is spent.
- The loop should use the **existing** sub-agent machinery: `core/subagent/runner.py` `run_parallel_subagents` is the natural fit (search → scrape → summarise agents in parallel). This avoids writing a second agent loop.
- Budget: max pages, max tokens, max minutes — read from config in the `core/agent_tools/limits.py` style; the job reports progress.
- `firecrawl_agent_status {id}` returns `{status, findings, sources}`.

### 6.13 Remote passthrough mode

`core/scraper/client.py` provides a client for the hosted Firecrawl (`FIRECRAWL_API_KEY`). When the key is configured **and** the native path is disabled by policy, the tools forward to the hosted API instead. This makes the naming contract testable both ways and gives users a fallback. It must never be the default when the native implementation is available.

### 6.14 Wiring

- Register `firecrawl_*` tools in `core/agent_tools/definitions.py` (`TOOL_IMPLS`) and `core/scraper/registry_ops.py`.
- Add a `"firecrawl"` family to `core/tool_surface.py` with prefix `firecrawl_` and keywords `["crawl", "map the site", "extract from the page", "firecrawl", "monitor the page", "scrape this page"]`. Note: `scrape` is already a browser-family keyword in `core/tool_surface.py` — keep the two families separate so "scrape" does not silently enable the whole Firecrawl surface.
- The **plan lane** must NOT get the mutating ones (`firecrawl_interact`, `firecrawl_crawl`, `firecrawl_agent`, `firecrawl_monitor_*`). Add them to the deny list next to `PLAN_MODE_TOOLS` in `routes/agent/constants.py`.
- The remote MCP entry in `core/mcp_catalog.py:231` stays; when it is enabled, disable the native `firecrawl_*` tools for that session (see §2.4 constraint 1) and say so in the tool description.

### 6.15 Tests

- `tests/test_scraper_scrape.py` — markdown/html/links conversion; `onlyMainContent`; tag filters.
- `tests/test_scraper_scrape_guards.py` — SSRF blocked; PCI number in a URL refused; blocked host refused; injected instruction in the page content fenced.
- `tests/test_scraper_map.py` — sitemap path and BFS path; limit; include/exclude.
- `tests/test_scraper_crawl.py` — crawl returns an id immediately; the status tool reports progress; the job resumes from a checkpoint.
- `tests/test_scraper_extract.py` — JSON extraction matches the schema; a failed model call yields `json: null` and a warning, not a fake object.
- `tests/test_scraper_parse.py` — pdf/docx/xlsx → text.
- `tests/test_scraper_interact.py` — interact session lifecycle; stopping releases it; the idle backstop closes a leaked session.
- `tests/test_firecrawl_envelope.py` — every tool returns `{"success": bool, "data": {...}}`; errors return `{"success": false, "error": str}`.

### 6.16 Exit gate

1. `firecrawl_scrape` on a local fixture returns markdown + metadata with the exact envelope, and the same call against the hosted Firecrawl (when a key is present) parses identically.
2. `firecrawl_crawl` returns an id in < 200 ms; the status tool reaches `completed`; the crawled pages are in `data`.
3. A crawl killed mid-way resumes from its checkpoint.
4. Every SSRF / PCI / injection negative test in `tests/test_scraper_scrape_guards.py` passes.

---

## 7. Optional Phase 5 — Local Firecrawl-compatible MCP server

**Goal:** expose the Phase 4 tools over the **MCP stdio protocol** so any MCP client (Claude Desktop, Cursor, another harness) can use them by name, exactly as if the hosted Firecrawl server were configured.

**Why this is optional but worth it:** the repo already contains the reference implementation of the transport (`core/echo_mcp_server.py`) and the catalog mechanism (`core/mcp_catalog.py`). This is ~200 lines and it turns "we have tools named like Firecrawl" into "we *are* a Firecrawl-compatible server".

### 7.1 Files

```
core/firecrawl_mcp_server.py   # stdio JSON-RPC 2.0 server exposing the firecrawl_* tools
```

### 7.2 Behaviour

- Speak newline-delimited JSON-RPC 2.0 on stdin/stdout, protocol version `2025-03-26`, exactly like `core/echo_mcp_server.py`.
- Handle `initialize`, `tools/list` (return the `firecrawl_*` schemas from `core/scraper/registry_ops.py`), `tools/call` (dispatch into `core/scraper/tools.py`), plus `notifications/initialized`.
- **Everything runs in-process in the server process** — no HTTP round-trip to the API server — so the tools must work without a live server. That means the MCP server process needs its own model access for `extract`; if the local model is unavailable, `extract` returns `json: null` with a warning (§6.8 rule).
- Register it in `core/mcp_catalog.py` as a **stdio** server entry alongside the existing remote Firecrawl entry, e.g. `{"name": "firecrawl-local", "transport": "stdio", "command": "python", "args": ["core/firecrawl_mcp_server.py"]}`.
- Tool names on the wire are the bare `firecrawl_*` names, so a client configured against the hosted server can be pointed at this one with **no prompt change**.

### 7.3 Tests

- `tests/test_firecrawl_mcp_server.py` — spawn the server as a subprocess, send `initialize` / `tools/list` / `tools/call` lines, assert the JSON-RPC responses and the envelope shape (mirrors `tests/test_mcp_spawn.py`).

---

## 8. Cross-cutting concerns (apply to every phase)

### 8.1 Security — non-negotiable

| Rule | Where it lives today | Applies to |
|---|---|---|
| SSRF guard on every outbound URL | `core/net_guard.py:45` (`check_url`) / `:87` (`guarded_get`) | scrape, map, crawl, parse, monitor, interact |
| PCI: refuse card numbers outbound | `core/web_tools.py:44` `_pan_egress` | every URL and query built by the new tools |
| PCI: mask card numbers inbound | `core/web_tools.py:51` `_mask_inbound` | every scraped/markdown/crawl payload |
| Prompt-injection fence on third-party text | `core/prompt_fence.py:50` (`fence`), `core/injection_guard.py` | scrape output, crawl output, agent findings |
| Host blocklist | `core/web_search/blocklist.py:80` (`is_blocked`) | scrape, map, crawl |
| Per-host politeness interval | `core/web_search/health.py:47` (`wait_turn(key, min_interval_s)`) | scrape, map, crawl, monitor |
| Local-only path access | `core/agent_tools/workspace.py:47` (`require_device_workspace`), `companion/policy.js ensurePath` | parse (local files), artifacts |
| Tenant isolation (404 not 403) | new | all new HTTP endpoints |
| Tool output caps | `core/agent_loop/tool_output.py`, `MAX_RESULT_CHARS` | every new tool |

### 8.2 Observability

- Every new tool writes to the run journal (`core/runs.py stage(...)`) so `/control/status` shows it.
- Job progress is visible in `GET /tasks` (Phase 1) and on the job SSE stream (Phase 2).
- Long operations log to `job_logs` (Phase 2) — last N lines shown by `task_check`.

### 8.3 Configuration

Add to `config/app.json` (read defensively, like `core/agent_tools/limits.py`):

```json
{
  "jobs": {"max_workers": 4, "default_retries": 2, "crawl_max_pages": 100},
  "webcheck": {"max_pages": 10, "max_flows": 5, "default_device": "desktop"},
  "scraper": {"user_agent": "...", "default_timeout_s": 30, "max_output_chars": 100000},
  "firecrawl": {"mode": "native", "remote_api_key": null}
}
```

`firecrawl.mode` ∈ `native` | `remote` | `off`. `remote` requires `remote_api_key`.

### 8.4 Tool-surface cost

Every new tool adds schema tokens to every step (see the `core/tool_surface.py` docstring: the full registry is 48 tools / ~8 500 tokens). The plan adds roughly **20** tools. Mitigation, applied throughout:

- the `firecrawl_*` family is only offered when the request names crawling/scraping/monitoring;
- the `tasks` family is only offered when the request names background work;
- `browser_audit` / `browser_check` join the **existing** `browser` family, so they cost nothing extra in gating.

Budget check to run at the end of Phase 4: measure the registry token count and compare with the `core/tool_surface.py` docstring figure. If the growth is unacceptable, move the `firecrawl_*` tools behind an **MCP server entry** instead of the built-in registry (Phase 5 does exactly that).

### 8.5 Documentation

- `README.md` — a "Web tooling" section: native vs hosted Firecrawl, how to switch.
- `.agents/skills/webapp-testing/SKILL.md` — prefer `browser_check` for whole-app checks.
- A new `.agents/skills/web-scraping/SKILL.md` — when to use `firecrawl_scrape` vs `web_fetch` vs `firecrawl_crawl`.
- `docs/ARCHITECTURE.md` (if present) — the Taskboard diagram.

---

## 9. Risks and mitigations

| Risk | Impact | Mitigation |
|---|---|---|
| The `firecrawl_*` names collide with the remote MCP server's names in a prompt | Confusing tool selection | Bare native names vs `mcp__server__firecrawl_*`; disable native when the remote is enabled (§2.4) |
| 20 new tools blow the per-step schema budget | Slower, more expensive turns | On-demand families (§8.4); Phase 5 moves them behind MCP |
| A crawl hammers a small site | Blocked / rude | Per-host politeness from `core/web_search/health.py`; `limit`/`maxDepth` caps; `MAX_CRAWL_PAGES` |
| A browser audit triggers a state-changing GET (logout, delete) | Data loss | Audit only reads; forbid `click` in audit mode; `browser_audit` never clicks |
| An `interact` session leaks | Resource leak on the user's machine | Mandatory `firecrawl_interact_stop` + the companion's 15-min idle close + a leak test |
| A crawl job dies mid-run | Lost work | Checkpoint per page; resume on restart (§4.4, §6.7) |
| The Taskboard reads state from four subsystems and drifts | Wrong status shown | The Taskboard owns no state; each adapter is the single reader of its subsystem |
| `browser_check` results are non-deterministic | False positives | Fixed checks in one `page.evaluate`; issues carry selectors so the model can re-verify |
| Extracted JSON is hallucinated | Wrong data presented as fact | `json: null` + warning when the model is unavailable; schema validation |

---

## 10. Execution checklist

Print this and work down it.

### Phase 1 — Taskboard
- [ ] `core/taskboard/model.py` — `TaskRecord` dataclass + `to_dict()`
- [ ] `core/taskboard/registry.py` — live handle registry
- [ ] `core/taskboard/sources.py` — `RunSource`, `SubagentSource`, `KbIngestSource`, `JobSource` (stub)
- [ ] `core/subagent/runner.py` — add a live sub-agent registry + a cancel flag
- [ ] `core/taskboard/service.py` — `list_tasks`, `get_task`, `stop_task`, `attach_task`
- [ ] `core/taskboard/tools.py` — `task_list`, `task_check`, `task_stop`
- [ ] `core/tool_surface.py` — add the `tasks` family
- [ ] `routes/taskboard/endpoints.py` — `GET /tasks`, `GET /tasks/{id}`, `POST /tasks/{id}/stop`, `POST /tasks/{id}/attach`
- [ ] Tests: `tests/test_taskboard.py`, `tests/test_taskboard_stop.py`, `tests/test_taskboard_tools.py`
- [ ] Exit gate §3.9 verified

### Phase 2 — Job queue
- [ ] `core/jobqueue/constants.py`, `models.py`, `store.py`
- [ ] `core/jobqueue/queue.py` — enqueue / claim / complete / fail / cancel / retry
- [ ] `core/jobqueue/worker.py` — N workers, per-kind caps, cancel polling, restart resume
- [ ] `core/jobqueue/handlers.py` — kind registry (start with a `noop` handler)
- [ ] `core/jobqueue/api.py`, `routes/jobqueue/endpoints.py`
- [ ] `job_start` / `job_status` / `job_cancel` / `job_list` tools
- [ ] Wire `JobSource` into the Taskboard
- [ ] Tests: `tests/test_jobqueue*.py`
- [ ] Exit gate §4.10 verified

### Phase 3 — Web-app self-check
- [ ] `companion/browserops.js` — `audit(p)` implementation + the fixed `page.evaluate`
- [ ] `companion/main.js` — dispatch `browser.audit`
- [ ] `core/companion_bridge/constants.py` — confirm `browser.audit` is **not** a confirm-op
- [ ] `core/webcheck/audit.py`, `flows.py`, `report.py`
- [ ] `core/webcheck/tools.py` + `registry_ops.py` — `browser_audit`, `browser_check`, `browser_check_report`
- [ ] `core/browser_tools/bridge.py` — allow an explicit session id
- [ ] `routes/agent/constants.py` — add the three tools to `EXECUTOR_TEST_TOOLS`
- [ ] `.agents/skills/webapp-testing/SKILL.md` updated
- [ ] Tests: `tests/test_webcheck_*.py` + a companion JS test
- [ ] Exit gate §5.9 verified

### Phase 4 — Native scraper
- [ ] `core/scraper/convert.py` — html→markdown/html/links
- [ ] `core/scraper/scrape.py` + `tools.py` + `registry_ops.py` — `firecrawl_scrape`
- [ ] `core/scraper/map.py` — `firecrawl_map`
- [ ] `core/scraper/crawl.py` + the job handler — `firecrawl_crawl` / `firecrawl_check_crawl_status`
- [ ] `core/scraper/extract.py` — `jsonOptions`
- [ ] `core/scraper/parse.py` — `firecrawl_parse`
- [ ] `core/scraper/monitor.py` — `firecrawl_monitor_*`
- [ ] `core/scraper/agent.py` — `firecrawl_agent` / `firecrawl_agent_status`
- [ ] `core/scraper/client.py` — remote passthrough
- [ ] `core/tool_surface.py` — add the `firecrawl` family
- [ ] `routes/agent/constants.py` — deny the mutating tools on the plan lane
- [ ] Tests: `tests/test_scraper_*.py`, `tests/test_firecrawl_envelope.py`
- [ ] Exit gate §6.16 verified

### Phase 5 — Local MCP server (optional)
- [ ] `core/firecrawl_mcp_server.py`
- [ ] `core/mcp_catalog.py` — register the stdio entry
- [ ] `tests/test_firecrawl_mcp_server.py`
- [ ] Exit gate §7.3 verified

### Final
- [ ] `pytest tests/ -x -q` green
- [ ] Schema-token budget measured and documented (`core/tool_surface.py` docstring updated)
- [ ] README + skills updated
- [ ] `git commit` with one commit per phase, each with its tests

---

## 11. Open questions for the requester

These are the decisions that were deliberately left open; each has a recommended default so an agent can proceed without waiting.

1. **Should the native `firecrawl_*` tools replace the hosted Firecrawl MCP entry in `core/mcp_catalog.py`, or coexist?**
   *Recommendation:* coexist; disable native per-session when the hosted server is enabled (§2.4).

2. **Is `firecrawl_agent` (autonomous research) in scope for this pass?**
   *Recommendation:* yes, but implement it **last** in Phase 4 — it depends on the job queue, the scraper, and the sub-agent runner.

3. **Should `browser_check` be allowed to write files (screenshots) into the workspace?**
   *Recommendation:* yes, into `.agent/webcheck/` only, via the existing workspace guard.

4. **Should the Taskboard be exposed in the UI as well as the API?**
   *Recommendation:* API + model tools in this pass; the UI is a separate frontend change.

5. **Should jobs be able to run as a different user (e.g. a service account)?**
   *Recommendation:* no — every job is owned by the user who started it; tenant isolation is tested.

6. **Should `browser_check` be added to `PARALLEL_READ_TOOLS` so the model can check several apps in one step?**
   *Recommendation:* no — the browser session is stateful; parallel audits must be separate jobs with separate session ids (§5.4).















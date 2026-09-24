# PROJECT KNOWLEDGE — Local Agent

> Internal architecture & maintenance reference. Written from a full read of the
> codebase (commit `c334c28` + working-tree changes). This is the *why* and the
> *how it fits together* document. For first-time setup see `README.md`; for
> measured benchmark sessions see `TESTING_LOG.md`.

---

## 1. TL;DR

This repo is **two products stacked on one FastAPI process**:

| Layer | What it does | Lives in |
|---|---|---|
| **A. Hardware orchestration runtime** | Launches / health-checks / restarts / auto-tunes `llama-server` for 2× Intel Arc A770 (Vulkan), and works around a WDDM VRAM-demotion bug that permanently halves throughput. | `autotune.py`, `core/process.py`, `core/state.py`, `core/gpu.py`, `core/profiles.py`, `core/config.py` |
| **B. Local agent platform** | Autonomous multi-step coding agent with streaming SSE, a pluggable tool registry (web / skills / MCP / plugins / shell), multi-lane model routing, plan mode, and project/session persistence. | `core/agent_loop.py`, `core/agent_tools.py`, `core/registry.py`, `core/mcp.py`, `core/skills.py`, `core/plugins.py`, `core/shell_tools.py`, `server_manager.py` |

`server_manager.py` (:8000) is a **thin proxy + control plane** in front of
`llama-server` (:8090). Clients talk OpenAI-compatible HTTP to the proxy, never
to llama-server directly.

**"Dual runtime" means two things at once**, which is a recurring source of
confusion:
1. Dual **GPU** — two A770s split by layer.
2. Dual **model lane** — a big "main" model plus small on-demand
   executor/vision/embedder models on their own ports.

---

## 2. Glossary

| Term | Meaning |
|---|---|
| **Lane** | Which model serves a given agent step: `main`, `executor`, `needle`, `vision`. |
| **Main** | The big model (Qwen3.8-27B) on both A770s, port 8090. |
| **Executor** | Small fast model (Qwen2.5-VL-3B) on a single A770, port 8091. Handles simple/early steps. |
| **Needle / Laya** | Optional CPU-only fast routers (`needle` or `laya` package). Zero VRAM. Configurable in `config/app.json` (`engine: "cactus_needle"` or `"laya"`). Falls back silently if not installed. |
| **Small models** | `executor` / `vision` / `embedder` — on-demand `llama-server` children with an idle reaper. |
| **Tuned block** | The `"tuned"` object inside a profile; written by `autotune.py`. Holds `tensor_split`, `n_gpu_layers`, `n_cpu_moe`. |
| **Capability** | A tool *source*: builtin, web, skill, mcp, plugin, shell. Toggleable wholesale from the UI. |
| **Lane escalation** | Automatically discarding a weak executor response and re-running that step on the main model. |
| **Delta reset** | SSE event telling the UI to throw away already-streamed text (used before escalation / validation rewrite). |
| **Cloud lane** | A lane served by a remote OpenAI-compatible provider instead of a local `llama-server`. Bindings live in `providers.json` (`core/cloud.py`). |
| **providers.json** | Untracked file holding cloud providers (base URLs + API keys) and lane bindings. Written only by the Settings → ☁️ Cloud Models card. |

---

## 3. Hardware baseline (this machine)

Verified in `TESTING_LOG.md` — treated as ground truth by the code:

- **GPU:** 2× Intel Arc A770 16GB (driver 32.0.101.8991), one card on **PCIe 3.0**. i5-13500, 64GB RAM, UHD 770 iGPU.
- **Vulkan indices:** `0` = UHD 770 **iGPU (must be excluded)**, `1` and `2` = the A770s. Therefore **every** profile and `model_configs.json` entry uses `gpu_devices: [1, 2]`.
- **llama.cpp:** Vulkan backend build `b10840` at `E:\AI\vulkan-arc\llama-vulkan` (`config/app.json` → `runtimes.vulkan`).

### Measured performance

| Config | Generation | Prompt eval |
|---|---|---|
| Single A770 baseline | 9.7 t/s | — |
| **Dual A770, tuned (`-sm layer`, `--tensor-split 9,11`)** | **14.44 t/s sustained** | 252.6 t/s (bench) |
| Dual A770 *after* long idle (pre-keepalive) | 7.7 t/s, permanent | — |
| Dual A770 with keepalive ON | 13.8 t/s | — |

Net gain from dual-GPU: **+49% generation, +74% prompt processing.**

### The three hardware findings that shaped the code

**(a) WDDM idle VRAM demotion — the single most important bug.**
~70s after the server goes idle, WDDM demotes the model's pages from VRAM to
system RAM. Memory *does* re-promote over PCIe when generation resumes, but the
process **never returns to full speed** (7.7 t/s forever) until it is
restarted. Cause 2 is a red herring in Task Manager: llama.cpp uses the GPU
**compute** engine, so the default 3D graph reads 0%.

*Fix, implemented:* `core/state.py::keepalive_loop()` fires a **1-token
generation every 25s while idle**, touching the weight pages so demotion never
happens. `state.last_activity` is updated by both the keepalive and the proxy,
so keepalive backs off automatically during real traffic. Cost ~194ms per ping.

**(b) Vulkan device selection.** `GGML_VK_VISIBLE_DEVICES` env var is unreliable
through `Popen`, so `llama-server` is launched with **explicit `-dev Vulkan1,Vulkan2`**.
(`autotune.py` still uses the env var — it works for `llama-bench`.)

**(c) `--tensor-split` separator mismatch.** `llama-bench` parses it with `/`,
`llama-server` with `,`. Getting it wrong silently benches a *single* device —
this was the original "15.3GB loaded on one card" bug.
`autotune.normalize_ts_for_bench()` translates. Profiles always store comma form.

### Other measured notes

- `layer` split mode only; `row` needs a per-layer cross-GPU all-reduce and is badly hurt by PCIe 3.0 (also deprecated upstream).
- First request after launch stalls ~13s on Vulkan shader compilation; the manager's warmup absorbs it.
- `llama-server` WorkingSet ~15.5GB / PrivateMemory ~19GB is **normal** (mmap'd GGUF + demoted pages), not CPU fallback.
- `Get-Counter '\GPU Engine(*)'` costs 2–6s per call → GPU polling is cached (`GPU_QUERY_INTERVAL_S = 4.0`).
- LUID map: `0x04ac733a` = A770 with display, `0x04ab067c` = headless A770, `0x000165f7` / `0x0001665a` = iGPU/other (`IGNORED_IGPU_LUIDS`).
- MoE profile (`-ncmoe`) has upstream load-balancing issues across GPUs (llama.cpp#15136); the escape hatch is manual `--override-tensor`. Not automated.

---

## 4. Repository map

```
server_manager.py        1693 L   FastAPI app: routes + the agent SSE loop. Entry point.
autotune.py               ~300 L   llama-bench grid sweeper -> writes profile["tuned"].

core/                    2847 L across 17 modules (all reusable logic)
  agent_tools.py          368   builtin tool impls + AGENT_TOOLS schemas + workspace sandbox (_ws_resolve)
  agent_loop.py           339   system prompt, degeneracy detection, arg repair, sanitization
  small_model.py          285   SmallModelInstance/Manager (executor/vision/embedder) + Needle router
  mcp.py                  277   hand-rolled MCP client (stdio + streamable HTTP)
  db.py                   232   SQLite: usage tracking + projects/sessions/messages
  web_tools.py            184   web_fetch (HTML->text) + web_search (DuckDuckGo, keyless)
  shell_tools.py          153   run_shell with permission gate + allow-pattern persistence
  profiles.py             145   profile / model_configs.json loading + GGUF -> dynamic profile
  state.py                142   ProxyState (process lifecycle, watchdog) + keepalive_loop
  skills.py               121   SKILL.md discovery/parsing + list_skills/read_skill tools
  monitor.py              108   in-flight request tracking, tok/s EMA, SSE usage extraction
  plugins.py              104   plugin loader + PluginAPI (tools, prompt fragments, hooks)
  registry.py              92   ToolRegistry — the single tool surface
  process.py               82   kill orphans + build_launch_command (the llama-server CLI)
  gpu.py                   77   PowerShell perf-counter query (compute engine), cached
  config.py                75   paths, ports, CONFIG_DEFAULTS, clamp/choice/target tables
  echo_mcp_server.py       63   minimal test MCP server

ui.html                 36 KB    single-page UI (served at GET /)
static/js/app.js      2675 L     all frontend logic (vanilla JS, no build step)
static/js/highlight.js  90 L     small syntax highlighter
static/css/style.css  1439 L     theming (dark/light/cycle)

config.json                      app config: models_dir, small_models, router, agent, capabilities
model_configs.json               per-model saved overrides, keyed by lowercase .gguf filename
profiles/*.json                  full launch profiles (dense + MoE)
projects.db / usage.db           SQLite (gitignored)
skills/<name>/SKILL.md           reusable instruction packs (4 present)
plugins/<name>/plugin.py         drop-in capability modules (1 example: devtools)
scripts/*.ps1                    setup / device-list / download / start helpers
scratch/                         ad-hoc test scripts (gitignored)
```

---

## 5. Architecture & data flow

### Process topology

```
                        ┌──────────────────────────────┐
   browser (ui.html) ──►│  server_manager.py  :8000    │
   OpenAI SDK ─────────►│  FastAPI + httpx proxy       │
                        └───┬──────────────┬───────────┘
                            │              │
        (normal traffic)    │              │ (agent traffic)
                            ▼              ▼
                 llama-server :8090   + small-model llama-servers
                 main model, both         executor :8091 (Vulkan1)
                 A770s, -sm layer         vision   :8092 (Vulkan1)
                                          embedder :8093 (Vulkan1)
                 Needle router (optional, CPU, 0 VRAM)
```

All child processes are spawned by `subprocess.Popen` and owned by the manager.
`lifespan()` (server_manager.py:155) does startup/shutdown, and
`_shutdown_cleanup()` kills llama-server, every small model, and all MCP
servers (idempotent via `_shutdown_done`).

### Startup sequence (`lifespan`)

1. `bootstrap_builtin_tools()` → mirror `agent_tools.TOOL_IMPLS` into the registry.
2. `register_web_tools()`, `register_skill_tools()`, `register_shell_tools()` — each self-disables if its capability flag is off.
3. `load_plugins()` → import `plugins/*/plugin.py`.
4. `kill_orphan_llama_servers()` → taskkill any leftovers, wait 2s for VRAM release.
5. `connect_all_mcp()` launched as a **background task** (so a slow MCP server can't block boot); tools register when it finishes.
6. Load the initial profile JSON if `--profile` was passed.
7. Start `state.watchdog()`, `keepalive_loop()`, and `small_models.start_reaper()`.

Shutdown cancels the three loops first, then runs the blocking teardown in a
thread wrapped in `asyncio.shield` so a second Ctrl+C can't corrupt the kill
sequence.

### Who owns what

| Concern | Owner |
|---|---|
| Main model process lifecycle + watchdog restart | `core/state.py::ProxyState` (global singleton `state`) |
| Small model processes + idle reaping | `core/small_model.py::SmallModelManager` (global `small_models`) |
| Tool registration + dispatch | `core/registry.py` (global `registry`) |
| Request metrics | `core/monitor.py` (`_monitor_state`) |
| Project/session state | `core/db.py` (`_projects_db`, `_usage_db`) |
| GPU telemetry | `core/gpu.py` (`_gpu_cache`, 4s TTL) |
| Current workspace / file-change snapshots | `core/agent_tools.py` (`_active_project`, `_ws_changes`) |
| Pending shell permission prompts | `server_manager.py` (`_perm_pending`) |
| Cloud providers + lane bindings | `core/cloud.py` (reads `providers.json`, falling back to `config.json → provider`) |

### Cloud lanes (`core/cloud.py`, `routes/cloud.py`)

A lane (`main` / `executor` / `vision`) is served **locally** unless its binding in
`providers.json → cloud.<lane>` resolves to a configured provider/model. One helper
decides this everywhere: `cloud.cloud_lane(lane)` → `None` means local.

```jsonc
// providers.json (created/updated only by the UI; gitignored, holds API keys)
{
  "provider": {
    "open router": {
      "npm": "@ai-sdk/openai-compatible",          // stored verbatim, unused at runtime
      "name": "open router",                        // cluster label in the dropdown
      "options": { "baseURL": "https://openrouter.ai/api/v1", "apiKey": "sk-or-v1-…" },
      "models": { "stealth/union-alpha": { "name": "union-alpha", "ctx": 131072 } }
    }
  },
  "cloud": { "main": "open router/stealth/union-alpha", "executor": null,
             "vision": null, "fallback_local": true }
}
```

- **`CloudClient`** (`core/cloud.py`) duck-types `httpx.AsyncClient` (`.stream()`, `.post()`),
  so `_llm_chat_stream` and the `/chat/compact` summarizer work unchanged. It injects
  `model`, `Authorization: Bearer`, and strips llama.cpp-only fields
  (`repeat_penalty`, `grammar`, `min_p`, …) plus `max_tokens: -1`.
- **Endpoint derivation** — `baseURL` ending in `/v1` gets `/chat/completions` appended;
  anything else gets `/v1/chat/completions` (covers `…/openai` style bases).
- **Modes** (`req.mode` from `#agent-engine`): `all-local`, `main-local-rest-cloud`,
  `main-cloud-rest-local`, `all-cloud`. Legacy `main`/`tiered` still accepted.
- **VRAM** — a cloud main lane never starts `:8090`; a cloud executor never warms up the
  local executor. All-cloud runs with **zero** local engines.
- **Truthfulness** — `get_model_info(lane)` returns `source: "cloud"`, `device: "<Provider> (cloud)"`
  and a ☁️ `display`; `/control/status` reports `main_source` / `cloud_main` so the pill
  shows `☁️ Cloud · …` instead of a false "Model unloaded".
- **UI ownership** — the top model dropdown sets the **main** lane (picking a cloud model
  binds it; picking a GGUF clears the binding). Executor/vision are set in the card.

### The proxy (`proxy()`, server_manager.py:1781)

`@app.api_route("/{path:path}")` catches **everything** that isn't a known
route — so `POST /v1/chat/completions` and any other llama-server endpoint just
work (OpenAI SDK, LM Studio clients, curl).

Behaviour:
- If the model isn't running and a profile is selected → **auto-start it** on demand; otherwise 503 `model_not_loaded`.
- Streaming is detected by sniffing the body for `"stream":true` → returns a `StreamingResponse`, otherwise a buffered `JSONResponse`.
- Every request: `monitor_begin` → stream/complete → `monitor_end` + `db_record_request` (usage.db) + a console tok/s line.
- Client disconnect mid-stream is recorded as status **499** rather than dropped.
- Updates `state.last_activity`, which suppresses the keepalive during real work.

---

## 6. The three config layers

**This is the most important thing to understand before editing anything
config-related.** Values are resolved across three files:

```
core/config.py CONFIG_DEFAULTS          (lowest priority — hardcoded fallbacks)
        │
        ▼
model_configs.json                      (per-model saved overrides)
   key = lowercase .gguf filename, e.g. "qwen3.8-27b-q4_k_m.gguf"
        │
        ▼
profiles/*.json                         (highest — the actual launch truth)
   + profiles/<x>.json["tuned"] { tensor_split, n_gpu_layers, n_cpu_moe }
```

### `core/config.py` — the schema of the config system

- **`CONFIG_DEFAULTS`** — fallback for every launch field, plus `llama_bin_dir` / `backend` / `gpu_devices`, which the active `config/app.json` runtime preset (`runtime`, or env `LLAMA_RUNTIME`) overwrites at import.
- **`CONFIG_INT_FIELDS`** — `key -> (min, max)` clamp ranges for integers. `0` means *"omit the flag, use llama.cpp's default"* for `threads`/`threads_batch`/`batch_size`/`ubatch_size`/`n_slots`.
- **`CONFIG_CHOICE_FIELDS`** — enum whitelists: `flash_attn` ∈ (on/off/auto), `kv_cache_type` ∈ (f16/bf16/q8_0/q5_0/q4_0/f32), `split_mode` ∈ (layer/row).
- **`CONFIG_TARGETS`** — `key -> (section, field)`; `None` section = top level of the profile.
- **`MODEL_CONFIG_KEYS`** — the persisted subset written into `model_configs.json`.

### The `tuned` quirk (read this twice)

`n_gpu_layers` and `tensor_split` are *stored inside* the profile's `"tuned"`
object, while every other key sits at the top level. So:

- `_config_for_profile()` (server_manager.py:261) reads `tuned` first, falling back to top level.
- `save_model_config()` (core/profiles.py:98) does the same, so the **effective (tuned) value wins** on save.
- `_standalone_profile()` (server_manager.py:285) deliberately re-nests saved `n_gpu_layers`/`tensor_split` back under `tuned` so the UI round-trip is lossless.

If you add a new config key, you must update `CONFIG_DEFAULTS`, `MODEL_CONFIG_KEYS`,
`CONFIG_TARGETS`, and (if it's numeric/choice) the clamp or choice table — and
add a branch in `_apply_config_update()` if it needs special parsing.

### Config write path (UI → disk → relaunch)

`POST /control/config {updates, persist, restart}`
→ `_apply_config_update()` validates/clamps one key at a time (returns a string error, or `None` on success)
→ `save_model_config()` persists to `model_configs.json`
→ `state.load_profile()` relaunches llama-server unless `restart: false`.
`keepalive_interval_s` is special-cased: applied **live**, no relaunch.
`context_size` is rounded down to a multiple of 8.

### `GET /control/config?model=...`

If `?model=` names a *different* model than the one in VRAM, the endpoint builds
a **standalone profile** from `model_configs.json` + defaults so the drawer edits
the dropdown-selected model rather than the loaded one. `curStatus_model_hint`
makes the saved config survive an unload/refresh.

---

## 7. Launch command construction (`core/process.py`)

`build_launch_command(profile) -> list[str]` is the ONLY place the llama-server
CLI is assembled. Flag order:

```
<llama-server> -m <model_path> -c <ctx> -ngl <n> -dev Vulkan1,Vulkan2
  -fa <flash_attn> --port 8090 --host 127.0.0.1
  [-sm layer --tensor-split 9,11]        # only when len(gpu_devices) > 1
  [-ctk <t> -ctv <t>]                     # from kv_cache_type
  [-t N] [-tb N] [-b N] [-ub N] [-np N]   # each omitted when 0
  [-ncmoe N]                              # only if model_type == "moe"
  --jinja
  [--spec-type draft-mtp -md <draft> --spec-draft-n-max N -ngld auto]   # if MTP + draft exists
  <server_extra_args...>                  # profile passthrough, e.g. ["--metrics"]
```

**The zero-share rule** (process.py:47-50): if `tensor_split` has one segment
per GPU and a segment is `0`, that device is **dropped from `-dev` entirely**
and the zero is removed from the split string. So:

| `tensor_split` | Result on `gpu_devices: [1,2]` |
|---|---|
| `"9,11"` | both GPUs, split 9/11 |
| `"1,0"` | **GPU #1 only** (`-dev Vulkan1`, no split flags) |
| `"0,1"` | **GPU #2 only** (`-dev Vulkan2`, no split flags) |

This is how single-GPU mode is expressed — there's no separate "use one GPU" toggle.

**MTP / speculative decoding:** if `mtp_enabled` and `mtp_draft_path` resolves to
a file that exists, the draft flags are appended; if the path is set but missing,
it logs a warning and silently launches **without** MTP.
`find_mtp_draft()` looks for `mtp-<stem>.gguf` beside the model or in `MODELS_DIR`,
then falls back to any `mtp-*.gguf` whose name shares the model's family prefix.

**`kill_orphan_llama_servers()`** runs at startup and is the reason the manager
can recover cleanly after a crash — it `taskkill /F /IM llama-server.exe /T`s
everything and waits 2s for Windows to release VRAM.

---

## 8. The tool registry & capability system

**This is the core architectural idea of the agent platform.** There is exactly
one tool surface: the global `registry` in `core/registry.py`. Every tool —
no matter where it comes from — is a `RegisteredTool(name, fn, schema, source,
meta, enabled)`.

| `source` | Registered by | Tool naming | Example |
|---|---|---|---|
| `builtin` | `registry.bootstrap_builtin_tools()` | bare name | `read_file` |
| `web` | `web_tools.register_web_tools()` | bare name | `web_search` |
| `skill` | `skills.register_skill_tools()` | bare name | `read_skill` |
| `shell` | `shell_tools.register_shell_tools()` | bare name | `run_shell` |
| `mcp:<server>` | `mcp.connect_all_mcp()` | `mcp__<server>__<tool>` | `mcp__echo__ping` |
| `plugin:<name>` | `plugins.PluginAPI.add_tool()` | `plugin__<name>__<tool>` | `plugin__devtools__timestamp_now` |

Key methods:

- `registry.schemas()` → the OpenAI `tools` array (enabled tools only).
- `registry.async_run(name, args)` → dispatches coroutines via `await`, sync functions via `run_in_executor`. Wraps exceptions into `"error: <Type>: <msg>"` **strings** (never raises), because the result is fed back to the model as a tool message.
- `registry.set_source_enabled(source, bool)` → how the UI's capability switches work.
- `registry.register(..., replace=True)` → every capability registers with `replace=True`, so re-running registration is idempotent.

### Dispatch precedence (`agent_loop.run_tool`)

```python
if registry.get(name) is not None:      # covers ALL sources
    return await registry.async_run(name, args)
impl = TOOL_IMPLS.get(name)             # legacy fallback for the executor core set
...
```

New code should always register into the registry. `TOOL_IMPLS` remains only so
the executor lane's lean tool set keeps working if the registry isn't bootstrapped.

### Builtin tools (`core/agent_tools.py`)

| Tool | Notes |
|---|---|
| `list_files` | glob, capped at 200 hits |
| `read_file` | capped at `MAX_TOOL_OUTPUT` (20,000 chars) |
| `grep` | regex over workspace, skips files >2MB, 100 hit cap |
| `write_file` | auto-infers a path from content if omitted (HTML→`index.html`, code→`main.py`); `MAX_EDIT_BYTES` = 512KB |
| `edit_file` | exact-string replace; **errors if `old_string` matches more than once** unless `replace_all` |
| `run_python` | writes `_agent_run.py` in the workspace and runs it; timeout `agent.exec_timeout_s` |
| `list_diff` | files created/modified this session (from `_ws_changes`) |
| `revert` | restores pre-session content, or deletes if newly created |
| `analyze_image` | async → routes to the vision small model |
| `search_memory` | crude keyword-frequency search across workspace files |

`AGENT_CORE_TOOLS` is the 5-tool subset (`write_file`, `read_file`, `edit_file`,
`list_files`, `run_python`) — note the executor lane **no longer uses it**
(see §9), it now gets the registry-filtered set.

### The workspace sandbox (`_ws_resolve`)

Every filesystem tool funnels through `_ws_resolve(rel)`. It:

1. Rejects nothing initially; strips a leading absolute workspace path if present.
2. Strips `/workspace/`, `workspace/`, `/root/workspace/`, `./workspace/` prefixes (the model habitually hallucinates these).
3. Resolves and verifies the result is inside the active workspace, else raises `PermissionError`.

The active workspace is `_active_project` (set from the UI) → that project's
`workspace_dir` from `projects.db`, else `WORKSPACE_ROOT/<project>`, else
`WORKSPACE_ROOT` (`config.json: workspace_dir`, default `E:\AI\workspace`).

### Web tools (`core/web_tools.py`)

- `web_fetch` — httpx GET (15s timeout, browser UA, follow redirects) → `_TextExtractor` (an `HTMLParser` subclass that drops script/style/nav/header/footer/aside/form/button and keeps readable text + links).
- `web_search` — scrapes `html.duckduckgo.com/html/`, unwraps DDG's `/l/?uddg=` redirect wrapper, returns up to 8 titles/URLs/snippets (6,000 char cap). **Keyless by design**; `capabilities.web_search_api_key` is a reserved slot for a Brave/Tavily swap-in (currently a no-op `pass` branch).

### MCP (`core/mcp.py`)

Hand-rolled client, no SDK dependency. Protocol version `2025-03-26`.

- **stdio transport** — spawns the server as a subprocess, newline-delimited JSON-RPC 2.0 on stdin/stdout. The response reader skips notifications and tolerates non-JSON noise on stdout. `RPC_TIMEOUT_S` 30, `INIT_TIMEOUT_S` 20.
- **http transport** — streamable HTTP with a session-id header; supports `text/event-stream` responses.
- Bridged schemas are converted from MCP `inputSchema` into an OpenAI function schema, description prefixed `[mcp:<server>]`.
- Results stringified by `_stringify_content()` (joins `content[]` text parts, caps at `MAX_TOOL_OUTPUT`).
- `core/echo_mcp_server.py` is a deliberately minimal test server (`ping`, `add`), already wired into `config.json`.
- **Global vs personal servers** (Settings -> Capabilities -> MCP). Global servers live in `capabilities.mcp_servers` and need `settings.orchestration.configure`. When an admin saves one, they must pick "Everyone (global)" or "Just me". Personal servers are stored in `auth.db` (`user_mcp_servers`) with secrets in the OS keychain under `u<id>:<name>:env:<KEY>`. Each runs as its own process keyed `<name>@u<id>`, and its tools are registered with an `owner` in the registry, so only that user's requests see them (resolved via `core/request_context.py`). A global name shadows a personal one. Non-admins may add only a public `https://` URL, or `npx`/`uvx` with a package listed in `capabilities.mcp_user_allowed_packages`, and cannot set loader/registry env vars (`NODE_*`, `UV_*`, `PATH`, …).

### Skills (`core/skills.py`)

Convention: `skills/<name>/SKILL.md` with `---` frontmatter carrying
`name`, `description`, `triggers` (simple `key: value` lines, not full YAML).

- **Cheap injection**: only names + one-line descriptions go into the agent system prompt (`skills_prompt_fragment()`).
- **Full body on demand**: `read_skill(name)` returns the markdown body, capped at `MAX_SKILL_BODY_CHARS` (12,000).
- The prompt fragment explicitly instructs the model to call `read_skill` **on its first step** when a skill name/trigger (e.g. `/grill-me`) appears in the request.
- Present skills: `commit-style`, `frontend-design`, `grill-me`, `web-research`.

### Plugins (`core/plugins.py`)

Convention: `plugins/<name>/plugin.py` exposing `register(api)` where `api` is a
`PluginAPI` giving:

- `api.add_tool(name, fn, description, parameters)` → registers `plugin__<name>__<tool>`.
- `api.add_system_prompt(fragment)` → appended to the agent system prompt (1,500 char cap).
- `api.on_event(hook, fn)` where hook ∈ `before_tool` | `after_tool` | `session_start` (fired via `fire_hook()`, errors isolated per-plugin).

Load failures are caught per-plugin with a traceback — one broken plugin can
never break the server. Example: `plugins/devtools/plugin.py`.

> Note: `before_tool` and `session_start` hooks are defined and dispatched but
> only `after_tool` is actually fired from the agent loop today.

### Shell (`core/shell_tools.py`)

See §10 for the full permission flow. Beyond that:

- `_sanity()` always refuses a hardcoded `FORBIDDEN_SUBSTR` list (disk format, recursive OS-dir wipe) — **even if `allow_patterns` is `["*"]`**.
- Runs with `shell=True`, `cwd=active_workspace()` (except `skills`/`npx skills` commands, which run in `BASE_DIR`), `PYTHONUNBUFFERED=1`, `CI=1`, and `stdin=DEVNULL` so CLIs can't hang on prompts.
- Auto-injects `-y` into `npx` invocations to prevent interactive hangs.
- After any command, if a `.agents/skills/` folder appeared (e.g. `npx skills add`), it **consolidates** those into the app's `skills/` dir and removes `.agents/` + `skills-lock.json`.
- `add_allow_pattern()` persists to `config.json` **and** live-updates the in-memory `APP_CONFIG`.

---

## 9. The agent loop (`POST /agent/run`)

`agent_run` (server_manager.py:853) returns a `StreamingResponse` of
`text/event-stream`. Request body (`AgentRequest`):

| Field | Default | Notes |
|---|---|---|
| `messages` | — | OpenAI chat format |
| `max_steps` | 12 | clamped to `[1, min(30, agent.max_steps)]` |
| `temperature` / `max_tokens` | 0.4 / 4096 | |
| `mode` | `"main"` | `"main"` forces the big model lane; anything else allows executor |
| `plan` | `false` | read-only plan mode (§10) |

### Pre-flight

1. If the main model isn't running and `mode == "main"` → **auto-start the profile on demand**.
2. If neither main nor executor is available → HTTP 400 ("No model loaded…").
3. Build the system prompt: `AGENT_SYSTEM_PROMPT.format(workspace=...)` + skills fragment + plugin fragments + (plan mode) `PLAN_MODE_PROMPT`. Appended to an existing system message, or inserted as `messages[0]`.
4. Extract `last_query` = the last **user** message (drives most heuristics).

### Special cases before the loop

- **Simple greetings** (`hi`, `hello`, `hey`, `help`, `test`, … or any query ≤3 chars not starting with `/`) short-circuit into a plain streaming chat with **no tools** on the best available lane. This is why "hi" feels instant.
- **Needle routing** (step 0 only, not plan mode, `mode != "main"`, no creation keywords, and no prior tool/assistant messages): the CPU router may return a single confident tool call, which is executed and streamed **without touching a GPU at all**.

### Per-step logic

```
for step in range(steps):
  lane = "main" if mode=="main" or not use_executor else "executor"
     └─ executor failure → log + fall back to main
  tools_for_lane =
       plan mode        → PLAN_MODE_TOOLS ∩ all_tools()
       executor lane    → {write_file, read_file, edit_file, list_files,
                           run_python, run_shell, read_skill, list_skills}
       main lane        → all_tools()
  stream the LLM → emit thought_delta / delta events → collect tool_calls
  if no tool_calls:  validate_and_finalize_response() → maybe rewrite → done
  else: for each tool call → sandbox check → (shell? permission modal)
        → run_tool() → fire_hook("after_tool") → append role:"tool" message
```

### Auto-escalation (executor → main)

At the end of a step, if the lane was `executor` **and** the main model is ready
**and** any of these is true, the streamed output is discarded (`delta_reset`)
and the step is immediately re-run on the main model:

- `is_degeneration_or_loop(content)` — repeated lines/sentences/phrases
- step 0 + creation intent (`make/create/write/build/code/html/…`) + refused or no tool calls
- step 0 + action intent (`run/install/test/read/grep/…`) + tutorial markdown code or refusal
- step 0 + completely empty content and no tool calls

The refusal detector looks for `"i cannot"`, `"i can't"`, `"i am unable"`,
`"as an ai"`, `"i don't have access"`.

### Response validation

`validate_and_finalize_response(last_query, content, reasoning, actions_taken)`
can **synthesize** a final answer when the model produced prose instead of doing
the work (returns `(text, was_synthesized, note)`). If it rewrites, the UI gets
`delta_reset` then the new `delta`, followed by a `validated` event.

### SSE event contract

The UI's `runAgentSSE()` consumes exactly these:

| Event | Payload | Meaning |
|---|---|---|
| `step` | `{step, total}` | new agent step began |
| `lane` | model_info | which model serves (`{lane, model, display, device, role}`) |
| `thought` | `{step, text, model}` | complete reasoning block |
| `thought_delta` | `{step, delta, model}` | streaming reasoning |
| `delta` | `{text}` | streaming answer text |
| `delta_reset` | `{}` | **discard all text streamed so far** |
| `tool_call` | `{id, name, args, model?, device?}` | tool about to run |
| `verify` | `{id, name, approved, note}` | sandbox check verdict |
| `permission_request` | `{req_id, cmd}` | shell needs user approval (UI opens modal) |
| `tool_result` | `{id, name, ok, result, model?}` | tool output |
| `validated` | `{synthesized, note}` | final answer checked/rewritten |
| `done` | `{}` / `{note, text}` | end of stream |

### Robustness machinery (`core/agent_loop.py`)

- Tool calls arriving as **text** instead of structured `tool_calls` are recovered by `_extract_text_tool_calls()` — it recognises `<tool_call>…</tool_call>`, fenced ```json blocks, and `<function name="..."><param name="...">` XML.
- `safe_parse_and_repair_args()` repairs malformed JSON arguments (trailing commas, unquoted keys, single quotes) and can infer a missing file path from the user's query.
- `validate_and_repair_tool_args()` normalizes arguments per-tool before dispatch.
- `_llm_chat_stream()` (server_manager.py:793) has a **three-tier retry**: on a 500 containing `"Failed to parse tool call arguments as JSON"` it sanitizes all historical tool-call arguments and retries; if that still fails it retries once more **with `tools` removed entirely** (plain-chat fallback) instead of erroring out.
- `sanitize_user_facing_content()` cleans text before it is stored back into history.
- `MAX_TOOL_OUTPUT` = 20,000 chars truncates every tool result fed back to the model.

---

## 10. Plan mode & the shell permission flow

### Plan mode (`req.plan = true`)

Three layers of enforcement, deliberately redundant:

1. **Prompt** — `PLAN_MODE_PROMPT` tells the model it is read-only and must produce a numbered plan.
2. **Tool schemas** — only `PLAN_MODE_TOOLS` are offered: `list_files`, `read_file`, `grep`, `search_memory`, `list_skills`, `read_skill`, `analyze_image`, `web_fetch`, `web_search`, plus `create_plan` and `get_plan` (the plan itself is plan mode's deliverable).
3. **Hard block at dispatch** — even if the model emits a mutating call anyway, the loop refuses it: `"error: plan mode is active — '<name>' is read-only-restricted."`

Plan mode also disables the Needle fast path.

### Structured plan tracking (`plan_items`)

Plan mode and Build mode now share a **persistent, DB-backed task plan**:

- **Storage** — `plan_items` table in `projects.db` (session-scoped, ordered, `status` ∈
  `pending / in_progress / done / failed`, optional note). Rows cascade-delete with their session
  (`db_delete_session` / `db_delete_project` remove them explicitly).
- **Tools** — `core/agent_tools.py`: `create_plan(items: [str])` (replaces the session's plan),
  `update_plan_item(item: 1-based, status, note?)`, `get_plan()`. They resolve the session from
  `_plan_session_id`, set per run by `set_plan_context(req.session_id)` in `routes/agent.py`.
- **Request field** — `/agent/run` accepts `session_id`; the UI sends `curSession.id`
  (agent-run.js now *awaits* `ensureSession` first so the first message has one).
- **Prompt injection** — in Build mode, an existing plan is appended to the system prompt as an
  `ACTIVE PLAN` block with per-step statuses; the agent is told to work pending/failed steps in
  order and tick them with `update_plan_item` after each finishes or fails.
- **Lane visibility** — plan tools are in `PLAN_MODE_TOOLS` and in the executor lane's core tuple,
  so both the 3B executor and the main model can create/update the plan.
- **SSE contract** — after any plan tool runs, the loop emits `event: plan` with the full item
  snapshot; the UI (`agent-run.js` → `planPanelHtml` in agent-acts.js, styled in style.css) renders
  a checklist with a progress bar in the message bubble. The snapshot lives in `meta.acts`
  (`{type:'plan', items:[...]}`, latest wins), so it is persisted with the assistant message and
  restored on session reload.
- **Max-steps wrap-up** — hitting the step cap now reports plan progress in the `done` event
  (`max steps reached — plan progress: 3/5 done, 1 failed`) instead of an empty cliff.

### Shell permission flow

The permission decision happens **in the SSE loop, not inside the tool** —
deliberate, so the modal can be shown while the loop awaits.

```
model emits run_shell {command}
   │
   ├─ _sanity() forbidden-list match?          → refused, always
   ├─ command matches an allow_pattern?        → execute, args += _pre_approved
   │     (checks config.json capabilities.shell.allow_patterns
   │      AND the active project's allow_patterns from projects.db)
   └─ else (ask_first defaults true):
        req_id = uuid4().hex[:12]
        emit SSE  permission_request {req_id, cmd}
        await _await_permission()          # 180s timeout
              ▲
              │  POST /agent/permission {req_id, decision, pattern?, project_id?}
              │      decision ∈ allow | project | always | deny
              │      "always"  → add_allow_pattern()            -> config.json
              │      "project" → db_add_project_allow_pattern() -> projects.db
              ▼
        denied  → tool error appended, loop continues
        allowed → args += _pre_approved: True → run_tool()
```

`_perm_pending` holds the outstanding prompts. The tool's own gate in
`tool_run_shell` is a fallback for **direct** (non-SSE) callers and depends on
the module-global `permission_callback`, which nothing currently assigns (§14).

---

## 11. Persistence

Two SQLite databases, both opened at import time with `check_same_thread=False`
and a module-level singleton connection. **No ORM, no migration framework** —
schema changes inspect `PRAGMA table_info` and issue `ALTER TABLE`.

### `usage.db` (one table)

`requests(id, ts, endpoint, model, prompt_tokens, completion_tokens, total_tokens, tps, duration_s, prompt_tps, stream, status)` + an index on `ts`.

Written by `db_record_request()` from the proxy for **every** request.
Read by `db_report(days, model)` → totals, `by_model[]`, `by_day[]`
(surfaced by `GET /control/report`).

### `projects.db`

```sql
projects(id, name UNIQUE, created_at, workspace_dir, allow_patterns TEXT DEFAULT '[]')
sessions(id, project_id → projects.id, title, created_at)
messages(id, session_id → sessions.id, role, content, meta TEXT, created_at)
```

- `project_id = NULL` in `sessions` is the special **"no project"** bucket (handled as `pid is None or pid == 0` throughout `db.py`).
- `projects.workspace_dir` is an optional **absolute** override; when null the workspace is `WORKSPACE_ROOT/<name>`.
- `allow_patterns` is a JSON array of fnmatch command patterns (the "Allow for this project" permission choice).
- `messages.meta` is a JSON blob of per-message UI metadata (tool calls, timing, lane).
- Deletes cascade manually (messages → sessions → project). `ON DELETE CASCADE` is declared but SQLite foreign keys are **not** enabled, so the manual deletes matter.

### Common space & documents

- `common_dir` (`COMMON_ROOT`) holds **one folder per user**: `COMMON_ROOT/user_<id>/`.
  `agent_tools.common_workspace()` resolves the caller's folder from the request
  context, so chat-generated files, uploads (`/agent/upload`, de-duped as
  `name-2.ext` instead of overwritten), `/agent/download` and `/agent/raw` never
  reach another user's files. `COMMON_ROOT/_unowned/` is served by no route.
  Migrate an old flat folder with `python scripts/migrate_common_per_user.py`
  (dry run; `--apply` to move, `--default-owner <id>` for untraceable files).
- `doc_files(id, user_id, session_id, name, kind, location, parent_id, version, sha256, source_spec, created_at)`
  (`core/doc_store.py`): version chain of generated/edited documents.
  `location` is `common` (name = file in the user's folder) or `device`
  (absolute path on the user's machine). `source_spec` is the markdown a PDF
  was rendered from; PDF edits patch it and re-render.
- `core/doc_ops/` reads and **surgically edits** .pptx/.xlsx/.docx/.csv (and
  generated .pdf via `source_spec`): `inspect` returns an outline with stable
  addresses (`s3/sh5`, `Sheet1!B7`, `p12`, `r3cAmount`, `sec2`); `edit` applies
  typed ops in memory and rejects the result unless every untargeted element
  (snapshot by identity) and every untargeted package part (C14N compare) is
  unchanged. Tools: `doc_inspect` / `doc_edit` / `doc_create`
  (`core/doc_tools.py`), in chat (common space) and agent mode (device bytes
  via companion `fs.read_b64` / `fs.write_b64`, 10 MB cap, never on server disk).
  Edits save `name-vN-<id>.ext`; the previous version is kept.

### Workspace-change tracking (in-memory only)

`core/agent_tools._ws_changes` = `{abs_path: {"before": str|None, ...}}`,
snapshotted lazily on the **first** write/edit to each file. Powers `list_diff`
and `revert`. **Not persisted** — cleared by `set_active_project()` and lost on restart.

---

## 12. HTTP endpoint reference

### Model / GPU control

| Method | Path | Purpose |
|---|---|---|
| GET | `/` | serves `ui.html` (no-store) |
| GET | `/static/{path}` | `ui.html`'s css/js (path-escape guarded) |
| GET | `/control/status` | profile, model, pid, uptime, restart_count, keepalive, ctx usage (from llama-server `/slots`), plus `main_source`/`cloud_main`/`executor_source` |
| GET | `/control/gpu` | cached adapters + per-pid compute % |
| GET | `/control/profiles` | `{models[], profiles[], cloud[]}`; flags `model_exists`, `mtp_available`, `size_gb`; cloud entries carry `value: "cloud:<provider>/<model>"` |
| GET/POST | `/control/config` | read / update launch config (clamp + persist + optional relaunch); returns a cloud-shaped payload (`{cloud:true, provider, model, ctx}`) when the target is a cloud model |
| POST | `/control/start` `/control/stop` `/control/restart` | process lifecycle |
| POST | `/control/keepalive` | `{enabled}` toggle |
| POST | `/control/switch` | `{profile\|target}` hot-swap model; `target: "cloud:<provider>/<model>"` binds the main lane instead of loading anything |
| GET | `/control/models` | per-lane status: `lanes.{main,executor,vision}` with `source`/`device`, small-model status, cloud binding counts |
| POST | `/agent/unload_small_models` | free their VRAM |

### Cloud providers / lanes

| Method | Path | Purpose |
|---|---|---|
| GET | `/control/cloud` | providers (keys **masked**), flattened cloud models, lane bindings, local lane inventory |
| GET | `/control/cloud/key?provider=` | explicit API-key reveal (👁 button) |
| POST | `/control/cloud/provider` | add/update a provider (base URL, key, models) → writes `providers.json`, applies live |
| DELETE | `/control/cloud/provider?name=` | remove a provider and unbind lanes that used it |
| POST | `/control/cloud/lanes` | bind executor/vision (`{"clear":["main"]}` unbinds); `fallback_local` |
| POST | `/control/cloud/test` | 1-token probe → `{ok, ms, sample}` or `{ok:false, error}` |

### Capabilities / shell

| Method | Path | Purpose |
|---|---|---|
| GET | `/control/capabilities` | web/skills/mcp/plugins/shell state, tool counts, MCP server status, plugin list |
| POST | `/control/capabilities` | enable/disable one capability (updates registry sources live) |
| POST | `/control/shell_settings` | shell enabled / ask_first / timeout / allow_patterns |

### Monitoring

| Method | Path | Purpose |
|---|---|---|
| GET | `/control/monitor` | in-flight + recent requests (tok/s, timings) |
| GET | `/control/report` | usage aggregation (`?days=&model=`) |

### Agent

| Method | Path | Purpose |
|---|---|---|
| POST | `/agent/run` | **SSE agent loop** |
| POST | `/agent/permission` | answer a shell permission prompt |
| GET | `/agent/workspace` | flat file list of the active workspace |
| GET | `/agent/ws/tree` | directory tree for the workspace rail |
| GET | `/agent/ws/file` | file content + diff lines (Codex-style viewer) |
| POST | `/agent/vision` | one-image describe via the vision small model |

### Projects / sessions

`GET|POST /control/projects`, `DELETE /control/projects/{pid}`,
`POST /control/projects/{pid}/activate`,
`GET|POST /control/projects/{pid}/sessions`,
`PATCH|DELETE /control/sessions/{sid}`,
`GET|POST /control/sessions/{sid}/messages`.

### Filesystem picker

`POST /control/browse_folder` (native Windows folder dialog via a PowerShell
script), `GET /control/fs/browse`, `POST /control/fs/mkdir`.

### Catch-all

`@app.api_route("/{path:path}", methods=["GET","POST"])` → `proxy()`.
This is what makes `/v1/chat/completions`, `/health`, `/slots`, `/metrics` etc.
transparently available.

---

## 13. Frontend

No framework, no bundler, no `package.json`. `ui.html` (36KB) holds the markup
and a handful of modals; `static/js/app.js` (2,675 lines) holds all logic;
`static/css/style.css` (1,439 lines) holds theming.

Notable areas in `app.js`:

| Area | Functions |
|---|---|
| Polling | `pollStatus`, `pollGpu`, `pollMonitor` (status/GPU/monitor loops) |
| Chat + agent | `send`, `runAgentSSE` (the SSE event consumer), `submitPrompt`, `warmup` |
| Rendering | `bubbleHtml`, `agentActsHtml`, `renderMonitorRecent`, `md` (mini markdown), `esc` |
| Model control | `loadSelectedModel`, `setLoadBtn`, `restoreDefault`, `fillConfigForm`, `updateGpuDependentFields` |
| Workspace rail | `wsLoadTree`, `wsShowFile`, `wsRefreshTree`, `renderWsChanges`, `updateWsRail` |
| Permissions | `showPermModal`, `answerPermission` |
| Projects/sessions | `loadProjects`, `loadSessions`, `openSession`, `ensureSession`, `activateProject` |
| Slash-command menu | `cmdMenuOpen`, `cmdMenuRender`, `cmdMenuPick`, `currentToken` |
| Attachments | `addAttachmentFile`, `buildPromptText`, `expandAtTags`, `refreshAttachUI` |
| Interactive Q&A | `renderInteractiveQuestions`, `submitGrillAnswers`, `toggleGrillOption` |
| Theming | `getSavedTheme`, `applyTheme`, `cycleTheme`, `setSettings`, `setMonitor` |

Conventions: `$()` is a `getElementById` shorthand; state is plain module-level
globals; the API is polled on intervals rather than pushed (except the agent's
SSE stream). The `@`/`/` token menus are built client-side from the same
capability data the server exposes.

Note `renderInteractiveQuestions` / `submitGrillAnswers` implement an
**interactive question card** (checkbox options parsed out of model output) —
this is what the `grill-me` skill drives.

---

## 14. Operational runbook

### Start the manager

```
python server_manager.py --profile profiles/qwen3.8-27b.json
# or:  ./scripts/start.ps1
```

Models are **not** loaded at boot by default — the profile is only *selected*.
Loading happens on `▶ Start Model`, on the first proxied request, or on the first
`/agent/run`. All launch flags come from `build_launch_command`.

### Recover full speed (14.4 t/s)

The model degrades to ~7.7 t/s after long idle **only if keepalive is off**.
Fix, in order of preference:

1. **Leave keepalive ON** — it never decays.
2. If it already decayed, the process will **not** recover. Do **CLEAR EVERYTHING** → **Start Model** (a fresh process). The degradation is per-process; a 4-minute cooldown does not help.

Do **not** trust Task Manager's GPU graphs while idle: the headless A770 shows
`dedicated = 0.00GB` because of WDDM demotion (it re-promotes within ~2s of a
request), and the 3D engine graph reads ~0% because llama.cpp uses the **compute**
engine. Use the UI's GPU panel (queries `engtype_compute`) or `GET /control/gpu`.

### Tune a model

```
python autotune.py --profile profiles/qwen3.8-27b.json        # dense: tensor-split sweep
python autotune.py --profile profiles/qwen3.8-flash-next.json # MoE: adds --n-cpu-moe sweep
```

Sweeps candidates from the profile's `autotune` block, runs real `llama-bench`
passes (512 prompt / 128 gen / 3 reps by default), prints a table, and writes the
winner into `profile["tuned"]` (`tensor_split`, `n_gpu_layers`, plus
`measured_tg_tokens_per_sec`, `measured_pp_tokens_per_sec`, `tuned_at`).
The MoE sweep walks `n_cpu_moe_candidates` high→low and stops at the first VRAM
OOM, backing off one step.

If llama.cpp renames a flag, the fix is a one-line change to the `FLAG_*`
constants at the top of `autotune.py`. If JSON parsing breaks, it prints the raw
output so you can diff against `RESULT_FIELDS`.

### Add a new model

1. Drop the `.gguf` in `MODELS_DIR` (`config.json: models_dir`, default `E:/AI/Models`).
2. It appears in the dropdown automatically — `GET /control/profiles` scans for `*.gguf`, skipping `mtp-*`, `mmproj-*`, and anything under an `orchestrator/` folder.
3. Adjust config in the UI drawer → persisted to `model_configs.json` keyed by lowercase filename.
4. Optionally tune it. Only add a `profiles/*.json` if you want it pinned/frozen (the dropdown lists those too).

### Switch models without restarting clients

```
curl -X POST http://localhost:8000/control/switch -d '{"profile":"profiles/qwen3.8-flash-next.json"}'
```

### Diagnose: generation is slow

1. `GET /control/status` → compare `measured_tg_tokens_per_sec` with the live monitor rate.
2. ~7.7 t/s on a long-lived process ⇒ WDDM demotion. Restart the process.
3. Check **Dedicated GPU memory *during* a request**, not after.
4. If only one card is hot: verify the launch line printed at startup contains `-dev Vulkan1,Vulkan2`, and that `GET /control/gpu` shows the llama-server pid on **both** A770 LUIDs.
5. Prompt-eval stable at ~35 t/s while generation decays is the signature of the demotion bug, **not** thermal throttling.

### Enable/disable capabilities

UI switches → `POST /control/capabilities` → live
`registry.set_source_enabled(source, bool)`. For MCP servers, add them under
`config.json → capabilities.mcp_servers` and restart the manager (MCP connects
once, as a background startup task).

---

## 15. Known issues & tech debt

Verified by reading the working tree at `c334c28` + uncommitted changes.
**None of these are fixed** — this is a findings list, ordered by impact.

### Bugs

1. **`submitGrillAnswers` is defined twice in `static/js/app.js`** (lines 291 and 323). The second definition silently wins; the first is dead code. Almost certainly a duplication accident during the grill-me work — the two bodies should be diffed and merged.

2. **`permission_callback` is imported in `server_manager.py:107` but never assigned.** `core/shell_tools.py`'s docstring claims "the agent SSE loop installs `permission_callback` before running tasks" — it does **not**. The real flow is inline in the SSE loop (which is why the UI works), but any *direct* call to `run_shell` outside the SSE loop falls through to `(False, "no permission channel available")`. Either install the callback or delete the dead import and the fallback branch.

### Portability (repo is bound to one machine)

3. `core/small_model.py:127` hardcodes `bin_dir = "E:\\AI\\llama-vulkan"` — small models **ignore** the profile's `llama_bin_dir`, so they break if the build moves.
4. `core/small_model.py:28` hardcodes the `E:\AI\workspace` fallback; `core/config.py` hardcodes `CONFIG_DEFAULTS["llama_bin_dir"]` and `gpu_devices: [1, 2]`.
5. `core/config.py:24` — `IGNORED_IGPU_LUIDS = {"0x000165f7", "0x0001665a"}` are *this box's* LUIDs, so the iGPU filter is wrong on any other machine.
6. `model_configs.json` and `profiles/*.json` all carry `gpu_devices: [1, 2]` (Vulkan indices) and absolute `model_path` / `llama_bin_dir`.

### Rough edges

7. `plugins.py` defines `before_tool` and `session_start` hooks and dispatches them via `fire_hook()`, but the agent loop only ever fires `after_tool`.
8. `web_tools._ddg_search_sync` has a `if api_key: pass` placeholder — the Brave/Tavily swap-in is unimplemented, and the DDG HTML scrape will break if DDG changes its markup.
9. `README.md` is stale twice over: it documents only `autotune.py` + `server_manager.py` (nothing about the agent platform), and its "Honesty check" claims the code has never run on real A770s — `TESTING_LOG.md` shows extensive verified runs since.
10. `core/agent_tools.AGENT_CORE_TOOLS` is now unused by the agent loop (the executor lane filters the registry instead). Kept only as a fallback path.
11. `proxy()` evaluates `r.json()` twice in the final `JSONResponse` expression. Harmless, slightly wasteful on non-JSON responses.
12. `requirements.txt` omits `needle` and `laya` (optional CPU routers) — intentional, but it means `router_available()` returns `False` silently on a clean install unless installed via `pip install needle` or `pip install laya`.
13. `.claude/skills/frontend-design/` is an empty directory that triggers git warnings (`could not open directory ... No such file or directory`); the real skill is `skills/frontend-design/`.
14. `config.json` sets `agent.idle_unload_s: 0`, which **disables** the small-model idle reaper (`unload_if_idle` returns early when `<= 0`). Intentional — keeps small models resident so they don't pay demotion costs — but easy to misread, since the code default is `120`.

### Uncommitted state

HEAD `c334c28` sits on a substantial uncommitted working tree: modified
`config.json`, `core/{agent_loop,db,monitor,skills,small_model}.py`,
`model_configs.json`, `server_manager.py`, `static/css/style.css`,
`static/js/app.js`, `ui.html`; untracked `core/shell_tools.py`,
`skills/frontend-design/`, `skills/grill-me/`. The whole shell capability, the
skills-consolidation logic, and the grill-me skill are part of that unshipped work.

---

## 16. Conventions & extension recipes

### Code conventions observed

- **Everything reusable goes in `core/`; `server_manager.py` only wires routes.** Each core module is imported once at the top of `server_manager.py` — keep that import block tidy, it doubles as the dependency map.
- **Module-level singletons**, not classes with instances: `state`, `small_models`, `registry`, `_monitor_state`, `_projects_db`, `_usage_db`, `_gpu_cache`, `_perm_pending`, `_active_project`, `_ws_changes`.
- **Errors are returned as strings, not raised**, at the tool boundary — `"error: {type}: {msg}"`, `"error: file not found: ..."`. The loop detects failure with `result.startswith("error:")`. Any tool you add should follow this.
- Static-feature gating: every capability's `register_*()` starts with a `APP_CONFIG["capabilities"][...]` check and returns early if disabled.
- Config keys are always validated through the `CONFIG_INT_FIELDS` clamp or `CONFIG_CHOICE_FIELDS` whitelist, never trusted raw.
- Logging is plain `print()` prefixed with `[server_manager]`, `[plugins]`, `[skills]`, or the role name; real errors go to `sys.stderr`.
- Deliberate "cheap first" bias: greetings short-circuit, Needle routes without a GPU, skills inject only names until asked, tool outputs truncate at 20k.

### Recipe: add a builtin tool

1. Implement `tool_foo(args: dict) -> str` in `core/agent_tools.py` (return an error string rather than raising; resolve paths via `_ws_resolve`).
2. Add a schema dict to `AGENT_TOOLS`.
3. Add it to `TOOL_IMPLS`.
Done — `bootstrap_builtin_tools()` mirrors it into the registry at startup.
If the tool must be usable in plan mode, add its name to `PLAN_MODE_TOOLS` in `server_manager.py`.
If it must be visible to the executor lane, add its name to the executor `tools_for_lane` tuple.

### Recipe: add a skill (no code)

```
skills/<name>/SKILL.md
---
name: my-skill
description: One line shown in the system prompt.
triggers: /my-skill, my-skill
---
<body the model reads when it calls read_skill>
```
No restart needed — `load_skills()` re-scans on every call.

### Recipe: add a plugin

`plugins/<name>/plugin.py`:
```python
def register(api):
    api.add_system_prompt("...")
    api.add_tool("my_tool", _impl, "description",
                 {"type": "object", "properties": {...}, "required": [...]})
    api.on_event("after_tool", lambda name, args, result: None)
```
Requires `capabilities.plugins: true` and a manager restart.

### Recipe: add an MCP server

```json
"mcp_servers": {
  "myserver": {"transport": "stdio", "command": "python", "args": ["path/to/server.py"]},
  "remote":   {"transport": "http",  "url": "http://127.0.0.1:9000/mcp"}
}
```
under `capabilities` in `config.json`, then restart. Tools appear as
`mcp__myserver__<tool>`. Use `core/echo_mcp_server.py` as a reference implementation.

### Recipe: add a config field

1. `core/config.py`: add to `CONFIG_DEFAULTS` + `MODEL_CONFIG_KEYS`.
2. Add to `CONFIG_INT_FIELDS` (clamp) or `CONFIG_CHOICE_FIELDS` (enum).
3. Add to `CONFIG_TARGETS` — `(None, "field")` for top level, `("tuned", "field")` for the tuned block.
4. If it needs custom parsing (like `tensor_split` or `mtp_enabled`), add a branch in `_apply_config_update()`.
5. Expose it in `_config_for_profile()` and in the UI drawer (`fillConfigForm` in `app.js`).
6. If it becomes a launch flag, emit it from `build_launch_command()` in `core/process.py`.

### Testing

`requirements.txt` has no test framework — validation to date has been manual
plus real-hardware sessions logged in `TESTING_LOG.md`. `scratch/` holds ad-hoc
scripts (`test_agent_e2e.py`, `test_orchestrator.py`) and is gitignored.

Quick sanity checks that work without a GPU:
```
python -c "import ast; ast.parse(open('server_manager.py',encoding='utf-8').read()); print('OK')"
python -c "import core.registry, core.config, core.profiles; print('imports OK')"
```
Importing `server_manager` itself boots nothing, but it does open both SQLite
databases and read `config.json` as import-time side effects.

---

## 17. Quick reference — "where do I change X?"

| I want to change… | Go to |
|---|---|
| llama-server CLI flags | `core/process.py::build_launch_command` |
| a flag *name* in autotune | `autotune.py` `FLAG_*` constants |
| config key validation / ranges | `core/config.py` |
| where a config key is stored in the profile | `core/config.py::CONFIG_TARGETS` |
| the agent system prompt / rules | `core/agent_loop.py::AGENT_SYSTEM_PROMPT` |
| plan-mode rules | `server_manager.py::PLAN_MODE_PROMPT` + `PLAN_MODE_TOOLS` |
| which tools a lane sees | `server_manager.py` ~line 1013 (`tools_for_lane`) |
| escalation heuristics | `server_manager.py` ~line 1052 (`should_escalate`) |
| tool result size limit | `core/agent_tools.py::MAX_TOOL_OUTPUT` |
| keepalive interval | `core/config.py::KEEPALIVE_INTERVAL_S` (runtime override: `POST /control/config {"keepalive_interval_s": N}`) |
| GPU polling TTL / iGPU filter | `core/config.py::GPU_QUERY_INTERVAL_S`, `IGNORED_IGPU_LUIDS` |
| workspace sandbox rules | `core/agent_tools.py::_ws_resolve` |
| shell command policy | `config.json → capabilities.shell` + `core/shell_tools.py` |
| watchdog restart backoff | `core/config.py` `WATCHDOG_INTERVAL_S`, `MAX_RESTART_BACKOFF_S`, `HEALTH_TIMEOUT_S` |
| ports | `core/config.py` (`PROXY_PORT` 8000, `LLAMA_SERVER_PORT` 8090, small models 8091-8093) |
| what skills/plugins/Capabilities see in the UI | `static/js/app.js::loadCapabilities` |
| cloud provider registry / lane routing | `core/cloud.py` (`cloud_lane`, `CloudClient`, `save_provider`, `set_lanes`) |
| cloud REST surface | `routes/cloud.py` |
| cloud optgroups in the model dropdown | `static/js/config.js::loadProfiles` |
| Cloud Models settings card | `static/js/cloud.js::loadCloudCard` |
| agent lane selection per mode | `routes/agent.py` (`agent_run` ← "lane resolution") |
| the 4 engine modes (`#agent-engine`) | `ui.html` + `routes/agent.py` (`mode` normalization) |
| ☁️ status pill / empty-state text | `static/js/gpu-status.js` (`setPill`, `mainLaneReady`) |

---

*Generated from a full source read of the working tree (HEAD `c334c28`). Line
numbers refer to that revision — verify before relying on them after further edits.*


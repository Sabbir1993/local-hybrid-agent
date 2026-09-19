# A770-Dual Runtime

A custom orchestration layer for running local LLMs at maximum throughput on
**2x Intel Arc A770 (16GB each, one on PCIe 3.0)**, built on top of
`llama.cpp`'s Vulkan backend.

## Why this exists, and what it actually is

Writing a from-scratch inference engine (custom GPU kernels, scheduler,
KV-cache manager) that beats `llama.cpp` on Arc hardware is not realistic —
that's years of work by Intel's own oneDNN/oneMKL teams plus the llama.cpp
community, and even they have open, unresolved performance bugs on this
hardware (see below). So this project does the part that's actually worth
custom-building: **hardware-aware orchestration** around llama.cpp's proven
Vulkan kernels —

- auto-tuning (`autotune.py`) that empirically sweeps GPU-split ratios and
  MoE-offload settings on *your* two cards instead of guessing,
- a process manager (`server_manager.py`) that launches `llama-server` with
  the tuned config, health-checks it, restarts it on crash, and lets you
  hot-swap between model profiles without touching the command line,
- profiles pre-filled with the right defaults for a dense model
  (Qwen3.8-27B) and an MoE model (Qwen3.8-Flash-Next), since they need
  different tuning strategies.

## Two decisions this is built on, and why

**Vulkan backend, not SYCL.** llama.cpp's SYCL backend has an open,
unresolved bug (ggml-org/llama.cpp#19918) where MoE text-generation
throughput on Arc A770 drops ~85% versus Vulkan on the same hardware (10 t/s
vs 68 t/s in the reported case). Vulkan is the safer default on Arc today.
If Intel ships a SYCL fix later, re-run `autotune.py` — the scripts don't
hardcode the backend choice into anything but the binary you point them at.

**`--split-mode layer` (pipeline split), not `row`.** With `layer` (the
default), each GPU owns a contiguous range of transformer layers and only a
small hidden-state vector crosses PCIe once per layer boundary — so your
PCIe 3.0 card is much less of a bottleneck than it sounds. `row` (tensor
parallelism) needs a cross-GPU all-reduce every layer and would be badly
hurt by the slower link; it's also flagged as poor-performing/deprecated
upstream. **The one place PCIe speed still matters a lot: MoE CPU-offload**
(`--n-cpu-moe`, used for Qwen3.8-Flash-Next). Offloaded expert weights get
streamed from system RAM to whichever GPU needs them, every token — so for
that model, the PCIe 3.0 card doing a disproportionate share of MoE layers
will show up as a real slowdown. `autotune.py` sweeps this empirically
rather than assuming a ratio, and the README below tells you how to check
for it.

**Known upstream bug to watch for:** `--n-cpu-moe` has a reported issue
(ggml-org/llama.cpp#15136) where it can load-balance unevenly across
multiple GPUs, filling one card's VRAM before touching the second — reported
on CUDA, unconfirmed on Vulkan. Watch GPU memory (Task Manager → Performance
→ GPU 0 / GPU 1, or `intel_gpu_top` if you have it) the first time you run a
tuned MoE config live. If one card is starved while the other overflows,
the escape hatch is `--override-tensor` for manual per-tensor placement
(not automated here — ask if you hit this and want it scripted).

## What this actually is today

What started as a CLI autotune/proxy pair has grown into a small local
control-center app: a FastAPI backend (`server_manager.py` + `routes/`)
serving a single-page web UI (`ui.html` + `static/js/`) with:

- **Chat mode** — direct conversation with the loaded model.
- **Agent mode** (Plan / Build) — an autonomous coding loop with file
  read/write/edit, shell (permission-gated), web search/fetch, skills, and
  MCP tool access against a project workspace.
- **Three lanes**: `main` (your big local model, e.g. Qwen3.8-27B),
  `executor` (a small fast local model for routine tool calls), and
  `vision`/`embedder` helpers — each can be served locally via
  `llama-server` or bound to a cloud OpenAI-compatible provider, in any mix
  (`all-local`, `main-local-rest-cloud`, `main-cloud-rest-local`,
  `all-cloud`), with automatic local fallback if a cloud lane fails.
- Projects/sessions persisted in SQLite (`projects.db`, `usage.db`,
  `memory.db`), a live request monitor, and a token-usage report.

## Setup (Windows)

1. **Update Arc drivers** to the latest from Intel (Arc Control or the
   driver package directly).
2. **Download a Vulkan-backend Windows release** of llama.cpp from
   https://github.com/ggml-org/llama.cpp/releases — grab the asset named
   `llama-<build>-bin-win-vulkan-x64.zip` (NOT the `-sycl-` or `-cuda-`
   ones). Unzip it somewhere, e.g. `C:\llama-vulkan\`.
3. **Verify both GPUs are visible:**
   ```
   C:\llama-vulkan\llama-bench.exe --list-devices
   ```
   You should see two Vulkan devices. Note their indices (0 and 1) — if
   your PCIe 3.0 card doesn't show up as device 0, that's fine, just note
   which index is which (cross-check against Task Manager's GPU tab, or
   `vulkaninfo` if installed).
4. **Install Python 3.10+** and the app's dependencies:
   ```
   pip install -r requirements.txt
   ```
5. **Configure `config/app.json`** (copy/edit the checked-in one):
   - `models_dir` — folder containing your GGUFs.
   - `workspace_dir` / `common_dir` — where Agent mode reads/writes project
     files.
   - `small_models.executor` / `.vision` / `.embedder` — model paths
     (relative to `models_dir`), ports, GPU index, and context size for the
     small local helper models. Leave a model's `"model"` field `null` to
     disable that lane.
   - `agent`, `router`, `roles`, `capabilities` (web search, skills, MCP
     servers, plugins, shell allow-patterns) — tune as needed; sensible
     defaults are already checked in.
6. **Configure `profiles/*.json`** for your main model(s): set
   `llama_bin_dir` to your unzipped llama.cpp folder and `model_path` to
   your downloaded GGUF (get GGUFs from Hugging Face — search
   `Qwen3.8-27B GGUF` / `Qwen3.8-Flash-Next GGUF`; Unsloth's quants are a
   solid default).
7. **(Optional) Cloud providers** — for hybrid/cloud lanes, add provider
   credentials and per-model `ctx` values either through the Settings panel
   in the UI, or directly in `config/providers.json` (git-ignored, since it
   holds API keys — see `[PLACEHOLDER]` note below). Never commit real API
   keys; use `[PLACEHOLDER]` in anything you share or paste elsewhere.

## Usage

**1. Auto-tune each local model once** (and again if you change
quant/context):

```
python autotune.py --profile profiles/qwen3.8-27b.json
python autotune.py --profile profiles/qwen3.8-flash-next.json
```

This runs a grid of short `llama-bench` passes (tensor-split ratios for the
dense model; `--n-cpu-moe` steps for the MoE model), prints a results table,
and writes the best-found flags back into `tuned` in the profile JSON. Each
sweep takes a few minutes — it's real benchmarking, not a guess.

**2. Start the app:**

```
python server_manager.py --profile profiles/qwen3.8-27b.json --port 8000
```

`--profile` is optional (you can load a model from the UI instead), and
`--port` defaults to 8000. This launches the FastAPI app, which:
health-checks/restarts the underlying `llama-server` process, hot-swaps
profiles without restarting your client, and serves both the web UI and an
OpenAI-compatible proxy.

**3. Open the control center** at `http://localhost:8000/` — pick a model
from the header dropdown, click **▶ Load**, and start chatting or switch to
**🤖 Agent Task** mode for autonomous file edits.

**4. Or drive it as a plain API** — `http://localhost:8000/v1/chat/completions`
is OpenAI-compatible (works with the OpenAI Python SDK, LM Studio-style
clients, etc.), and `/agent/run` streams the autonomous coding loop over SSE
for programmatic use. Switch the loaded profile without a UI:
```
curl -X POST http://localhost:8000/control/switch -d '{"profile":"profiles/qwen3.8-flash-next.json"}'
```

## Files

- `autotune.py` — sweeps GPU-split / MoE-offload settings via `llama-bench`,
  writes tuned config into the profile.
- `server_manager.py` — FastAPI bootstrap: mounts the UI, wires up
  `routes/*`, and manages process lifespan (llama-server, MCP, plugins,
  keepalive, background memory tasks).
- `routes/` — HTTP surface: `control` (lifecycle/profiles/GPU/config),
  `agent` (autonomous coding loop, permissions, vision), `chat` (direct
  completions), `projects` (sessions/workspace browsing), `capabilities`
  (skills/MCP/plugins/shell settings), `cloud` (provider/lane bindings),
  `proxy` (OpenAI-compatible passthrough to `llama-server`).
- `core/` — orchestration internals: `config.py` (paths/defaults),
  `small_model.py` (small-model lane manager + `APP_CONFIG`), `cloud.py`
  (cloud provider/lane resolution), `agent_loop.py`/`agent_tools.py` (tool
  execution, sandboxing, plan tracking), `vram.py` (preflight VRAM checks),
  `roles.py` (named sub-agent presets).
- `config/app.json` — main app config (models dir, small-model lanes,
  agent/router settings, roles, capabilities). `config/providers.json` —
  UI-managed cloud provider/lane bindings (git-ignored; holds API keys, so
  never commit real values there or in `config/model_configs.json`).
- `profiles/qwen3.8-27b.json` — dense-model profile (tensor-split sweep only,
  no MoE offload — it's not an MoE model).
- `profiles/qwen3.8-flash-next.json` — MoE-model profile (tensor-split +
  `--n-cpu-moe` sweep, larger context default given its 262K native window).
- `ui.html` + `static/js/*` — the single-page web control center (chat,
  agent mode, settings, monitor, usage report).
- `scripts/list_devices.ps1` — quick Vulkan device check.
- `scripts/start.ps1` — convenience wrapper around `server_manager.py`.

## Security note (payment-adjacent environments)

If you deploy this alongside anything that touches cardholder or payment
data, keep it network-isolated from that scope: this app runs a local shell
tool (permission-gated but still powerful) and stores API keys in
`config/providers.json` in plaintext on disk. Don't point `workspace_dir` /
`common_dir` at directories containing real payment credentials, PANs, or
tokens, and treat `config/providers.json` and `usage.db` as sensitive —
they aren't encrypted at rest.

## Honesty check

I don't have Arc hardware in the environment this was written in, so none
of the autotune/runtime flag behavior has been run against real A770s —
it's built strictly from current llama.cpp flag documentation and the
specific bug reports linked above, not guessed. Treat the first
`autotune.py` run as the real verification step: if a flag has changed name
in a newer llama.cpp release, `llama-bench --help` will tell you
immediately, and the fix is a one-line change in `autotune.py`'s `FLAG_*`
constants at the top of the file.

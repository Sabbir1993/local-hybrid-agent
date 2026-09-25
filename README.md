# Local Agent

A local LLM orchestration layer built on top of `llama.cpp`, originally tuned
for **2x Intel Arc A770 (16GB each, one on PCIe 3.0)** via the Vulkan
backend. GPU count and tensor-split are now dynamic (1..N GPUs, any vendor
llama.cpp has a backend for — Vulkan or CUDA) — see
[Portability: any GPU count, any machine](#portability-any-gpu-count-any-machine)
below for what "dynamic" does and doesn't cover yet.

## Why this exists, and what it actually is

Writing a from-scratch inference engine (custom GPU kernels, scheduler,
KV-cache manager) that beats `llama.cpp` is not realistic — that's years of
work by hardware vendors' own kernel teams plus the llama.cpp community. So
this project does the part that's actually worth custom-building:
**hardware-aware orchestration** around llama.cpp's proven kernels —

- auto-tuning (`autotune.py`) that empirically sweeps GPU-split ratios and
  MoE-offload settings on *your* cards instead of guessing,
- a process manager (`server_manager.py`) that launches `llama-server` with
  the tuned config, health-checks it, restarts it on crash, and lets you
  hot-swap between models without touching the command line,
- a VRAM preflight check (`core/vram.py`) that estimates whether a model +
  context will actually fit before launching, and suggests a safe `-ngl` /
  `tensor_split` if it won't,
- per-model launch overrides in `config/model_configs.json`, keyed by
  filename, so different models can have different tuned settings without
  hand-maintained profile files.

## Two decisions this is built on, and why

**Vulkan backend by default, not SYCL, on Intel Arc.** llama.cpp's SYCL
backend has an open, unresolved bug (ggml-org/llama.cpp#19918) where MoE
text-generation throughput on Arc A770 drops ~85% versus Vulkan on the same
hardware (10 t/s vs 68 t/s in the reported case). Vulkan is the safer default
on Arc today. If Intel ships a SYCL fix later, re-run `autotune.py`.

**`--split-mode layer` (pipeline split), not `row`.** With `layer` (the
default), each GPU owns a contiguous range of transformer layers and only a
small hidden-state vector crosses PCIe once per layer boundary — so a PCIe
3.0 card is much less of a bottleneck than it sounds. `row` (tensor
parallelism) needs a cross-GPU all-reduce every layer and would be badly hurt
by a slower link; it's also flagged as poor-performing/deprecated upstream.
**The one place PCIe speed still matters a lot: MoE CPU-offload**
(`--n-cpu-moe`). Offloaded expert weights get streamed from system RAM to
whichever GPU needs them, every token — so for MoE models, a slower card
doing a disproportionate share of MoE layers will show up as a real
slowdown. `autotune.py` sweeps this empirically rather than assuming a ratio.

**Known upstream bug to watch for:** `--n-cpu-moe` has a reported issue
(ggml-org/llama.cpp#15136) where it can load-balance unevenly across
multiple GPUs, filling one card's VRAM before touching the others —
reported on CUDA, unconfirmed on Vulkan. Watch GPU memory (Task Manager →
Performance, or the app's own GPU panel) the first time you run a tuned MoE
config live. If one card is starved while another overflows, the escape
hatch is `--override-tensor` for manual per-tensor placement (not automated
here).

## What this actually is today

What started as a CLI autotune/proxy pair has grown into a small local
control-center app: a FastAPI backend (`server_manager.py` + `routes/`)
serving a single-page web UI (`ui.html` + `static/js/`) with:

- **Chat mode** — direct conversation with the loaded model.
- **Agent mode** (Plan / Build) — an autonomous coding loop with file
  read/write/edit, shell (permission-gated), web search/fetch, skills, and
  MCP tool access against a project workspace.
- **Models & jobs** (Settings → 🧭 Models & Jobs): built-in lanes `main`
  (your big local model), `executor` (a small fast local model for routine
  tool calls), `vision` and `embedder`, plus **any number of extra models** —
  admins add local ones (a `.gguf` on a chosen GPU/port), each user adds
  their own cloud ones. Every kind of work is a fixed *job* (thinking &
  planning, routine tool calls, summarizing, commit messages, sub-agents,
  checking answers, reading images, search memory…) that you map to a model;
  unmapped jobs keep today's defaults. Each model has an "if it fails, use…"
  backup, so a failed cloud call falls back automatically. Agent presets:
  Private (all on this PC), Balanced, Best quality, Main model only
  (`core/lanes.py`).
- **Answer check** (🛡 next to Send): a second model double-checks answers —
  *check after* (badge) or *check before showing* (the fixed answer is
  shown); a broken checker never blocks the answer (`core/verifier.py`).
- **Images, videos and speech** (Settings → Models & Jobs → *Create & listen*;
  `core/media.py`, `routes/media.py`). Each is off until a model is added:
  - `/image <description>` and `/video <description>` in chat, the composer's
    **🖼 Image** select (Send makes an image), and the agent tools
    `generate_image` / `generate_video`. Plain chat can also draw when asked
    ("draw me …") if the image model is local. Served by
    **stable-diffusion.cpp on this PC** (`sd-server.exe`, Vulkan; images for
    now, video next): put it in `runtimes.<name>.sd_bin_dir` or next to the
    llama folder as `sd-vulkan/`, and the model files (e.g. Z-Image Turbo or
    Qwen Image 2.1: diffusion `.gguf`, text-encoder `.gguf`, VAE
    `.safetensors`) in `Models/orchestrator/image-models/`
    (`video-models/` for video). It is **loaded by hand** (▶ Load in Settings
    or in the chat card) and stays loaded until ■ Unload — a request never
    starts it and the idle timer never stops it. Or by a cloud model
    (OpenAI-style `images/generations` / `videos`, Google Imagen / Veo). Cloud videos ask
    before they're made (they cost money); admins set daily cloud limits.
  - **Working from pictures** (sd.cpp on this PC only — pictures are never
    sent to a cloud model): attach pictures in 🖼 mode (or press ✏️ Edit this
    picture on a result) and either **change one picture** (`init_image`, a
    "how much to change" slider) or **edit / combine** up to 10 pictures
    referred to as `<image1>`, `<image2>` (`ref_images`). Combining needs an
    edit-capable model: for Qwen Image 2.1 add its vision weights
    (`mmproj-Qwen3VL-8B-Instruct-F16.gguf`, passed as `--llm_vision`).
    Pictures are checked (PNG/JPEG/WEBP, 10 MB each, 40 MB total), scaled to
    ≤ 1536 px and re-encoded, which drops EXIF/GPS; they aren't stored, and the
    audit log keeps only their count and size (`core/media_images.py`).
  - 🎤 dictation and attached audio files → text with **whisper.cpp on this
    PC** (`whisper-server.exe`, Vulkan or CPU build, e.g.
    `ggml-large-v3-turbo-q5_0.bin` in `Models/orchestrator/`, for Bangla +
    English). The browser converts
    recordings to 16 kHz WAV, so no ffmpeg is needed. Put `whisper-server.exe`
    in `runtimes.<name>.whisper_bin_dir`, or next to the llama folder as
    `whisper-vulkan/` or `whisper/`. Cloud speech-to-text stays off until an
    admin allows it — audio can't be scanned for card numbers before it leaves.
  - Results are saved in your private `common/user_<id>/generated/` folder.
- **Helper model files** (every model except the main one) are picked only
  from `Models/orchestrator/` (image/video models from its `image-models/` /
  `video-models/` subfolders); the main-model list never shows that folder.
- Projects/sessions persisted in SQLite (`projects.db`, `usage.db`,
  `memory.db`), a live request monitor, and a token-usage report.
- **Fast CPU Routing Layer (0 MB VRAM)**: Optional sub-step router on CPU for zero-VRAM tool dispatch. Supports both **Cactus Needle-2** and **Convai Laya**, switchable via `config/app.json` (`"engine": "cactus_needle"` or `"engine": "laya"`).

### Fast CPU Routing Layer: Switching Needle-2 and Laya

In `config/app.json`:
```json
"router": {
  "enabled": true,
  "confidence_threshold": 0.75,
  "engine": "cactus_needle",
  "executor_grammar": true,
  "laya": {
    "checkpoint": "convaiinnovations/laya",
    "subfolder": null,
    "device": "cpu",
    "preload": false
  }
}
```
- **Needle-2 (`"engine": "cactus_needle"`)**: 45M Simple Attention Network (~14MB binary, ~28MB RAM). Autoregressively fills tool arguments via grammar. Installed via `pip install needle`.
- **Laya (`"engine": "laya"`)**: 421M ModernBERT non-autoregressive decision model running strictly on CPU (`device="cpu"`). High-accuracy semantic routing with calibrated probabilities. Installed via `pip install laya`.

Both engines execute strictly on CPU with **0 MB GPU VRAM usage**, preserving all VRAM for your primary models.

## Portability: any GPU count, any machine

The GPU-facing logic is dynamic, not hardcoded to "2x Arc A770":

- **GPU count**: 1 GPU → no `--tensor-split`/`-dev` list is emitted at all.
  2+ GPUs → `core/vram.py: compute_tensor_split()` computes a split
  proportional to each card's free VRAM (this is what reproduces today's
  hand-tuned `9,11`-style ratio for two similar-sized A770s, and generalizes
  to N cards of any size).
- **Backend (vendor)**: `core/backend.py` maps a single `"backend"` setting
  (`"vulkan"` or `"cuda"`) to the right `-dev` prefix (`Vulkan0,Vulkan1...`
  vs `CUDA0,CUDA1...`) and the right visible-devices env var
  (`GGML_VK_VISIBLE_DEVICES` vs `CUDA_VISIBLE_DEVICES`). Everything else —
  `--tensor-split`, `--split-mode`, `-ngl`, `--n-cpu-moe`, etc. — is
  llama.cpp's own flag surface and doesn't change between backends.

**What that means for moving to a different machine:** in most cases you do
**not** need to touch Python code — clone the repo, install the matching
llama.cpp build for your GPU vendor, and point the config files below at
your paths. See [Setup on a new machine](#setup-on-a-new-machine).

**What's still machine-specific and needs a one-time edit** (there's no UI
for these yet — they live in source/config, not a settings panel):

| What | Where | Why it's not auto |
|---|---|---|
| `llama_bin_dir`, `backend`, `gpu_devices` | `config/app.json` → `runtimes` + `runtime` (or env `LLAMA_RUNTIME`) | Install-level settings, not per-model; the active runtime overrides per-model device lists. No UI field writes these yet. |
| GPU display names (e.g. "A770 #1 · display") | `static/js/theme.js` → `LUID_NAMES` | Cosmetic only — labels a Windows adapter LUID with a human name in the GPU panel. Safe to leave as generic `GPU #N` on a new machine. |
| Integrated-GPU LUIDs to hide from the GPU panel | `core/config.py` → `IGNORED_IGPU_LUIDS` | Windows-perfcounter LUIDs are per-machine; an iGPU you want hidden on one box may not exist on another. |
| `models_dir`, `workspace_dir`, `common_dir`, small-model lane paths | `config/app.json` | Per-install file paths, meant to be edited (see setup below). |
| Per-model launch tuning (`tensor_split`, `n_gpu_layers`, `context_size`, ...) | `config/model_configs.json` | Keyed by GGUF filename; carries over only if you copy the same models + this file. Safe to delete and let autotune/preflight regenerate. |

None of these require a code change — they're all values in `core/config.py`
or `config/*.json` — but they are hand-edited, not auto-detected, today.

## Setup on a new machine

This is the actual "clone the repo and go" path.

1. **Get the right llama.cpp binary for your GPU.**
   - Intel Arc / any Vulkan-capable GPU: download a **Vulkan** Windows
     release from https://github.com/ggml-org/llama.cpp/releases — the
     asset named `llama-<build>-bin-win-vulkan-x64.zip` (not `-sycl-` or
     `-cuda-`).
   - NVIDIA: download a **CUDA** release instead — the asset named
     `llama-<build>-bin-win-cuda-x64.zip` (matching your installed CUDA
     toolkit version). Requires NVIDIA drivers + CUDA toolkit already
     installed.
   - Unzip it somewhere, e.g. `C:\llama-vulkan\` or `C:\llama-cuda\`.
2. **Verify your GPU(s) are visible:**
   ```
   C:\llama-vulkan\llama-bench.exe --list-devices
   ```
   (or `.\scripts\list_devices.ps1 -BinDir "C:\llama-vulkan"`). Confirm you
   see one device per physical GPU, and note their indices.
3. **Clone this repo** and install Python 3.10+ dependencies:
   ```
   git clone <this-repo-url>
   cd a770-dual-runtime
   pip install -r requirements.txt
   ```
4. **Tell it which backend and binary to use**: add one preset per llama.cpp
   build under `runtimes` in [`config/app.json`](config/app.json), then pick
   one with `runtime`:
   ```json
   "runtime": "vulkan",
   "runtimes": {
     "vulkan": { "llama_bin_dir": "C:/llama-vulkan", "backend": "vulkan", "gpu_devices": [0, 1], "small_model_gpu": 0 },
     "cuda":   { "llama_bin_dir": "C:/llama-cuda",   "backend": "cuda",   "gpu_devices": [0],    "small_model_gpu": 0 }
   }
   ```
   `gpu_devices` are that build's `llama-bench --list-devices` indices.
   `small_model_gpu` is used when a small model's `gpu` isn't in that list.
   To switch builds without editing the file, set `LLAMA_RUNTIME=cuda`
   before starting `server_manager.py`. The startup log prints the active
   runtime. After switching GPUs, re-run autotune (`tuned` splits are hardware-specific).
   Single-GPU machine: use `"gpu_devices": [0]` — the app automatically
   omits `--tensor-split` and multi-device `-dev` wiring when there's only
   one device.
5. **Configure [`config/app.json`](config/app.json)** (copy/edit the checked-in one):
   - `models_dir` — folder containing your GGUFs.
   - `media_dirs` — optional folders for `image_models`, `video_models` and
     `voice_models` (whisper `.bin` model files; the `whisper-server.exe`
     program itself goes in `E:\AI\vulkan-arc\whisper-vulkan`). Absolute, or relative to `models_dir`; default
     `Models/orchestrator/<image-models|video-models|voice-models>`. Read at
     start: restart the server after changing them.
   - `workspace_dir` / `common_dir` — where Agent mode reads/writes project
     files. `common_dir` gets one private sub-folder per user
     (`user_<id>/`) for chat-generated files and uploads; to move an older
     flat folder, run `python scripts/migrate_common_per_user.py` (dry run
     first, then `--apply`).
   - `small_models.executor` / `.vision` / `.embedder` — model paths
     (relative to `models_dir`), ports, GPU index, and context size for the
     small local helper models. Leave a model's `"model"` field `null` to
     disable that lane.
   - `agent`, `router`, `roles`, `capabilities` (web search, skills, MCP
     servers, plugins, shell allow-patterns) — tune as needed; sensible
     defaults are already checked in.
6. **Drop your GGUFs into `models_dir`.** No profile files are required —
   picking a `.gguf` from the UI dropdown (or `--profile <path-to-gguf>` on
   the CLI) builds a launch config on the fly from `CONFIG_DEFAULTS` plus
   any saved overrides for that filename in `config/model_configs.json`
   (created automatically the first time you tune settings for a model
   through the UI's config drawer — you don't need to hand-write it).
7. **(Optional) Cloud providers** — for hybrid/cloud lanes, add provider
   credentials and per-model `ctx` values either through the Settings panel
   in the UI. API keys go to the OS keychain (Windows Credential Manager);
   the per-user `config/providers/user_<id>.json` only keeps
   `"apiKeyRef": "keyring"`. A legacy `config/providers.json` (or
   `providers.json.migrated`) is imported into the keychain on start and
   then deleted. Never commit real API
   keys; use `[PLACEHOLDER]` in anything you share or paste elsewhere.

## Usage

**1. Start the app:**

```
python server_manager.py --port 8000
```

`--profile` is optional — you can pass a path to a `.gguf` or a saved JSON
profile to auto-load on startup, or just load a model from the UI once it's
up. `--port` defaults to 8000. This launches the FastAPI app, which
health-checks/restarts the underlying `llama-server` process, hot-swaps
models without restarting your client, and serves both the web UI and an
OpenAI-compatible proxy.

**2. Open the control center** at `http://localhost:8000/` — pick a model
from the header dropdown. The VRAM preflight check runs automatically before
launch and will suggest a safe `-ngl`/`tensor_split` if the model + context
won't fit as configured. Click **▶ Load**, then chat, or switch to
**🤖 Agent Task** mode for autonomous file edits.

**3. (Optional) Auto-tune a model** for the best tensor-split / MoE-offload
ratio via real `llama-bench` sweeps:
```
python autotune.py --profile <path-to-a-profile.json>
```
This currently requires a standalone JSON profile file (with `model_path`,
`llama_bin_dir`, `autotune` candidate lists, and a `tuned` section to write
results into) rather than a bare `.gguf` — it predates the dynamic
`model_configs.json`-driven path used by `server_manager.py` and the UI, and
hasn't been migrated yet. For day-to-day use, the UI's config drawer +
preflight suggestions cover most tuning without needing this script; treat
`autotune.py` as an advanced/manual tool for now.

**4. Or drive it as a plain API** — `http://localhost:8000/v1/chat/completions`
is OpenAI-compatible (works with the OpenAI Python SDK, LM Studio-style
clients, etc.), and `/agent/run` streams the autonomous coding loop over SSE
for programmatic use. Switch the loaded model without the UI:
```
curl -X POST http://localhost:8000/control/switch -d '{"profile":"E:/AI/Models/your-model.gguf"}'
```

## Files

- `autotune.py` — sweeps GPU-split / MoE-offload settings via `llama-bench`
  against a standalone JSON profile (see the caveat above).
- `server_manager.py` — FastAPI bootstrap: mounts the UI, wires up
  `routes/*`, and manages process lifespan (llama-server, MCP, plugins,
  keepalive, background memory tasks).
- `routes/` — HTTP surface: `control` (lifecycle/profiles/GPU/config),
  `agent` (autonomous coding loop, permissions, vision), `chat` (direct
  completions), `projects` (sessions/workspace browsing), `capabilities`
  (skills/MCP/plugins/shell settings), `cloud` (provider/lane bindings),
  `proxy` (OpenAI-compatible passthrough to `llama-server`).
- `core/` — orchestration internals: `config.py` (paths/defaults, incl.
  `backend`/`llama_bin_dir`/`gpu_devices` — the machine-specific knobs),
  `backend.py` (Vulkan/CUDA device-naming abstraction), `profiles.py`
  (dynamic profile construction from a raw `.gguf` + `model_configs.json`
  overrides), `small_model.py` (small-model lane manager + `APP_CONFIG`),
  `cloud.py` (cloud provider/lane resolution), `agent_loop.py`/
  `agent_tools.py` (tool execution, sandboxing, plan tracking), `vram.py`
  (device discovery, tensor-split computation, VRAM preflight checks),
  `gpu.py` (Windows perf-counter GPU stats), `roles.py` (named sub-agent
  presets).
- `config/app.json` — main app config (models dir, small-model lanes,
  agent/router settings, roles, capabilities). `config/model_configs.json`
  — per-model launch overrides, keyed by GGUF filename (tensor split, ngl,
  context size, etc.) — the source of truth for tuned settings; safe to
  delete to reset a model to defaults. `config/providers/user_<id>.json` —
  each user's cloud providers, own cloud models, job choices and answer-check
  settings (git-ignored; API keys are in the OS keychain, not in the file).
- `ui.html` + `static/js/*` — the single-page web control center (chat,
  agent mode, settings, monitor, usage report). `static/js/theme.js` holds
  the (optional, cosmetic) machine-specific GPU display-name map.
- `scripts/list_devices.ps1` — quick Vulkan/CUDA device check.
- `scripts/start.ps1` — convenience wrapper around `server_manager.py`.
- `scripts/setup_windows.ps1` — one-time Python dependency install + a
  printed checklist of the manual steps above.

## Security note (payment-adjacent environments)

This repo is developed by someone working at a PCI-DSS certified payment
gateway; even though this app has nothing to do with payment processing,
the same discipline applies if you run it near anything that touches
cardholder data. If you deploy this alongside anything in PCI scope, keep it
network-isolated from that scope: this app runs a local shell tool
(permission-gated but still powerful). Cloud API keys are stored in the OS
keychain (Windows Credential Manager, DPAPI-protected per Windows user), not
in plaintext files. Don't point `workspace_dir` /
`common_dir` at directories containing real payment credentials, PANs, or
tokens; treat `usage.db` / `memory.db` as sensitive data at rest (not
encrypted); and never paste real card numbers, tokens, or merchant
credentials into chat/agent sessions — use `[PLACEHOLDER]` values in any
example or test data.

## Honesty check

The dynamic GPU-count/backend logic (`core/backend.py`,
`compute_tensor_split()`) has been verified with unit-level checks (1/2/3
simulated devices, uneven VRAM splits) and confirmed to reproduce today's
hand-tuned 2x-A770 `9,11` ratio, but it has only actually been *run* against
real hardware on the original 2x Arc A770 (Vulkan) box — the CUDA path is
implemented from current llama.cpp CLI/flag documentation, not verified
against real NVIDIA hardware yet. If you try it on NVIDIA and `-dev CUDA0,…`
or `CUDA_VISIBLE_DEVICES` don't behave as documented, `llama-bench --help`
and `--list-devices` on your build will tell you immediately, and the fix is
localized to `core/backend.py`.

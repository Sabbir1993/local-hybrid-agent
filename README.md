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
4. **Install Python 3.10+** and the orchestration deps:
   ```
   pip install -r requirements.txt
   ```
5. **Edit `profiles/*.json`**: set `llama_bin_dir` to your unzipped folder,
   and `model_path` to your downloaded GGUF for each model (get GGUFs from
   Hugging Face — search `Qwen3.8-27B GGUF` / `Qwen3.8-Flash-Next GGUF`;
   Unsloth's quants are a solid default).

## Usage

**1. Auto-tune (run once per model, and again if you change quant/context):**

```
python autotune.py --profile profiles/qwen3.8-27b.json
python autotune.py --profile profiles/qwen3.8-flash-next.json
```

This runs a grid of short `llama-bench` passes (tensor-split ratios for the
dense model; `--n-cpu-moe` steps for the MoE model), prints a results table,
and writes the best-found flags back into `tuned` in the profile JSON. Each
sweep takes a few minutes — it's real benchmarking, not a guess.

**2. Run the server:**

```
python server_manager.py --profile profiles/qwen3.8-27b.json
```

This launches `llama-server` with the tuned flags, exposes an
OpenAI-compatible API on `http://localhost:8000/v1/...` (proxied, with
request logging and measured tokens/sec per request), and restarts the
underlying process if it crashes. Switch models without restarting your
client:

```
curl -X POST http://localhost:8000/control/switch -d '{"profile":"profiles/qwen3.8-flash-next.json"}'
```

**3. Talk to it** exactly like any OpenAI-compatible endpoint (works with
the OpenAI Python SDK, LM Studio-style clients, etc.) at
`http://localhost:8000/v1/chat/completions`.

## Files

- `autotune.py` — sweeps GPU-split / MoE-offload settings via `llama-bench`,
  writes tuned config into the profile.
- `server_manager.py` — FastAPI process manager + proxy: launches
  `llama-server`, health-checks/restarts it, logs per-request tokens/sec,
  supports live profile switching.
- `profiles/qwen3.8-27b.json` — dense-model profile (tensor-split sweep only,
  no MoE offload — it's not an MoE model).
- `profiles/qwen3.8-flash-next.json` — MoE-model profile (tensor-split +
  `--n-cpu-moe` sweep, larger context default given its 262K native window).
- `scripts/list_devices.ps1` — quick Vulkan device check.
- `scripts/start.ps1` — convenience wrapper around `server_manager.py`.

## Honesty check

I don't have Arc hardware in the environment this was written in, so none
of this has been run against real A770s — it's built strictly from current
llama.cpp flag documentation and the specific bug reports linked above, not
guessed. Treat the first `autotune.py` run as the real verification step:
if a flag has changed name in a newer llama.cpp release, `llama-bench
--help` will tell you immediately, and the fix is a one-line change in
`autotune.py`'s `FLAG_*` constants at the top of the file.

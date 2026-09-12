# Dual-GPU Verification & Performance Test Log

**Date:** 2026-09-07 (evening session)
**Machine:** i5-13500, 2x Intel Arc A770 16GB (driver 32.0.101.8991), UHD 770 iGPU, 64GB RAM
**Stack:** llama.cpp b10840 Vulkan (`E:\AI\llama-vulkan`), server_manager.py proxy (:8000) -> llama-server (:8090)
**Model:** Qwen3.8-27B Q4_K_M (15.65 GiB, 27.32B params)
**Launch args:** `-c 32768 -ngl 999 -sm layer --tensor-split 9,11 -dev Vulkan1,Vulkan2 -fa on -ctk f16 -ctv f16 --metrics`

---

## Summary of results

| Test | tg (generation) | pp (prompt eval) | Notes |
|---|---|---|---|
| llama-bench (fresh process, autotune) | **14.6 t/s** | 252.6 t/s | tuned config `9,11`, `-sm layer` |
| Server warmup req 1 (right after launch) | **14.44 t/s flat** | 65.9 t/s (65 tok) | task 0, sustained over 400 tok |
| Server warmup req 2 (t+68s, still hot) | **14.44 t/s flat** | 20.1 t/s (4 tok) | task 403 |
| My req 1 (after **37 min idle**) | 11.5 avg, 13.5 peak | 35.9 t/s | sawtooth 13.5 <-> 9.0 |
| My req 2 (back-to-back) | 8.4 avg | 37.1 t/s | decays 11.25 -> 6.9 |
| My req 3 (back-to-back) | 7.8 avg | 35.0 t/s | flat ~7.0 after token 120 |
| My req 4 (after 4 min cooldown) | ~7.7 avg | 34.9 t/s | started 11.2 while VRAM promoting, settled 7.7 |
| My req 5 (counter polling active) | 7.5 avg | - | polling made no difference |
| Probe reqs (120 tok each, idle-demoted) | 5.9-8.8 | - | |

**Peak verified speed: 14.44 t/s sustained dual-GPU** (matches bench 14.6). Single-GPU baseline before this project: 9.7 t/s. Net gain: **+49% generation, +74% prompt processing**.

---

## GPU 2 "not being used" - resolved (two causes)

### Cause 1: WDDM idle VRAM demotion (cosmetic)
The headless A770 (`LUID 0x04ab067c`) has its model pages demoted from VRAM to system RAM by the Intel/WDDM power management **~70s after going idle**:

```
IDLE  memory: display=8.86GB  headless=0.00GB   <- looks like single-GPU in Task Manager
POST  memory: display=8.88GB  headless=10.13GB  <- 2s after a generation starts
```

Memory re-promotes over PCIe the moment generation begins. **Task Manager readings while idle are misleading.**

### Cause 2: compute engine, not 3D engine (measurement artifact)
llama.cpp Vulkan runs on the GPU **compute** engine (`engtype_compute`), not the 3D engine. Task Manager's default GPU graphs and a 3D-engine filter both show ~0%. Raw counter dump **mid-generation** (pid 24852 = llama-server):

```
pid_24852_luid_0x00000000_0x04ab067c_phys_0_eng_5_engtype_compute = 53%   <- headless A770
pid_24852_luid_0x00000000_0x04ac733a_phys_0_eng_5_engtype_compute = 45%   <- display A770
```

**Both A770s actively compute during generation. Dual-GPU offload is working.**

To see this yourself in Task Manager: Performance tab -> GPU 2 -> change one of the small graphs to "Compute" and watch during generation; or check "Dedicated GPU memory" *during* a request (not after).

---

## NEW FINDING: idle demotion permanently degrades speed until restart

The most important discovery of this session:

- The server's first two requests after launch ran at **14.44 t/s flat** (zero variance over 800 tokens).
- After **37 minutes of idle** (VRAM demoted), every subsequent request ran at 11.5 -> 8.4 -> 7.8 -> 7.7 t/s, **and never recovered to 14.4** - even back-to-back, even after 4 min cooldown, even with memory fully re-promoted (10.13GB resident on the headless card).
- Ruled out:
  - *Thermal/power throttling*: 4-minute cooldown did not restore speed; prompt eval (CPU-bound, 35 t/s) rock-stable throughout; decay pattern monotonic with idle time, not load.
  - *Competing processes*: only Chrome GPU process active, pinned to the iGPU (`0x0001665a`), 0% on A770s.
  - *Counter polling side effects*: request 5 with active polling = same 7.5 t/s.
  - *Context/KV effects*: pp stayed ~35 t/s from the very first request after idle (vs 65.9 during warmup with identical args).

**Conclusion:** once WDDM demotes the pages, the process never gets full-speed VRAM back (promoted pages presumably land in worse residency / shared-memory state). Only a process restart restores 14.44 t/s.

**Practical fix (not yet implemented):** add a keepalive to `server_manager.py` - a 1-token generation every ~25-30s during idle touches the weight pages and prevents demotion entirely. The warmup requests prove this works: continuous activity = permanently flat 14.44 t/s.

---

## Root causes fixed earlier in this project (recap)

1. **Separator quirk:** llama-bench parses `--tensor-split` with `/` separators, llama-server with `,`. The comma form in llama-bench silently benched a single device (root cause of the original 15.3GB-on-one-card load). autotune.py now translates (`normalize_ts_for_bench`), server_manager.py passes `-dev Vulkan1,Vulkan2` explicitly.
2. **Device filtering:** `GGML_VK_VISIBLE_DEVICES` env var kept for llama-bench; llama-server uses explicit `-dev` (env var was unreliable via Popen).
3. **Vulkan enumeration:** Vulkan0 = UHD 770 iGPU (must be excluded), Vulkan1/2 = A770s.

---

## Environment / tooling notes

- `Get-Counter '\GPU Engine(*)'` takes ~2-6s per call on this machine - impractical for tight sampling loops; single dumps work fine.
- Engine instance LUID is `$InstanceName.Split('_')[4]` (e.g. `pid_24852_luid_0x00000000_0x04ab067c_phys_0_eng_5_engtype_compute`).
- `rg` is not installed; use `Select-String` / built-in grep tool.
- First request after server start stalls ~13s on Vulkan shader compilation (pp 5.7 t/s); manager's automatic warmup absorbs this.
- `llama-server` WorkingSet ~15.5GB / PrivateMemory ~19GB is normal (mmap'd GGUF + demoted pages), not CPU fallback.
- LUID map: `0x04ac733a` = A770 with display attached, `0x04ab067c` = headless A770, `0x000165f7`/`0x0001665a` = iGPU/other.
- n_slots = 4 (context split 4x8192); all tests used slot id 3.

---

## Open items

1. **Implement keepalive in server_manager.py** (highest impact - prevents the 14.4 -> 7.7 t/s degradation).
2. Optional: speculative decoding with `E:\AI\Models\mtp-Qwen3.8-27B-Q4_0.gguf` (1.3GB MTP draft) for higher tok/s.
3. Optional: Qwen3.8-Flash-Next MoE profile model not yet downloaded.

---

# Session 2 (2026-09-07 late evening): Control UI + keepalive

## Built: LM-Studio-style web UI (`ui.html`, served by server_manager.py at http://localhost:8000/)

- **Sidebar — Power:** Start Model / Restart / Unload Model / **CLEAR EVERYTHING** (red, with confirm modal; unloads model + frees VRAM + clears chat)
- **Sidebar — Keepalive toggle:** 1-token ping every 25s while idle (fix for the demotion decay found in session 1)
- **Sidebar — GPUs:** live per-adapter VRAM bars (x/16GB) + compute-engine % (the engine llama.cpp actually uses - 3D shows 0)
- **Sidebar — Settings:** system prompt, temperature, max tokens
- **Main:** streaming chat (SSE), collapsible "Thinking" block for reasoning output, per-response tok/s, abort/stop button, clear chat, profile switcher (flags missing models)
- New backend endpoints: `/control/stop|start|restart|gpu|profiles|keepalive`, extended `/control/status`, `GET /` serves the UI. Keepalive backs off automatically during real requests.

## Test results (all PASS)

| Test | Result |
|---|---|
| GET / serves UI (25,800 bytes, all controls present) | PASS |
| /control/profiles lists both profiles, flags missing model | PASS |
| /control/gpu returns 4 adapters + per-pid compute (llama-server visible on BOTH A770 LUIDs) | PASS |
| /control/stop: llama-server gone, pid=null, no zombie watchdog restart | PASS |
| /control/start: reload healthy, warmup (1 tok) absorbs shader compile | PASS |
| Fresh-process generation | **14.44 t/s flat** (full speed restored) |
| Keepalive ON, 110s idle: headless A770 **still 10.13 GB** (was 0 GB by ~70s before) | PASS |
| Post-idle generation with keepalive | **13.8 t/s** (was 7.7 t/s permanent decay before) |
| Keepalive ping cost | ~194 ms per 1-token ping, every 25s (negligible) |

## Verified workflow for full speed at any time

Model left idle for hours -> speed decayed? Press **CLEAR EVERYTHING** then **Start Model** -> fresh process = 14.4 t/s again. Or just leave **Keepalive ON** and it never decays (current state: keepalive is ON).

## Updated open items

1. ~~Keepalive~~ DONE (UI toggle + backend, verified).
2. Optional: speculative decoding with MTP draft model for >14.4 t/s.
3. Optional: download Qwen3.8-Flash-Next model for the MoE profile.

---

# Session 3 (2026-09-08 evening): Full config UI + fixes

## Fixed

- **Modal never closed**: `#modal-bg { display:flex }` overrode the `hidden` attribute's `display:none`. Fix: `#modal-bg[hidden] { display:none !important }` (it was actually visible on page load, too).
- Sidebar text ~15% smaller (logo, buttons, labels, GPU stats, inputs).

## Added: Model Config panel (sidebar)

Context size, GPU layers (-ngl), CPU threads (-t), batch threads (-tb), batch size (-b), ubatch size (-ub), parallel slots (-np), tensor split, split mode (layer/row), flash attention (on/off/auto), KV cache type (f16/q8_0/q5_0/q4_0/bf16/f32), keepalive interval. Backend: `GET/POST /control/config` with validation + clamping, persists into the profile JSON, relaunches llama-server to apply (keepalive interval applies live).

## Added: sampling controls (chat requests)

Temperature, Top P, Min P, Top K, Repeat penalty, Max tokens - sent per-request from the chat UI.

## Test results (all PASS)

| Test | Result |
|---|---|
| GET /control/config returns effective config | PASS |
| POST threads=14, ubatch=512, keepalive=20 -> launch line shows `-t 14 -ub 512`, healthy in 14.2s | PASS |
| Config persisted into profiles/qwen3.8-27b.json (`"threads": 14`, `"ubatch_size": 512`) | PASS |
| POST tensor_split="banana" -> HTTP 400, no restart | PASS |
| Generation after config change (-t 14 -ub 512) | 13.8 t/s (full speed) |
| UI contains new controls; modal CSS fix present in served HTML | PASS |
| Live user session via browser UI (user clicked Restart + chatted): streamed 2.5s, no errors, restart_count=0 | PASS |

Note: brief 503 "Loading model" responses during testing were a race with the user manually restarting via the UI - not a bug. llama-server health takes ~14s to reload after a config change.

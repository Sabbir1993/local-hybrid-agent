# Phase 1 — Stop the bleeding (approved, ready to execute)

Worktree is clean — drift was landed in `bf795b5` (includes `routes/telemetry_ws.py`, cookie/Bearer-only auth, no `?token=`).

## Real fixture captured (b11429, commit d81235049, Vulkan)
Run: `llama-bench.exe -m E:/AI/Models/orchestrator/Qwen2.5-VL-3B-Instruct-Q4_K_M.gguf -ngl 99 -sm layer -fa auto -p 16 -n 16 -r 1 -o json`
- TWO rows: pp row `n_prompt=16,n_gen=0`; tg row `n_prompt=0,n_gen=16`
- Each row carries BOTH `avg_ts` (t/s) and `avg_ns` (ns/token) + `stddev_ts` (+`samples_ns/ts`)
- pp row avg_ts=28.728729, tg row avg_ts=10.109277
- Full JSON captured in terminal transcript; to be saved as `tests/fixtures/llama_bench_b11429.json`

## Commit sequence (each independent)

1. ~~sync: land drift~~ — DONE by user (bf795b5)
2. **fix(bench): parse fixes**
   - `core/llama_bench.py`: extract pure `parse_bench_json(stdout) -> dict` used by `run_bench`
     - `_row_ts(row)`: prefer `avg_ts`; fallback `avg_ns` → `ts = 1e9/avg_ns`, stddev `sd_ts = stddev_ns*1e9/avg_ns²`
     - rows: `np>0,ng==0`→pp; `ng>0,np==0`→tg; both>0→combined (tg=ts, `combined: True`)
     - carry `pp_stddev_ts`/`tg_stddev_ts`; keep failure reasons `unparseable`/`no_recognizable_rows`
     - remove tautology `ng_ >= ng_` (llama_bench.py:137)
   - New `tests/fixtures/llama_bench_b11429.json` (real capture above)
   - New `tests/test_llama_bench_parse.py`: real fixture (pp=28.7287, tg=10.1093), avg_ns-only build variant, combined-row variant, stddev_ns propagation (ts=1e9/1582704700≈6.318), unparseable, garbage rows
3. **fix(autotune): failure classification + robustness**
   - `run_bench` distinct reasons: `oom` (marker hit) / `crash` (nonzero exit, no marker) / `timeout` / `unparseable`
   - `sweep_n_cpu_moe`: break only on `oom`; crash/timeout → retry once then skip with "non-OOM failure does NOT mean it doesn't fit"
   - `bench_timeout_s` in autotune block, default 300
   - auto-create `profile["autotune"]` defaults (tensor_split_candidates=None→algorithmic, n_gpu_layers, split_mode="layer", bench 512/128, repetitions=2, moe candidates) + `profile.setdefault("tuned", {})`
   - `profile_path.write_text` → `core.config.atomic_write_json` (autotune.py:173)
   - win print includes stddev (autotune.py:91 keep max(tg_ts) for now)
   - tests: mock run_bench for oom/crash/timeout paths
4. **security(companion-shell): routes/projects/companion_endpoints.py:110–207**
   - delete server-side `create_subprocess_exec` fallback → `{"ok":False,...,"is_remote":True}` 400 (mirror fs_browse)
   - `Depends(require_permission(...))` level used by agent loop (chat.use or existing key — check routes/agent/permissions.py)
   - `audit_log()` call; run command through `core/shell_tools` always-refused list
   - keep `approved_in_app=True` (user-typed = in-app initiated)
   - tests: no-companion→400 is_remote, no-permission→403, denied pattern→400
5. **telemetry: real sensors or honest N/A**
   - `core/gpu.py`/`core/telemetry.py`: try Windows perf counter `\GPU Data(*)\Temperature` (+`nvidia-smi` for CUDA later); unavailable → `null` temp/power/pcie, no `32.0` default total
   - delete fabricated formulas (telemetry.py:79–85 base_temp/power, :171 pcie); token-weighted averages (or rename honestly); log recurring /slots failures (telemetry.py:148–149)
   - `static/js/telemetry-dashboard.js`: "N/A" pill + tooltip
   - tests for unit shapes
6. **test(telemetry-ws): tests/test_telemetry_ws.py** (4401 no-cookie, 4403 missing monitor.view, happy path 1 snapshot) + extend tests/test_auth_invariant.py to walk WebSocket routes if it currently skips them
7. **fix(state): watchdog hot-swap race (core/state.py:423–438)**
   - snapshot target before `sleep(backoff)`; after sleep: if a live process exists OR target changed → skip restart (+ log); fix missing `)` :413–415
   - tests where feasible; note manual verification step
8. **Final verification**: `python -m unittest discover tests`, `ruff`, `npm run lint`, `node tests/js/run_all.js`

## Env facts
- llama-vulkan bin: `E:/AI/vulkan-arc/llama-vulkan`; models: `E:/AI/Models`; config at `config/app.json` runs.{vulkan,cuda}
- `core.config` exports: atomic_write_json, apply_runtime, load model configs via core/profiles/config_store
- nos: `GGML_VK_VISIBLE_DEVICES=0` selects UHD iGPU as Vulkan device 0 (device index ≠ lanenumbering in --list-devices)

#!/usr/bin/env python3
"""
autotune.py - empirically sweeps GPU-split / MoE-offload settings for a
dual-Arc-A770 (asymmetric PCIe) setup using llama.cpp's `llama-bench`, and
writes the best-found config back into the profile JSON.

Why this exists instead of a hardcoded config: your two A770s may not be
identical (different board partners, different PCIe generation/lanes), and
the effect of MoE CPU-offload (--n-cpu-moe) on a PCIe-3.0-limited card is
something to *measure*, not guess. This script runs short, real
llama-bench passes and picks whatever wins.

Requirements: Python 3.10+, a Vulkan-backend llama.cpp Windows build
(llama-<build>-bin-win-vulkan-x64.zip from
https://github.com/ggml-org/llama.cpp/releases) with llama-bench.exe in
`llama_bin_dir` from the profile.

NOTE ON FIELD NAMES: llama-bench's JSON output field names have changed
across llama.cpp releases before. If parsing fails, run
`llama-bench --help` and `llama-bench -m <model> -o json -p 16 -n 16`
by hand once, compare the JSON keys to RESULT_FIELDS below, and adjust.
This script prints the raw JSON on any parse failure specifically so that
fix is quick.
"""

import argparse
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

from core.backend import visible_devices_env
from core import vram

# Known-good as of the llama.cpp docs/issues consulted when this was written
# (docs/multi-gpu.md, docs/build.md, llama-bench manpage). If your build's
# `llama-bench --help` differs, edit these - they're the only place CLI
# flag names live.
FLAG_MODEL = "-m"
FLAG_NGL = "-ngl"
FLAG_SPLIT_MODE = "-sm"
FLAG_TENSOR_SPLIT = "--tensor-split"
FLAG_N_CPU_MOE = "-ncmoe"  # long form: --n-cpu-moe
FLAG_FLASH_ATTN = "-fa"
FLAG_N_PROMPT = "-p"
FLAG_N_GEN = "-n"
FLAG_REPETITIONS = "-r"
FLAG_OUTPUT = "-o"

# Substrings that indicate an out-of-memory / device failure rather than a
# clean benchmark result. Intentionally broad - a false positive here just
# means autotune backs off one step early, which is the safe direction.
OOM_MARKERS = (
    "out of memory",
    "out of device memory",
    "outofdevicememoryerror",
    "vk_error_out_of_device_memory",
    "failed to allocate",
    "cudamalloc failed",
    "insufficient memory",
    "ggml_vulkan: device memory allocation",
)


def build_gpu_env(profile: dict) -> dict:
    # Restrict the backend to the devices in profile["gpu_devices"] (physical
    # device indices as printed by `llama-bench --list-devices`), so an iGPU
    # (e.g. UHD 770) doesn't get folded into the tensor split. The env var
    # name depends on the backend (Vulkan vs CUDA build of llama.cpp).
    env = dict(os.environ)
    devs = profile.get("gpu_devices")
    if devs:
        env_var = visible_devices_env(profile.get("backend", "vulkan"))
        env[env_var] = ",".join(str(d) for d in devs)
    return env


def find_llama_bench(bin_dir: str) -> Path:
    bin_dir_path = Path(bin_dir)
    candidates = [bin_dir_path / "llama-bench.exe", bin_dir_path / "llama-bench"]
    for c in candidates:
        if c.exists():
            return c
    found = shutil.which("llama-bench")
    if found:
        return Path(found)
    sys.exit(
        f"Couldn't find llama-bench(.exe) in {bin_dir} or on PATH.\n"
        f"Check 'llama_bin_dir' in your profile JSON points at the unzipped "
        f"llama-<build>-bin-win-vulkan-x64 folder."
    )


def normalize_ts_for_bench(tensor_split: str) -> str:
    # llama-bench (b10840) parses --tensor-split with '/' separators, while
    # llama-server parses the same flag with ',' separators. Profiles store
    # comma form (what llama-server consumes); translate for llama-bench.
    return tensor_split.replace(",", "/")


def run_bench(bench_path: Path, model_path: str, *, ngl: int, split_mode: str,
              tensor_split: str, n_cpu_moe: int | None, flash_attn: str,
              n_prompt: int, n_gen: int, repetitions: int, timeout: int = 180,
              env: dict | None = None):
    cmd = [
        str(bench_path),
        FLAG_MODEL, model_path,
        FLAG_NGL, str(ngl),
        FLAG_SPLIT_MODE, split_mode,
        FLAG_TENSOR_SPLIT, normalize_ts_for_bench(tensor_split),
        FLAG_FLASH_ATTN, flash_attn,
        FLAG_N_PROMPT, str(n_prompt),
        FLAG_N_GEN, str(n_gen),
        FLAG_REPETITIONS, str(repetitions),
        FLAG_OUTPUT, "json",
    ]
    if n_cpu_moe is not None:
        cmd += [FLAG_N_CPU_MOE, str(n_cpu_moe)]

    print(f"  $ {' '.join(cmd)}")
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, env=env)
    except subprocess.TimeoutExpired:
        return {"ok": False, "reason": "timeout", "raw": ""}

    combined = (proc.stdout or "") + (proc.stderr or "")
    lowered = combined.lower()
    if proc.returncode != 0 or any(marker in lowered for marker in OOM_MARKERS):
        return {"ok": False, "reason": "oom_or_error", "raw": combined[-2000:]}

    try:
        data = json.loads(proc.stdout)
    except json.JSONDecodeError:
        # llama-bench sometimes writes a markdown table to stdout alongside
        # JSON, or JSON to a different stream depending on version - try to
        # salvage a JSON array from anywhere in stdout.
        start = proc.stdout.find("[")
        end = proc.stdout.rfind("]")
        if start != -1 and end != -1:
            try:
                data = json.loads(proc.stdout[start:end + 1])
            except json.JSONDecodeError:
                return {"ok": False, "reason": "unparseable", "raw": proc.stdout[-2000:]}
        else:
            return {"ok": False, "reason": "unparseable", "raw": proc.stdout[-2000:]}

    pp_ts, tg_ts = None, None
    for row in data:
        try:
            np_ = int(row.get("n_prompt", 0))
            ng_ = int(row.get("n_gen", 0))
            ts = float(row.get("avg_ts", row.get("avg_ns", 0)))
        except (TypeError, ValueError):
            continue
        if ng_ > 0 and (pp_ts is None or ng_ >= ng_):  # generation row
            tg_ts = ts
        if np_ > 0 and ng_ == 0:
            pp_ts = ts

    if pp_ts is None and tg_ts is None:
        return {"ok": False, "reason": "no_recognizable_rows", "raw": json.dumps(data)[:2000]}

    return {"ok": True, "pp_ts": pp_ts, "tg_ts": tg_ts, "raw": data}


def default_tensor_split_candidates(profile) -> list[str]:
    """Algorithmic candidates for any GPU count, used when the profile has no
    (or an empty) "tensor_split_candidates" list. Seeds from a VRAM-proportional
    split (core.vram.compute_tensor_split) and adds a small neighborhood of
    +/-1 perturbations per device so the sweep still explores nearby ratios,
    the way the old hand-authored 2-GPU lists did."""
    gpu_devices = profile.get("gpu_devices") or []
    if len(gpu_devices) <= 1:
        return ["1"]
    devs = vram.query_devices(profile.get("llama_bin_dir"))
    by_idx = {d["index"]: d for d in devs}
    target_devs = [by_idx[i] for i in gpu_devices if i in by_idx]
    if len(target_devs) != len(gpu_devices):
        # device list unavailable (e.g. bench not run yet) - fall back to equal split
        target_devs = [{"free_b": 1} for _ in gpu_devices]
    seed = vram.compute_tensor_split(target_devs)
    seed_vals = [int(v) for v in seed.split(",")]
    candidates = {seed}
    for i in range(len(seed_vals)):
        for delta in (-1, 1):
            v = list(seed_vals)
            v[i] = max(1, v[i] + delta)
            candidates.add(",".join(str(x) for x in v))
    return sorted(candidates)


def sweep_tensor_split(bench_path, profile, n_cpu_moe_for_sweep):
    auto = profile["autotune"]
    env = build_gpu_env(profile)
    results = []
    ts_candidates = auto.get("tensor_split_candidates") or default_tensor_split_candidates(profile)
    print("\n=== Stage 1: tensor-split sweep ===")
    for ts in ts_candidates:
        print(f"\n-- tensor-split {ts} --")
        r = run_bench(
            bench_path, profile["model_path"],
            ngl=auto["n_gpu_layers"], split_mode=auto["split_mode"],
            tensor_split=ts, n_cpu_moe=n_cpu_moe_for_sweep,
            flash_attn=profile.get("flash_attn", "auto"),
            n_prompt=auto["bench_prompt_tokens"], n_gen=auto["bench_gen_tokens"],
            repetitions=auto["repetitions"], env=env,
        )
        if r["ok"]:
            print(f"   pp={r['pp_ts']:.1f} t/s  tg={r['tg_ts']:.1f} t/s")
            results.append((ts, r))
        else:
            print(f"   FAILED ({r['reason']}) - skipping this split")
    if not results:
        sys.exit("No tensor-split candidate produced a usable result. See raw output above.")
    best_ts, best_r = max(results, key=lambda kv: kv[1]["tg_ts"] or 0)
    print(f"\nBest tensor-split: {best_ts} ({best_r['tg_ts']:.1f} t/s generation)")
    return best_ts, best_r, results


def sweep_n_cpu_moe(bench_path, profile, tensor_split):
    auto = profile["autotune"]
    env = build_gpu_env(profile)
    print("\n=== Stage 2: --n-cpu-moe sweep (high offload -> low offload) ===")
    print("Stopping at the first OOM and backing off one step, per upstream guidance.")
    last_good = None
    for ncmoe in auto["n_cpu_moe_candidates"]:
        print(f"\n-- n-cpu-moe {ncmoe} --")
        r = run_bench(
            bench_path, profile["model_path"],
            ngl=auto["n_gpu_layers"], split_mode=auto["split_mode"],
            tensor_split=tensor_split, n_cpu_moe=ncmoe,
            flash_attn=profile.get("flash_attn", "auto"),
            n_prompt=auto["bench_prompt_tokens"], n_gen=auto["bench_gen_tokens"],
            repetitions=auto["repetitions"], env=env,
        )
        if r["ok"]:
            print(f"   pp={r['pp_ts']:.1f} t/s  tg={r['tg_ts']:.1f} t/s  -> OK")
            last_good = (ncmoe, r)
        else:
            print(f"   FAILED ({r['reason']}) - this offload level doesn't fit. "
                  f"Using last good value.")
            break
    if last_good is None:
        sys.exit(
            "Every --n-cpu-moe candidate failed, including the most conservative one.\n"
            "Check model_path is correct and the model isn't larger than your combined "
            "VRAM+RAM even fully CPU-offloaded, or lower context_size in the profile."
        )
    ncmoe, r = last_good
    print(f"\nBest --n-cpu-moe: {ncmoe} ({r['tg_ts']:.1f} t/s generation)")
    return ncmoe, r


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--profile", required=True, help="Path to profile JSON")
    args = ap.parse_args()

    profile_path = Path(args.profile)
    profile = json.loads(profile_path.read_text())
    bench_path = find_llama_bench(profile["llama_bin_dir"])

    if profile.get("gpu_devices"):
        print(f"Restricting Vulkan devices to {profile['gpu_devices']} via "
              f"GGML_VK_VISIBLE_DEVICES (indices from `llama-bench --list-devices`).")

    if not Path(profile["model_path"]).exists():
        print(f"WARNING: model_path '{profile['model_path']}' doesn't exist on this "
              f"machine yet - edit the profile before running autotune for real.",
              file=sys.stderr)

    is_moe = profile.get("model_type") == "moe"
    seed_n_cpu_moe = None
    if is_moe:
        candidates = profile["autotune"]["n_cpu_moe_candidates"]
        seed_n_cpu_moe = candidates[len(candidates) // 2]  # a safe middle value

    best_ts, best_ts_result, all_ts_results = sweep_tensor_split(
        bench_path, profile, seed_n_cpu_moe
    )

    tuned = {
        "tensor_split": best_ts,
        "n_gpu_layers": profile["autotune"]["n_gpu_layers"],
        "measured_pp_tokens_per_sec": best_ts_result["pp_ts"],
        "measured_tg_tokens_per_sec": best_ts_result["tg_ts"],
        "tuned_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
    }

    if is_moe:
        best_ncmoe, ncmoe_result = sweep_n_cpu_moe(bench_path, profile, best_ts)
        tuned["n_cpu_moe"] = best_ncmoe
        tuned["measured_pp_tokens_per_sec"] = ncmoe_result["pp_ts"]
        tuned["measured_tg_tokens_per_sec"] = ncmoe_result["tg_ts"]

    profile["tuned"].update(tuned)
    profile_path.write_text(json.dumps(profile, indent=2))
    print(f"\nWrote tuned config back into {profile_path}:")
    print(json.dumps(tuned, indent=2))
    print(
        "\nReminder: watch Task Manager's per-GPU memory the first time you run this "
        "live (server_manager.py) - there's a known upstream bug where --n-cpu-moe can "
        "load one GPU's VRAM before touching the second (ggml-org/llama.cpp#15136, "
        "reported on CUDA, unconfirmed on Vulkan). If you see that, tell me and I'll "
        "script --override-tensor per-tensor placement instead."
    )


if __name__ == "__main__":
    main()

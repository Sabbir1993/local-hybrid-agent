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
import sys
import time
from pathlib import Path

from core.config import apply_runtime
from core import vram
from core.llama_bench import build_gpu_env, find_llama_bench, run_bench



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
    profile = apply_runtime(json.loads(profile_path.read_text()))
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

"""
core/llama_bench.py - Run one llama-bench pass and parse its JSON (used by autotune.py).

llama-bench's JSON field names have changed across llama.cpp releases; on a parse
failure the raw output is printed so the field mapping here is quick to fix.
"""

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from core.backend import visible_devices_env


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

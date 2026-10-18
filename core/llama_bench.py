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


def _row_ts(row) -> tuple:
    """tokens/sec for one llama-bench JSON row, distrustful of field generations.
    Modern builds carry avg_ts (tokens/sec); some emit only avg_ns
    (nanoseconds per token), which must be inverted - going through with
    avg_ns as if it already were tokens/sec fed fake 1e9-scale numbers into
    the sweep winner. Returns (ts, stddev_ts), either None when absent."""
    try:
        avg_ts = row.get("avg_ts")
        if avg_ts is not None:
            sd = row.get("stddev_ts")
            return float(avg_ts), (float(sd) if sd is not None else None)
        avg_ns = row.get("avg_ns")
        if avg_ns is not None and float(avg_ns) > 0:
            ns = float(avg_ns)
            sd_ns = row.get("stddev_ns")
            # ts = 1e9/ns, so the propagated deviation is d(ts) = 1e9*sd_ns/ns^2
            sd = (1e9 * float(sd_ns) / (ns * ns)) if sd_ns else None
            return 1e9 / ns, sd
    except (TypeError, ValueError):
        return None, None
    return None, None


def parse_bench_json(stdout: str) -> dict:
    """Parse llama-bench -o json output into a result dict of the same shape
    run_bench returns. Pure function so the parser, not the process spawn,
    is what the tests pin down.

    Row layout (verified against build b11429 / commit d81235049): with both
    -p and -n set, the build emits two rows - a prefill row (n_prompt>0,
    n_gen==0) and a generation row (n_prompt==0, n_gen>0). Older builds ram
    both into one combined row; there, combined=True and its timestamp acts
    as the generation-side number."""
    try:
        data = json.loads(stdout)
    except json.JSONDecodeError:
        # llama-bench sometimes writes a markdown table to stdout alongside
        # JSON, or JSON to a different stream depending on version - try to
        # salvage a JSON array from anywhere in stdout.
        start = stdout.find("[")
        end = stdout.rfind("]")
        if start != -1 and end != -1:
            try:
                data = json.loads(stdout[start:end + 1])
            except json.JSONDecodeError:
                return {"ok": False, "reason": "unparseable"}
        else:
            return {"ok": False, "reason": "unparseable"}
    if not isinstance(data, list):
        return {"ok": False, "reason": "unparseable"}

    pp_ts = tg_ts = pp_stddev = tg_stddev = None
    combined = False
    for row in data:
        if not isinstance(row, dict):
            continue
        try:
            np_ = int(row.get("n_prompt") or 0)
            ng_ = int(row.get("n_gen") or 0)
        except (TypeError, ValueError):
            continue
        ts, sd = _row_ts(row)
        if ts is None:
            continue
        if np_ > 0 and ng_ == 0:
            pp_ts, pp_stddev = ts, sd
        elif ng_ > 0 and np_ == 0:
            tg_ts, tg_stddev = ts, sd
        elif np_ > 0 and ng_ > 0:
            combined = True
            tg_ts, tg_stddev = ts, sd

    if pp_ts is None and tg_ts is None:
        return {"ok": False, "reason": "no_recognizable_rows"}

    result = {"ok": True, "pp_ts": pp_ts, "tg_ts": tg_ts, "combined": combined}
    if pp_stddev is not None:
        result["pp_stddev_ts"] = round(pp_stddev, 3)
    if tg_stddev is not None:
        result["tg_stddev_ts"] = round(tg_stddev, 3)
    return result


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

    bench_output = (proc.stdout or "") + (proc.stderr or "")
    lowered = bench_output.lower()
    # Marker first, exit code second: a crash whose stderr mentions an
    # allocation is still an OOM, but plain nonzero exit (port clash, DLL
    # load failure, segfault) is NOT "doesn't fit" - conflating the two made
    # the MoE sweep back off its offload permanently on any transient error.
    if any(marker in lowered for marker in OOM_MARKERS):
        return {"ok": False, "reason": "oom", "raw": bench_output[-2000:]}
    if proc.returncode != 0:
        return {"ok": False, "reason": "crash", "raw": bench_output[-2000:]}

    parsed = parse_bench_json(proc.stdout)
    if parsed["ok"]:
        return parsed
    return {**parsed, "raw": proc.stdout[-2000:]}

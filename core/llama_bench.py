"""
core/llama_bench.py - Run one llama-bench pass and parse its JSON (used by autotune.py), and
llama-batched-bench multi-user sweeps (CLI: python -m core.llama_bench --batched -m MODEL ...).

llama-bench's JSON field names have changed across llama.cpp releases; on a parse
failure the raw output is printed so the field mapping here is quick to fix.
"""

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from core.backend import build_child_env
from core.vram.devices import resolve_device_ids


# Known-good as of the llama.cpp docs/issues consulted when this was written
# (docs/multi-gpu.md, docs/build.md, llama-bench manpage). If your build's
# `llama-bench --help` differs, edit these - they're the only place CLI
# flag names live.
FLAG_MODEL = "-m"
FLAG_DEVICE = "-dev"
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
    "ze_result_error_out_of_device_memory",
    "ur_result_error_out_of_resources",
    "pi_error_out_of_resources",
)


def build_gpu_env(profile: dict) -> dict:
    # The command also receives exact -dev IDs. Do not set visibility filters:
    # CUDA-style filters can renumber devices and break tensor-split ordering.
    return build_child_env(
        profile.get("backend", "vulkan"),
        runtime_env=profile.get("runtime_env"),
        device_indices=profile.get("gpu_devices"),
        set_visibility=False,
    )


def bench_device_ids(profile: dict) -> list[str]:
    return resolve_device_ids(
        profile.get("gpu_devices") or [],
        backend=profile.get("backend", "vulkan"),
        llama_bin_dir=profile.get("llama_bin_dir"),
        runtime_env=profile.get("runtime_env"),
        strict=False,
    )


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
              env: dict | None = None, device_ids: list[str] | None = None):
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
    if device_ids:
        cmd[3:3] = [FLAG_DEVICE, ",".join(device_ids)]
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


# --------------------------------------------------------------------------------------------
# llama-batched-bench: N parallel sequences in one context, the multi-user serving shape.
#
# Flags verified against `llama-batched-bench --help` of build b11429 (commit d81235049).
# Unlike llama-bench, batched-bench uses the common argument parser, so --tensor-split takes
# llama-server's comma form, not llama-bench's '/' form.
# --------------------------------------------------------------------------------------------

BATCHED_FLAG_CTX = "-c"
BATCHED_FLAG_NPL = "-npl"
BATCHED_FLAG_NPP = "-npp"
BATCHED_FLAG_NTG = "-ntg"
BATCHED_FLAG_PPS = "-pps"
BATCHED_FLAG_KVU = "-kvu"
BATCHED_FLAG_CTK = "-ctk"
BATCHED_FLAG_CTV = "-ctv"
BATCHED_FLAG_BATCH = "-b"
BATCHED_FLAG_UBATCH = "-ub"
BATCHED_FLAG_OUTPUT = "--output-format"   # {md,jsonl}; older builds only print the md table


def find_llama_batched_bench(bin_dir: str) -> Path:
    bin_dir_path = Path(bin_dir)
    for c in (bin_dir_path / "llama-batched-bench.exe", bin_dir_path / "llama-batched-bench"):
        if c.exists():
            return c
    found = shutil.which("llama-batched-bench")
    if found:
        return Path(found)
    sys.exit(
        f"Couldn't find llama-batched-bench(.exe) in {bin_dir} or on PATH.\n"
        f"Check the runtime's 'llama_bin_dir' points at a llama.cpp build that ships the tool."
    )


def _csv(values) -> str:
    if isinstance(values, (list, tuple)):
        return ",".join(str(int(v)) for v in values)
    return str(values)


def build_batched_cmd(bench_path: Path, model_path: str, *, npl, npp, ntg, ctx: int,
                      ngl: int | None = None, device_ids: list[str] | None = None,
                      split_mode: str | None = None, tensor_split: str | None = None,
                      cache_type_k: str | None = None, cache_type_v: str | None = None,
                      flash_attn: str | None = None, kv_unified: bool = False,
                      prompt_shared: bool = False, batch: int | None = None,
                      ubatch: int | None = None, extra_args: list[str] | None = None,
                      output_format: str | None = "jsonl") -> list[str]:
    """The llama-batched-bench command line. Pure, so tests can pin the flags."""
    cmd = [str(bench_path), FLAG_MODEL, model_path, BATCHED_FLAG_CTX, str(int(ctx)),
           BATCHED_FLAG_NPP, _csv(npp), BATCHED_FLAG_NTG, _csv(ntg), BATCHED_FLAG_NPL, _csv(npl)]
    if ngl is not None:
        cmd += [FLAG_NGL, str(ngl)]
    if device_ids:
        cmd += [FLAG_DEVICE, ",".join(device_ids)]
    if split_mode:
        cmd += [FLAG_SPLIT_MODE, split_mode]
    if tensor_split:
        cmd += [FLAG_TENSOR_SPLIT, tensor_split.replace("/", ",")]
    if cache_type_k:
        cmd += [BATCHED_FLAG_CTK, cache_type_k]
    if cache_type_v:
        cmd += [BATCHED_FLAG_CTV, cache_type_v]
    if flash_attn:
        cmd += [FLAG_FLASH_ATTN, flash_attn]
    if batch:
        cmd += [BATCHED_FLAG_BATCH, str(batch)]
    if ubatch:
        cmd += [BATCHED_FLAG_UBATCH, str(ubatch)]
    if kv_unified:
        cmd.append(BATCHED_FLAG_KVU)
    if prompt_shared:
        cmd.append(BATCHED_FLAG_PPS)
    if extra_args:
        cmd += list(extra_args)
    if output_format:
        cmd += [BATCHED_FLAG_OUTPUT, output_format]
    return cmd


def _batched_row(pp, tg, pl, n_kv, t_pp, s_pp, t_tg, s_tg, t, s, pps=None) -> dict:
    pl = int(pl)
    s_tg = float(s_tg)
    return {
        "npp": int(pp), "ntg": int(tg), "npl": pl, "n_kv": int(n_kv),
        "prefill_tps": round(float(s_pp), 2),
        "decode_tps_total": round(s_tg, 2),
        # every sequence decodes in the same batch, so one user's speed is the total / npl
        "decode_tps_per_user": round(s_tg / pl, 2) if pl > 0 else None,
        "t_pp_s": round(float(t_pp), 3), "t_tg_s": round(float(t_tg), 3),
        "t_total_s": round(float(t), 3), "total_tps": round(float(s), 2),
        "prompt_shared": None if pps is None else bool(pps),
    }


def parse_batched_jsonl(text: str) -> list[dict]:
    """Rows from `--output-format jsonl`: one JSON object per line among the load logs."""
    rows = []
    for line in (text or "").splitlines():
        line = line.strip()
        if not (line.startswith("{") and line.endswith("}")):
            continue
        try:
            d = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(d, dict) or "speed_tg" not in d or "pl" not in d:
            continue
        try:
            rows.append(_batched_row(d["pp"], d["tg"], d["pl"], d.get("n_kv", 0), d["t_pp"],
                                     d["speed_pp"], d["t_tg"], d["speed_tg"], d["t"], d["speed"],
                                     d.get("is_pp_shared")))
        except (KeyError, TypeError, ValueError):
            continue
    return rows


_MD_COLUMNS = {"pp": "PP", "tg": "TG", "pl": "B", "n_kv": "N_KV", "t_pp": "T_PP s",
               "s_pp": "S_PP t/s", "t_tg": "T_TG s", "s_tg": "S_TG t/s", "t": "T s", "s": "S t/s"}


def parse_batched_markdown(text: str) -> list[dict]:
    """Rows from the default markdown table:
    |    PP |     TG |    B |   N_KV |   T_PP s | S_PP t/s |   T_TG s | S_TG t/s |      T s |    S t/s |"""
    rows, index = [], None
    for line in (text or "").splitlines():
        line = line.strip()
        if not line.startswith("|"):
            continue
        cells = [c.strip() for c in line.strip("|").split("|")]
        if index is None:
            if "S_TG t/s" in cells and "B" in cells:
                index = {k: cells.index(v) for k, v in _MD_COLUMNS.items() if v in cells}
            continue
        if all(set(c) <= set("-: ") for c in cells):
            continue
        try:
            v = {k: cells[i] for k, i in index.items()}
            rows.append(_batched_row(v["pp"], v["tg"], v["pl"], v.get("n_kv", 0), v["t_pp"],
                                     v["s_pp"], v["t_tg"], v["s_tg"], v["t"], v["s"]))
        except (KeyError, IndexError, TypeError, ValueError):
            continue
    return rows


def parse_batched_output(text: str) -> dict:
    rows = parse_batched_jsonl(text)
    fmt = "jsonl"
    if not rows:
        rows, fmt = parse_batched_markdown(text), "md"
    if not rows:
        return {"ok": False, "reason": "no_recognizable_rows"}
    return {"ok": True, "format": fmt, "rows": rows}


def _flag_unsupported(output: str, flag: str) -> bool:
    low = output.lower()
    return flag in output and ("invalid argument" in low or "unknown argument" in low
                               or "error: " in low and "usage" in low)


def run_batched_bench(bench_path: Path, model_path: str, *, npl=(1, 4, 8, 15), npp=(512,),
                      ntg=(128,), ctx: int = 0, ngl: int | None = 999,
                      device_ids: list[str] | None = None, split_mode: str | None = None,
                      tensor_split: str | None = None, cache_type_k: str | None = None,
                      cache_type_v: str | None = None, flash_attn: str | None = None,
                      kv_unified: bool = False, prompt_shared: bool = False,
                      batch: int | None = None, ubatch: int | None = None,
                      extra_args: list[str] | None = None, timeout: int = 3600,
                      env: dict | None = None) -> dict:
    """One llama-batched-bench sweep. Returns {"ok": True, "rows": [...], "format": ...} with a
    row per (npp, ntg, npl): prefill_tps, decode_tps_total, decode_tps_per_user, ...; or
    {"ok": False, "reason": "oom" | "crash" | "timeout" | "no_recognizable_rows", "raw": tail}.

    ctx=0 sizes the context for the largest case: max(npl) * (max(npp) + max(ntg)), which is what
    batched-bench needs when each sequence keeps its own prompt (no -pps)."""
    if not ctx:
        def _mx(v):
            return max(v) if isinstance(v, (list, tuple)) else int(v)
        ctx = _mx(npl) * (_mx(npp) + _mx(ntg))
        if prompt_shared:
            ctx = _mx(npp) + _mx(npl) * _mx(ntg)
    kw = dict(npl=npl, npp=npp, ntg=ntg, ctx=ctx, ngl=ngl, device_ids=device_ids,
              split_mode=split_mode, tensor_split=tensor_split, cache_type_k=cache_type_k,
              cache_type_v=cache_type_v, flash_attn=flash_attn, kv_unified=kv_unified,
              prompt_shared=prompt_shared, batch=batch, ubatch=ubatch, extra_args=extra_args)

    def _once(output_format):
        cmd = build_batched_cmd(bench_path, model_path, output_format=output_format, **kw)
        print(f"  $ {' '.join(cmd)}")
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, env=env)
        return proc, (proc.stdout or "") + (proc.stderr or "")

    try:
        proc, output = _once("jsonl")
        if proc.returncode != 0 and _flag_unsupported(output, BATCHED_FLAG_OUTPUT):
            proc, output = _once(None)     # build predates --output-format: parse the md table
    except subprocess.TimeoutExpired:
        return {"ok": False, "reason": "timeout", "raw": ""}

    if any(marker in output.lower() for marker in OOM_MARKERS):
        return {"ok": False, "reason": "oom", "raw": output[-2000:]}
    if proc.returncode != 0:
        return {"ok": False, "reason": "crash", "raw": output[-2000:]}
    parsed = parse_batched_output(output)
    if parsed["ok"]:
        parsed["ctx"] = ctx
        return parsed
    return {**parsed, "raw": output[-2000:]}


def format_batched_table(rows: list[dict]) -> str:
    head = f"{'npp':>7} {'ntg':>5} {'users':>5} {'prefill t/s':>12} {'decode t/s':>11} {'per user':>9} {'total s':>8}"
    out = [head, "-" * len(head)]
    for r in rows:
        per = r["decode_tps_per_user"]
        out.append(f"{r['npp']:>7} {r['ntg']:>5} {r['npl']:>5} {r['prefill_tps']:>12.1f} "
                   f"{r['decode_tps_total']:>11.1f} {(per if per is not None else 0):>9.1f} {r['t_total_s']:>8.1f}")
    return "\n".join(out)


def _int_list(s: str) -> list[int]:
    return [int(x) for x in s.replace("/", ",").split(",") if x.strip()]


def main(argv=None) -> int:
    import argparse
    ap = argparse.ArgumentParser(
        prog="python -m core.llama_bench", allow_abbrev=False,
        description="Multi-user throughput sweep with llama-batched-bench (one context, N parallel "
                    "sequences). Uses the active runtime's bin dir, backend env and devices unless "
                    "overridden. Pass flags the build knows but this tool does not as "
                    "--extra-arg=--flag (repeatable).")
    ap.add_argument("--batched", action="store_true", required=True,
                    help="run llama-batched-bench (the only mode of this CLI; plain llama-bench runs via autotune.py)")
    ap.add_argument("-m", "--model", required=True, help="GGUF model path")
    ap.add_argument("--profile", help="profile JSON; its runtime fields are forced to the active runtime like autotune")
    ap.add_argument("--bin-dir", help="llama.cpp bin dir (default: the active runtime's)")
    ap.add_argument("-npl", "--npl", default="1,4,8,15", help="parallel sequences (users), comma list")
    ap.add_argument("-npp", "--npp", default="512", help="prompt tokens per sequence, comma list")
    ap.add_argument("-ntg", "--ntg", default="128", help="generated tokens per sequence, comma list")
    ap.add_argument("-c", "--ctx", type=int, default=0, help="context size (0 = enough for the largest case)")
    ap.add_argument("-ngl", "--ngl", type=int, default=999)
    ap.add_argument("-dev", "--dev", help="exact device ids, comma list (default: the runtime's gpu_devices)")
    ap.add_argument("-sm", "--split-mode", help="none|layer|row|tensor")
    ap.add_argument("-ts", "--tensor-split", help="e.g. 1,1")
    ap.add_argument("-ctk", "--cache-type-k")
    ap.add_argument("-ctv", "--cache-type-v")
    ap.add_argument("-fa", "--flash-attn", help="on|off|auto")
    ap.add_argument("-kvu", "--kv-unified", action="store_true", help="one KV pool shared by all sequences")
    ap.add_argument("-pps", "--prompt-shared", action="store_true", help="all sequences share one prompt")
    ap.add_argument("-b", "--batch", type=int)
    ap.add_argument("-ub", "--ubatch", type=int)
    ap.add_argument("--extra-arg", action="append", default=[], metavar="ARG",
                    help="raw argument passed through, e.g. --extra-arg=--kv-unified-per-slot --extra-arg=131072")
    ap.add_argument("--timeout", type=int, default=3600, help="seconds for the whole sweep")
    ap.add_argument("--json", action="store_true", help="print the result as JSON instead of a table")
    args = ap.parse_args(argv)

    from core.config import apply_runtime   # loads the app config: only after --help is handled
    profile = json.loads(Path(args.profile).read_text()) if args.profile else {}
    profile = apply_runtime(profile)
    bench = find_llama_batched_bench(args.bin_dir or profile["llama_bin_dir"])
    env = build_gpu_env(profile)
    device_ids = [d for d in args.dev.split(",") if d] if args.dev else (bench_device_ids(profile) or None)

    result = run_batched_bench(
        bench, args.model, npl=_int_list(args.npl), npp=_int_list(args.npp), ntg=_int_list(args.ntg),
        ctx=args.ctx, ngl=args.ngl, device_ids=device_ids, split_mode=args.split_mode,
        tensor_split=args.tensor_split, cache_type_k=args.cache_type_k, cache_type_v=args.cache_type_v,
        flash_attn=args.flash_attn, kv_unified=args.kv_unified, prompt_shared=args.prompt_shared,
        batch=args.batch, ubatch=args.ubatch, extra_args=args.extra_arg, timeout=args.timeout, env=env)
    if args.json:
        print(json.dumps(result, indent=2))
    elif result["ok"]:
        print(f"\nllama-batched-bench  ctx={result['ctx']}  ({result['format']} output)")
        print(format_batched_table(result["rows"]))
    else:
        print(f"\nFAILED: {result['reason']}\n{result.get('raw', '')}", file=sys.stderr)
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())

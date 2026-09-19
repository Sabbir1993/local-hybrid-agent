"""
core/vram.py - Preflight VRAM checker for the dual-A770 Vulkan runtime.

Estimates how much VRAM a llama-server launch will need *before* spawning it,
compares that against what is actually free on each Vulkan device, and - when
it won't fit - suggests concrete fixes (unload a co-tenant model, rebalance
tensor-split, reduce -ngl, shrink KV cache type, lower context) instead of
letting a WDDM OOM spill thrash/hang the whole desktop.

How it measures:
  * `llama-bench --list-devices` reports per-Vulkan-index total/free VRAM from
    the Vulkan memory budget (VK_EXT_memory_budget), which already accounts
    for every other process' allocations - the small/orchestrator models, the
    desktop, and the other llama-server instance. Device order matches the
    `-dev VulkanN` indices llama-server uses.
  * Model weights size comes from the GGUF file itself; the KV cache is
    derived from the GGUF header (n_layer, n_head_kv, head_dim) x context x
    kv-type bytes-per-element; a small fixed compute-buffer estimate and a
    per-GPU headroom reserve are added on top.

Failure policy comes from config/app.json:
    "preflight": {"mode": "block" | "warn" | "off", "headroom_gb": 1.0}
  block = refuse to launch when it won't fit (default)
  warn  = log the projection, launch anyway
  off   = skip the check entirely
"""

from __future__ import annotations

import json
import re
import struct
import subprocess
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

from .config import BASE_DIR, CONFIG_DEFAULTS, CONFIG_FILE

GB = 1024 ** 3
MIB = 1024 * 1024

# Bytes per KV element for each kv_cache_type (quant types incl. block scale).
_KV_BYTES_PER_ELEM = {"f16": 2.0, "bf16": 2.0, "f32": 4.0,
                      "q8_0": 34 / 32, "q5_0": 22 / 32, "q4_0": 18 / 32}

_DEFAULT_HEADROOM_GB = 1.0
_PREFLIGHT_MODES = ("block", "warn", "off")

# If free VRAM drops below this on any target device while llama-server is
# still loading, the allocation is about to spill into shared memory (WDDM
# thrash/hang) -> abort the load. Used by state._wait_healthy.
VRAM_WALL_FREE_B = 256 * MIB

_DEV_TTL_S = 10.0
_dev_cache: dict = {"ts": 0.0, "data": []}
_dev_lock = threading.Lock()

_gguf_cache: dict = {}          # path -> ((mtime_ns, size), info)
_gguf_lock = threading.Lock()


class PreflightError(RuntimeError):
    """Raised when a launch won't fit in VRAM and preflight mode is 'block'."""

    def __init__(self, message: str, plan: dict):
        super().__init__(message)
        self.plan = plan


def _preflight_cfg() -> dict:
    cfg = {"mode": "block", "headroom_gb": _DEFAULT_HEADROOM_GB}
    try:
        d = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
        p = d.get("preflight", {})
        if isinstance(p, dict):
            if p.get("mode") in _PREFLIGHT_MODES:
                cfg["mode"] = p["mode"]
            try:
                cfg["headroom_gb"] = max(0.0, min(4.0, float(
                    p.get("headroom_gb", cfg["headroom_gb"]))))
            except (TypeError, ValueError):
                pass
    except Exception:
        pass
    return cfg


# --------------------------------------------------------------------------
# Per-device free/total VRAM (llama-bench --list-devices -> VK_EXT memory budget)
# --------------------------------------------------------------------------

_LIST_DEV_RE = re.compile(
    r"^\s*Vulkan(\d+):\s*(.+?)\s*\(\s*([\d.]+)\s*(MiB|GiB)"
    r"(?:\s*,\s*([\d.]+)\s*(MiB|GiB)\s+free)?\s*\)\s*$")


def _run_list_devices(bin_dir) -> list:
    base = Path(bin_dir or CONFIG_DEFAULTS["llama_bin_dir"])
    bench = None
    for name in ("llama-bench.exe", "llama-bench"):
        cand = base / name
        if cand.exists():
            bench = cand
            break
    if bench is None:
        return []
    try:
        out = subprocess.run([str(bench), "--list-devices"], capture_output=True,
                             text=True, errors="replace", timeout=25)
    except Exception as e:
        print(f"[vram] llama-bench --list-devices failed: {e}", file=sys.stderr)
        return []
    devs = []
    for line in (out.stdout or "").splitlines():
        m = _LIST_DEV_RE.match(line)
        if not m:
            continue
        idx = int(m.group(1))
        name = m.group(2).strip()
        total_b = int(float(m.group(3)) * (MIB if m.group(4) == "MiB" else GB))
        free_b = total_b
        if m.group(5):
            free_b = int(float(m.group(5)) * (MIB if m.group(6) == "MiB" else GB))
        free_b = max(0, min(free_b, total_b))
        devs.append({"index": idx, "name": name, "total_b": total_b,
                     "free_b": free_b, "used_b": total_b - free_b})
    return devs


def query_devices(llama_bin_dir=None, force: bool = False) -> list:
    """[{index, name, total_b, free_b, used_b}] per Vulkan device; 10s cache."""
    with _dev_lock:
        now = time.time()
        if (not force and _dev_cache["data"]
                and now - _dev_cache["ts"] < _DEV_TTL_S):
            return _dev_cache["data"]
        devs = _run_list_devices(llama_bin_dir)
        if devs:
            _dev_cache["ts"] = now
            _dev_cache["data"] = devs
        return _dev_cache["data"]  # stale fallback if the fresh query failed

# --------------------------------------------------------------------------
# Minimal GGUF header parser (weights size + KV geometry)
# --------------------------------------------------------------------------

_GGUF_FMT = {0: ("<B", 1), 1: ("<b", 1), 2: ("<H", 2), 3: ("<h", 2),
             4: ("<I", 4), 5: ("<i", 4), 6: ("<f", 4), 7: ("<B", 1),
             10: ("<Q", 8), 11: ("<q", 8), 12: ("<d", 8)}

_SHARD_RE = re.compile(r"-(\d{5})-of-(\d{5})\.gguf$", re.I)


class _Cursor:
    """Incremental reader over the first few MB of a (possibly huge) GGUF."""

    def __init__(self, fh, chunk=4 * MIB):
        self.fh = fh
        self.buf = b""
        self.pos = 0
        self.chunk = chunk

    def _need(self, n: int) -> bool:
        if len(self.buf) - self.pos >= n:
            return True
        while len(self.buf) - self.pos < n:
            data = self.fh.read(max(self.chunk, n - (len(self.buf) - self.pos)))
            if not data:
                return False
            self.buf = self.buf[self.pos:] + data
            self.pos = 0
        return True

    def read(self, n: int) -> bytes:
        if not self._need(n):
            raise EOFError("unexpected EOF inside GGUF header")
        out = self.buf[self.pos:self.pos + n]
        self.pos += n
        return out

    def skip(self, n: int) -> None:
        while n > 0:
            step = min(n, self.chunk)
            if not self._need(step):
                raise EOFError("unexpected EOF inside GGUF header")
            self.pos += step
            n -= step


def _gguf_files(model_path: Path):
    """(total_bytes_of_all_shards, path_of_first_shard) - handles split GGUFs."""
    m = _SHARD_RE.search(model_path.name)
    if m:
        prefix = model_path.name[:m.start()]
        parts = sorted(model_path.parent.glob(
            f"{prefix}-?????-of-{m.group(2)}.gguf"))
        if parts:
            return sum(p.stat().st_size for p in parts), parts[0]
    return model_path.stat().st_size, model_path


def parse_gguf_info(model_path) -> "dict | None":
    """Parse GGUF metadata needed for the VRAM estimate.

    Returns {arch, n_layer, n_head, n_head_kv, n_embd, head_dim, ctx_train,
    n_expert, n_vocab, file_bytes, complete, model_path, error} or None.
    Never raises: partial info degrades to complete=False (low confidence).
    """
    p = Path(model_path)
    if not p.exists():
        return None
    with _gguf_lock:
        try:
            st = p.stat()
        except OSError:
            return None
        cached = _gguf_cache.get(str(p))
        if cached and cached[0] == (st.st_mtime_ns, st.st_size):
            return cached[1]

    parse_err = None
    raw: dict = {}
    try:
        total_b, first = _gguf_files(p)
    except OSError as e:
        return {"arch": "llama", "n_layer": 32, "n_head": 32, "n_head_kv": 8,
                "n_embd": None, "head_dim": 128, "ctx_train": 0, "n_expert": 0,
                "n_vocab": None, "file_bytes": 0, "complete": False,
                "model_path": str(p), "error": str(e)}
    try:
        with open(first, "rb") as fh:
            cur = _Cursor(fh)
            if cur.read(4) != b"GGUF":
                raise ValueError("not a GGUF file")
            version = struct.unpack("<I", cur.read(4))[0]
            if version < 2:
                raise ValueError(f"unsupported GGUF version {version}")
            cur.read(8)  # tensor count (not needed)
            kv_count = struct.unpack("<Q", cur.read(8))[0]
            if kv_count > 100_000:
                raise ValueError("implausible GGUF kv_count")
            for _ in range(kv_count):
                klen = struct.unpack("<Q", cur.read(8))[0]
                key = cur.read(klen).decode("utf-8", "replace")
                vtype = struct.unpack("<I", cur.read(4))[0]
                if vtype == 8:  # string
                    ln = struct.unpack("<Q", cur.read(8))[0]
                    if key == "general.architecture":
                        raw[key] = cur.read(ln).decode("utf-8", "replace")
                    else:
                        cur.skip(ln)
                elif vtype == 9:  # array
                    et = struct.unpack("<I", cur.read(4))[0]
                    cnt = struct.unpack("<Q", cur.read(8))[0]
                    if key == "tokenizer.ggml.tokens":
                        raw["n_vocab"] = cnt
                    if et == 8:
                        for _ in range(cnt):
                            ln = struct.unpack("<Q", cur.read(8))[0]
                            cur.skip(ln)
                    elif et in _GGUF_FMT:
                        _, sz = _GGUF_FMT[et]
                        if 0 < cnt <= 1024:
                            raw[key] = [struct.unpack(_GGUF_FMT[et][0],
                                                      cur.read(sz))[0]
                                        for _ in range(cnt)]
                        else:
                            cur.skip(sz * cnt)
                    else:
                        raise ValueError(f"bad array elem type {et}")
                elif vtype in _GGUF_FMT:
                    fmt, sz = _GGUF_FMT[vtype]
                    raw[key] = struct.unpack(fmt, cur.read(sz))[0]
                else:
                    raise ValueError(f"unknown GGUF kv type {vtype}")
                arch = raw.get("general.architecture")
                if arch:
                    needed = (f"{arch}.block_count",
                              f"{arch}.attention.head_count",
                              f"{arch}.attention.head_count_kv",
                              f"{arch}.embedding_length",
                              f"{arch}.attention.key_length",
                              f"{arch}.context_length",
                              f"{arch}.expert_count")
                    if all(k in raw for k in needed):
                        break  # got the geometry; skip tokenizer blobs
    except Exception as e:
        parse_err = str(e)

    arch = raw.get("general.architecture") or "llama"

    def scalar(*keys, default=None):
        for k in keys:
            if k in raw:
                v = raw[k]
                if isinstance(v, list):
                    return max(v) if v else default  # per-layer arrays -> worst case
                return v
        return default

    n_layer = scalar(f"{arch}.block_count")
    n_head = scalar(f"{arch}.attention.head_count")
    n_kv = scalar(f"{arch}.attention.head_count_kv")
    n_embd = scalar(f"{arch}.embedding_length")
    head_dim = scalar(f"{arch}.attention.key_length",
                      f"{arch}.attention.value_length")
    complete = True
    if head_dim is None and n_embd and n_head:
        head_dim = int(n_embd) // int(n_head)
    if head_dim is None:
        head_dim, complete = 128, False
    if n_kv is None:
        n_kv = n_head
        if n_kv is None:
            n_kv, complete = 8, False
    if n_layer is None:
        n_layer, complete = 32, False
    ctx_train = scalar(f"{arch}.context_length", default=0)

    info = {"arch": arch,
            "n_layer": int(n_layer), "n_head": int(n_head) if n_head else None,
            "n_head_kv": int(n_kv), "n_embd": int(n_embd) if n_embd else None,
            "head_dim": int(head_dim), "ctx_train": int(ctx_train or 0),
            "n_expert": int(scalar(f"{arch}.expert_count", default=0) or 0),
            "n_vocab": raw.get("n_vocab"), "file_bytes": total_b,
            "complete": complete, "model_path": str(p), "error": parse_err}
    with _gguf_lock:
        _gguf_cache[str(p)] = ((st.st_mtime_ns, st.st_size), info)
    return info

# --------------------------------------------------------------------------
# Footprint estimation + effective launch params
# --------------------------------------------------------------------------

def estimate_footprint(info: dict, ctx: int, kv_type: str = "f16",
                       ubatch: int = 512, flash_attn: str = "auto",
                       headroom_gb: float | None = None) -> dict:
    """bytes for {weights, kv_total, compute, headroom} + confidence label."""
    cfg = _preflight_cfg()
    if headroom_gb is None:
        headroom_gb = cfg["headroom_gb"]
    weights_b = int(info["file_bytes"] * 1.01)          # + ~1% meta/tensors slack
    kv_pe = _KV_BYTES_PER_ELEM.get(kv_type, 2.0)
    kv_per_token_b = info["n_layer"] * info["n_head_kv"] * info["head_dim"] * 2 * kv_pe
    kv_total_b = int(kv_per_token_b * max(0, int(ctx)))
    compute_b = int(0.30 * GB + 2048 * 8 * max(0, int(ubatch or 512)))
    if str(flash_attn).lower() == "off":
        compute_b += int(0.25 * GB)
    return {"weights_b": weights_b, "kv_per_token_b": int(kv_per_token_b),
            "kv_total_b": kv_total_b, "compute_b": compute_b,
            "headroom_b": int(headroom_gb * GB),
            "confidence": "high" if info.get("complete") else "low"}


def effective_params(profile: dict, overrides: dict | None = None) -> dict:
    """Resolve launch params like build_launch_command does
    (override > tuned > top-level > CONFIG_DEFAULTS, incl. the zero-share rule)."""
    ov = overrides or {}
    tuned = profile.get("tuned") or {}

    def pick(key, default):
        v = ov.get(key)
        if v is not None:
            return v
        v = tuned.get(key)
        if v is not None:
            return v
        v = profile.get(key)
        if v is not None:
            return v
        return default

    n_gpu_layers = int(pick("n_gpu_layers", CONFIG_DEFAULTS["n_gpu_layers"]))
    tensor_split = str(pick("tensor_split", CONFIG_DEFAULTS["tensor_split"]))
    context_size = int(pick("context_size", CONFIG_DEFAULTS["context_size"]))
    kv_cache_type = str(pick("kv_cache_type", CONFIG_DEFAULTS["kv_cache_type"]))
    flash_attn = str(pick("flash_attn", CONFIG_DEFAULTS["flash_attn"]))
    ubatch = int(pick("ubatch_size", 0) or CONFIG_DEFAULTS["ubatch_size"])
    gpu_devices = list(ov.get("gpu_devices")
                       or profile.get("gpu_devices")
                       or CONFIG_DEFAULTS["gpu_devices"])
    split_mode = str(pick("split_mode", CONFIG_DEFAULTS["split_mode"]))
    model_path = profile.get("model_path")
    llama_bin_dir = profile.get("llama_bin_dir") or CONFIG_DEFAULTS["llama_bin_dir"]

    # zero-share rule (mirrors build_launch_command): "0,1" -> GPU 2 only
    shares = [s.strip() for s in tensor_split.split(",") if s.strip().isdigit()]
    if len(shares) == len(gpu_devices) and len(shares) > 1:
        pairs = [(d, int(s)) for d, s in zip(gpu_devices, shares) if int(s) > 0]
        if pairs:
            gpu_devices = [d for d, _ in pairs]
            tensor_split = ",".join(str(s) for _, s in pairs)

    draft_b = 0
    if profile.get("mtp_enabled") and profile.get("mtp_draft_path"):
        dp = Path(str(profile["mtp_draft_path"]))
        if dp.exists():
            try:
                draft_b = dp.stat().st_size
            except OSError:
                pass

    return {"n_gpu_layers": n_gpu_layers, "tensor_split": tensor_split,
            "context_size": context_size, "kv_cache_type": kv_cache_type,
            "flash_attn": flash_attn, "ubatch_size": ubatch,
            "gpu_devices": gpu_devices, "split_mode": split_mode,
            "model_path": model_path, "llama_bin_dir": llama_bin_dir,
            "draft_b": draft_b}

def _shares_for(target_devs: list, tensor_split: str) -> list:
    """Normalized per-GPU share of the split (equal when unparseable)."""
    n = max(1, len(target_devs))
    ints = [int(s) for s in str(tensor_split).split(",") if s.strip().isdigit()]
    vec = [float(v) for v in ints] if (len(ints) == n and any(ints)) else [1.0] * n
    tot = sum(vec) or 1.0
    return [v / tot for v in vec]


def _eval_fit(target_devs: list, s_norm: list, weights_b: int, units: int,
              ngl: int, kv_total_b: int, compute_b: int, headroom_b: int):
    """Per-GPU need and margin (free - need) for a given ngl/kv/split."""
    off_frac = min(max(0, int(ngl)), units) / max(1, units)
    off_b = weights_b * off_frac
    needs, margins = [], []
    for d, s in zip(target_devs, s_norm):
        need = off_b * s + kv_total_b * s + compute_b + headroom_b
        needs.append(need)
        margins.append(d["free_b"] - need)
    return needs, margins


def _fits(target_devs, s_norm, weights_b, units, ngl, kv_total_b,
          compute_b, headroom_b) -> bool:
    _, margins = _eval_fit(target_devs, s_norm, weights_b, units, ngl,
                           kv_total_b, compute_b, headroom_b)
    return all(m >= 0 for m in margins)


def _shares_str(s_norm: list, scale: int = 20) -> str:
    ints = [max(1, int(round(s * scale))) for s in s_norm]
    idx = ints.index(max(ints))
    ints[idx] = max(1, ints[idx] + (scale - sum(ints)))
    return ",".join(map(str, ints))

# --------------------------------------------------------------------------
# Suggestions
# --------------------------------------------------------------------------

def _small_models_loaded():
    """[(role, instance)] of currently-running small models (lazy import)."""
    out = []
    try:
        from .small_model import small_models
        for role, inst in small_models.instances.items():
            if inst.is_up() and inst.model_path:
                out.append((role, inst))
    except Exception:
        pass
    return out


def _small_model_est_bytes(inst) -> int:
    info = parse_gguf_info(inst.model_path)
    if info:
        fp = estimate_footprint(info, inst.ctx, "f16", 512, "on", headroom_gb=0.0)
        return fp["weights_b"] + fp["kv_total_b"] + fp["compute_b"]
    try:
        return int(inst.model_path.stat().st_size * 1.01) + int(0.5 * GB)
    except OSError:
        return 0


def _build_suggestions(target_devs, eff, info, fp, s_norm, units):
    """Ordered suggestions with fits_after verdicts for each."""
    sug = []
    weights_b = fp["weights_b"] + eff["draft_b"]
    kv_b = fp["kv_total_b"]
    comp, hr = fp["compute_b"], fp["headroom_b"]
    fit = lambda ngl, kv=kv_b, sh=None, w=weights_b: _fits(
        target_devs, sh or s_norm, w, units, ngl, kv, comp, hr)

    # 1) unload a co-tenant small model on a target card
    for role, inst in _small_models_loaded():
        dev_idx = getattr(inst, "gpu", None)
        freed = _small_model_est_bytes(inst)
        sim = [dict(d) for d in target_devs]
        hit = False
        for d in sim:
            if d["index"] == dev_idx:
                d["free_b"] += freed
                hit = True
        if not hit:
            continue
        _, sim_margins = _eval_fit(sim, s_norm, weights_b, units,
                                   min(eff["n_gpu_layers"], units),
                                   kv_b, comp, hr)
        sug.append({"type": "unload_small_model", "value": role,
                    "frees_gb": round(freed / GB, 2),
                    "fits_after": all(m >= 0 for m in sim_margins),
                    "desc": f"unload the '{role}' small model on Vulkan{dev_idx} "
                            f"(frees ~{freed / GB:.1f} GB)"})

    # 2) KV cache quantization
    if eff["kv_cache_type"] in ("f16", "bf16", "f32"):
        pe = _KV_BYTES_PER_ELEM.get(eff["kv_cache_type"], 2.0)
        kv_q8 = int(kv_b * (34 / 32) / pe)
        if kv_q8 < kv_b:
            sug.append({"type": "kv_cache_type", "value": "q8_0",
                        "saves_gb": round((kv_b - kv_q8) / GB, 2),
                        "fits_after": fit(eff["n_gpu_layers"], kv=kv_q8),
                        "desc": f"kv_cache_type q8_0 (KV {kv_b / GB:.1f} -> "
                                f"{kv_q8 / GB:.1f} GB)"})

    # 3) rebalanced tensor split proportional to free VRAM
    if len(target_devs) > 1:
        avail = [d["free_b"] - comp - hr for d in target_devs]
        if all(a > 0 for a in avail):
            tot = float(sum(avail))
            new_s = [a / tot for a in avail]
            new_split = _shares_str(new_s)
            if new_split != eff["tensor_split"]:
                wkv = weights_b + kv_b
                ok = all(wkv * s <= a for s, a in zip(new_s, avail))
                sug.append({"type": "tensor_split", "value": new_split,
                            "fits_after": ok,
                            "desc": f"tensor_split {new_split} "
                                    f"(proportional to free VRAM)"})

    # 4) reduce GPU offload: max ngl that fits with the current split
    if not fit(eff["n_gpu_layers"]):
        lo, hi, best = 0, units, 0
        while lo <= hi:
            mid = (lo + hi) // 2
            if fit(mid):
                best, lo = mid, mid + 1
            else:
                hi = mid - 1
        if best > 0 and best < units:
            sug.append({"type": "n_gpu_layers", "value": best,
                        "cpu_layers": units - best,
                        "fits_after": True,
                        "desc": f"GPU layers -ngl {best} of {units} "
                                f"({units - best} layer(s) on CPU, slower)"})

    # 5) reduce context so FULL offload fits
    kvcap = None
    for d, s in zip(target_devs, s_norm):
        cap = (d["free_b"] - comp - hr - weights_b * s) / max(s, 1e-6)
        kvcap = cap if kvcap is None else min(kvcap, cap)
    if kvcap and kvcap > 0 and fp["kv_per_token_b"] > 0:
        ctx_new = int(kvcap / fp["kv_per_token_b"])
        ctx_new = max(2048, (ctx_new // 1024) * 1024)
        if ctx_new < eff["context_size"]:
            kv_new = int(fp["kv_per_token_b"] * ctx_new)
            ok = fit(units, kv=kv_new)
            sug.append({"type": "context_size", "value": ctx_new,
                        "fits_after": ok,
                        "desc": f"context_size {ctx_new} "
                                f"(KV {kv_b / GB:.1f} -> {kv_new / GB:.1f} GB, "
                                f"keeps full GPU offload)"})

    # 6) MoE note
    if info.get("n_expert", 0) > 0:
        sug.append({"type": "note",
                    "desc": "MoE model: run `python autotune.py --profile <profile>` "
                            "to sweep --n-cpu-moe (keeps expert FFNs in CPU RAM)"})
    return sug

# --------------------------------------------------------------------------
# Planner + enforcement
# --------------------------------------------------------------------------

def _fmt_gb(b) -> str:
    return f"{b / GB:.1f} GB"


def preflight_mode() -> str:
    return _preflight_cfg()["mode"]


def _human_msg(plan: dict) -> str:
    status = plan["status"]
    name = plan.get("model") or "model"
    if status == "unknown":
        return f"[vram] {name}: VRAM check unavailable - {plan.get('message', '')}"
    est = plan.get("estimate") or {}
    eff = plan.get("effective") or {}
    lines = [f"[vram] {name}: {est.get('total_gb', '?')} GB needed "
             f"(weights {est.get('weights_gb')} + KV@ctx {est.get('kv_gb')} + "
             f"compute {est.get('compute_gb')}) with -ngl {eff.get('n_gpu_layers')}, "
             f"split \"{eff.get('tensor_split')}\", headroom {est.get('headroom_gb')} GB"]
    for d in plan.get("devices", []):
        if d.get("target"):
            if status == "nofit":
                lines.append(f"  Vulkan{d['index']}: need {d['need_gb']} GB, "
                             f"free {d['free_gb']} GB -> short {d['short_gb']} GB")
            else:
                lines.append(f"  Vulkan{d['index']}: need {d['need_gb']} GB, "
                             f"free {d['free_gb']} GB (margin {d['margin_gb']} GB)")
    if status == "nofit" and plan.get("suggestions"):
        lines.append("Suggestions (apply in the Model Config panel, then load again):")
        for i, s in enumerate(plan["suggestions"][:5], 1):
            verdict = "then it fits" if s.get("fits_after") else "helps"
            lines.append(f"  {i}. {s['desc']} -> {verdict}")
    return "\n".join(lines)


def plan_launch(profile: dict, overrides: dict | None = None,
                devices: list | None = None) -> dict:
    """Full preflight plan: verdict, per-device numbers, suggestions, message."""
    cfg = _preflight_cfg()
    plan = {"status": "unknown", "allow": True, "mode": cfg["mode"],
            "model": None, "checked_at": datetime.now(timezone.utc).isoformat(),
            "effective": None, "estimate": None, "devices": [],
            "suggestions": [], "message": ""}
    try:
        eff = effective_params(profile, overrides)
    except Exception as e:
        plan["message"] = f"could not resolve launch params: {e}"
        return plan
    plan["effective"] = {k: eff[k] for k in (
        "n_gpu_layers", "tensor_split", "context_size", "kv_cache_type",
        "flash_attn", "gpu_devices", "ubatch_size")}
    mp = eff["model_path"]
    if not mp or not Path(str(mp)).exists():
        plan["message"] = f"model file missing: {mp}"
        return plan
    plan["model"] = Path(str(mp)).name
    info = parse_gguf_info(mp)
    if info is None:
        plan["message"] = f"could not stat model file: {mp}"
        return plan
    fp = estimate_footprint(info, eff["context_size"], eff["kv_cache_type"],
                            eff["ubatch_size"], eff["flash_attn"])
    weights_b = fp["weights_b"] + eff["draft_b"]
    fp["total_b"] = weights_b + fp["kv_total_b"] + fp["compute_b"]
    plan["estimate"] = {"weights_gb": round(weights_b / GB, 2),
                        "kv_gb": round(fp["kv_total_b"] / GB, 2),
                        "compute_gb": round(fp["compute_b"] / GB, 2),
                        "headroom_gb": round(fp["headroom_b"] / GB, 2),
                        "total_gb": round(fp["total_b"] / GB, 2),
                        "kv_per_token_kb": round(fp["kv_per_token_b"] / 1024, 2),
                        "confidence": fp["confidence"],
                        "layers": info["n_layer"] + 1,
                        "n_expert": info.get("n_expert", 0)}

    devs = devices if devices is not None else query_devices(eff["llama_bin_dir"])
    plan["devices"] = [{"index": d["index"], "name": d["name"],
                        "total_gb": round(d["total_b"] / GB, 2),
                        "free_gb": round(d["free_b"] / GB, 2),
                        "used_gb": round(d["used_b"] / GB, 2),
                        "target": False} for d in devs]
    if not devs:
        plan["message"] = ("no Vulkan device info (llama-bench --list-devices "
                           "missing/failed) - cannot verify VRAM")
        return plan
    tmap = {d["index"]: d for d in devs}
    targets = [tmap.get(i) for i in eff["gpu_devices"]]
    if any(t is None for t in targets):
        plan["message"] = (f"target device(s) {eff['gpu_devices']} not present in "
                           f"Vulkan device list {[d['index'] for d in devs]}")
        return plan
    units = info["n_layer"] + 1
    s_norm = _shares_for(targets, eff["tensor_split"])
    needs, margins = _eval_fit(targets, s_norm, weights_b, units,
                               eff["n_gpu_layers"], fp["kv_total_b"],
                               fp["compute_b"], fp["headroom_b"])
    for t, need, margin in zip(targets, needs, margins):
        for pd in plan["devices"]:
            if pd["index"] == t["index"]:
                pd.update({"target": True, "need_gb": round(need / GB, 2),
                           "free_gb": round(t["free_b"] / GB, 2),
                           "margin_gb": round(margin / GB, 2),
                           "short_gb": round(max(0.0, -margin) / GB, 2)})
    if all(m >= 0 for m in margins):
        tight = min(margins) < 0.08 * max(t["total_b"] for t in targets)
        plan["status"] = "tight" if (tight or fp["confidence"] == "low") else "fit"
    else:
        plan["status"] = "nofit"
        plan["allow"] = cfg["mode"] != "block"
        plan["suggestions"] = _build_suggestions(targets, eff, info, fp, s_norm, units)
    plan["message"] = _human_msg(plan)
    return plan

def check_or_raise(profile: dict) -> dict:
    """Preflight for the main llama-server launch. Raises PreflightError
    when it won't fit and preflight.mode is 'block'; always returns the plan."""
    plan = plan_launch(profile)
    if plan["status"] == "nofit":
        if plan["mode"] == "block":
            raise PreflightError(plan["message"], plan)
        print(plan["message"])
    elif plan["status"] in ("tight", "unknown"):
        print(plan["message"])
    return plan


def _main_model_suggestion(plan: dict, target_idx: int) -> dict | None:
    """'unload the main model' suggestion for small-model preflights."""
    try:
        from .state import state
        prof = state.profile
        if not prof or not (state.process and state.process.poll() is None):
            return None
        sub = plan_launch(prof)
        freed = sum(d.get("need_gb", 0) for d in sub.get("devices", [])
                    if d.get("target"))
        return {"type": "unload_main_model",
                "desc": f"unload the main model ({sub.get('model', '?')}, "
                        f"~{freed:.1f} GB across Vulkan{sub.get('effective', {}).get('gpu_devices')})",
                "fits_after": True}
    except Exception:
        return None


def check_small_model_or_raise(role: str, model_path, ctx: int,
                               vulkan_index: int, mmproj_path=None) -> dict:
    """Preflight for a small-model (orchestrator/vision/embedder) launch on
    a single Vulkan device. Raises PreflightError in 'block' mode."""
    profile = {"model_path": str(model_path), "context_size": int(ctx),
               "n_gpu_layers": 999, "gpu_devices": [int(vulkan_index)],
               "tensor_split": "1", "kv_cache_type": "f16",
               "flash_attn": "on", "ubatch_size": 512,
               "llama_bin_dir": CONFIG_DEFAULTS["llama_bin_dir"]}
    plan = plan_launch(profile)
    if plan["status"] != "nofit":
        if plan["status"] in ("tight", "unknown"):
            print(plan["message"])
        return plan

    eff = plan.get("effective") or {}
    idx = (eff.get("gpu_devices") or [vulkan_index])[0]
    fp = plan.get("estimate") or {}
    extra = []

    # a) unload the main model
    s = _main_model_suggestion(plan, idx)
    if s:
        extra.append(s)

    # b) move this small model to the other discrete GPU with the most free VRAM
    devs = plan.get("devices") or []
    cands = [d for d in devs if d["index"] != idx
             and d.get("total_gb", 0) >= 4.0]
    if cands:
        best = max(cands, key=lambda d: d["free_gb"])
        need = (fp.get("weights_gb", 0) + fp.get("kv_gb", 0)
                + fp.get("compute_gb", 0) + fp.get("headroom_gb", 0))
        extra.append({"type": "config_small_model_gpu", "value": best["index"],
                      "fits_after": best["free_gb"] >= need,
                      "desc": f"set config.json small_models.{role}.gpu = "
                              f"{best['index']} (Vulkan{best['index']} has "
                              f"{best['free_gb']} GB free)"})

    plan["suggestions"] = extra + plan.get("suggestions", [])
    plan["message"] = _human_msg(plan)
    if plan["mode"] == "block":
        raise PreflightError(plan["message"], plan)
    print(plan["message"])
    return plan


def wall_check(target_indices, devices: list) -> "int | None":
    """Called from state._wait_healthy while llama-server is loading.
    Returns the Vulkan index that hit the VRAM wall, or None."""
    if not devices:
        return None
    for d in devices:
        if d["index"] in target_indices and d["free_b"] <= VRAM_WALL_FREE_B:
            return d["index"]
    return None








from pathlib import Path

from ..config import CONFIG_DEFAULTS
from .constants import GB, _KV_BYTES_PER_ELEM, _preflight_cfg
from .gguf_parser import parse_gguf_info


def estimate_footprint(info: dict, ctx: int, kv_type: str = "f16",
                       ubatch: int = 512, flash_attn: str = "auto",
                       headroom_gb: float | None = None) -> dict:
    """bytes for {weights, kv_total, compute, headroom} + confidence label."""
    cfg = _preflight_cfg()
    if headroom_gb is None:
        headroom_gb = cfg["headroom_gb"]
    weights_b = int(info["file_bytes"] * 1.01)
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
    """Resolve launch params like build_launch_command does."""
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

    shares = [s.strip() for s in tensor_split.split(",") if s.strip().isdigit()]
    if len(shares) == len(gpu_devices) and len(shares) > 1:
        pairs = [(d, int(s)) for d, s in zip(gpu_devices, shares) if int(s) > 0]
        if pairs:
            gpu_devices = [d for d, _ in pairs]
            tensor_split = ",".join(str(s) for _, s in pairs)

    draft_b = 0
    for flag, key in (("mtp_enabled", "mtp_draft_path"), ("vision_capable", "mmproj_path")):
        if profile.get(flag) and profile.get(key):
            dp = Path(str(profile[key]))
            if dp.exists():
                try:
                    draft_b += dp.stat().st_size
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
    kv_off_b = kv_total_b * off_frac
    needs, margins = [], []
    for d, s in zip(target_devs, s_norm):
        need = off_b * s + kv_off_b * s + compute_b + headroom_b
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


def compute_tensor_split(devices: list) -> "str | None":
    """Tensor-split string for N GPUs, proportional to each device's free VRAM."""
    n = len(devices)
    if n <= 1:
        return None
    free = [max(1, int(d.get("free_b", d.get("total_b", 1)))) for d in devices]
    tot = float(sum(free))
    s_norm = [f / tot for f in free]
    return _shares_str(s_norm)


def _small_models_loaded():
    """[(role, instance)] of currently-running small models (lazy import)."""
    out = []
    try:
        from ..small_model import small_models
        for role, inst in small_models.instances.items():
            if inst.is_up() and inst.model_path:
                out.append((role, inst))
    except Exception:
        pass
    return out


def _small_model_est_bytes(inst) -> int:
    if hasattr(inst, "est_bytes"):
        return inst.est_bytes()
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

    # 1) unload a co-tenant small model
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

    # 3) rebalanced tensor split
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
                            "desc": f"tensor_split {new_split} (proportional to free VRAM)"})

    # 4) reduce GPU offload
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
                        "desc": f"GPU layers -ngl {best} of {units} ({units - best} layer(s) on CPU, slower)"})

    # 5) reduce context
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
                        "desc": f"context_size {ctx_new} (KV {kv_b / GB:.1f} -> {kv_new / GB:.1f} GB, keeps full GPU offload)"})

    # 6) MoE note
    if info.get("n_expert", 0) > 0:
        sug.append({"type": "note",
                    "desc": "MoE model: run `python autotune.py --profile <profile>` to sweep --n-cpu-moe"})
    return sug

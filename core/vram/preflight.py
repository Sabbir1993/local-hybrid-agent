from datetime import datetime, timezone
from pathlib import Path

from ..config import CONFIG_DEFAULTS
from .constants import GB, PreflightError, _preflight_cfg
from .devices import query_devices
from .estimator import (
    _build_suggestions,
    _eval_fit,
    _shares_for,
    effective_params,
    estimate_footprint,
)
from .gguf_parser import parse_gguf_info


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
    """Preflight for the main llama-server launch."""
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
        from ..state import state
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
    """Preflight for a small-model launch on a single Vulkan device."""
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

    s = _main_model_suggestion(plan, idx)
    if s:
        extra.append(s)

    devs = plan.get("devices") or []
    cands = [d for d in devs if d["index"] != idx and d.get("total_gb", 0) >= 4.0]
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

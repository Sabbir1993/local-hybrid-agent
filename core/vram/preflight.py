from datetime import datetime, timezone
from pathlib import Path

from ..config import CONFIG_DEFAULTS
from .constants import GB, PreflightError, _preflight_cfg
from .devices import DeviceQueryError, query_devices
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


def _devices_or_fail(eff: dict, plan: dict) -> list:
    """Device list for the verdict, or [] with the plan marked not-allowed.

    strict=True is the point of this function: an absent/failed/unparseable
    `llama-bench --list-devices` is "we cannot tell", not "zero devices", and it must not
    produce a verdict the gate will trust. mode="warn"/"off" still lets the load through
    (with a loud message), because a driver hiccup should not brick the box.
    """
    try:
        return query_devices(eff["llama_bin_dir"], strict=True)
    except DeviceQueryError as e:
        plan["status"] = "unknown"
        plan["allow"] = False
        plan["message"] = (f"[vram] {plan.get('model') or 'model'}: cannot verify VRAM - {e} - "
                           "not launching, because a fit that cannot be checked is not a fit.")
        return []


def plan_launch(profile: dict, overrides: dict | None = None,
                devices: list | None = None) -> dict:
    """Full preflight plan: verdict, per-device numbers, suggestions, message.

    Fail-closed contract: every path that returns without a verdict sets allow=False, because
    "we could not size this" is not the same as "this fits". check_or_raise enforces allow.
    """
    cfg = _preflight_cfg()
    plan = {"status": "unknown", "allow": True, "mode": cfg["mode"],
            "model": None, "checked_at": datetime.now(timezone.utc).isoformat(),
            "effective": None, "estimate": None, "devices": [],
            "suggestions": [], "message": ""}
    try:
        eff = effective_params(profile, overrides)
    except Exception as e:
        plan["allow"] = False
        plan["message"] = (f"[vram] could not resolve launch params: {e} - "
                           "not launching, because an unsized launch cannot be checked.")
        return plan
    plan["effective"] = {k: eff[k] for k in (
        "n_gpu_layers", "tensor_split", "context_size", "kv_cache_type",
        "flash_attn", "gpu_devices", "ubatch_size")}
    mp = eff["model_path"]
    if not mp or not Path(str(mp)).exists():
        plan["allow"] = False
        plan["message"] = f"[vram] model file missing: {mp} - not launching."
        return plan
    plan["model"] = Path(str(mp)).name
    info = parse_gguf_info(mp)
    if info is None:
        plan["allow"] = False
        plan["message"] = (f"[vram] could not stat model file: {mp} - "
                           "not launching, because an unreadable model cannot be sized.")
        return plan
    if info.get("stat_ok") is False:
        # The file exists but could not be read. The estimate would come out near zero and the
        # verdict "fit", which is the opposite of what an unreadable model file means.
        plan["status"] = "unknown"
        plan["allow"] = False
        plan["message"] = (f"could not read model file: {mp} ({info.get('error') or 'unknown error'}) - "
                           "not launching, because an unreadable model cannot be sized.")
        return plan
    fp = estimate_footprint(info, eff["context_size"], eff["kv_cache_type"],
                            eff["ubatch_size"], eff["flash_attn"],
                            n_cpu_moe=eff.get("n_cpu_moe", 0),
                            n_expert_used=info.get("n_expert") or 0)
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

    devs = devices if devices is not None else _devices_or_fail(eff, plan)
    if not devs:
        return plan
    plan["devices"] = [{"index": d["index"], "name": d["name"],
                        "total_gb": round(d["total_b"] / GB, 2),
                        "free_gb": round(d["free_b"] / GB, 2),
                        "used_gb": round(d["used_b"] / GB, 2),
                        "target": False} for d in devs]
    tmap = {d["index"]: d for d in devs}
    targets = [tmap.get(i) for i in eff["gpu_devices"]]
    if any(t is None for t in targets):
        plan["allow"] = False
        plan["message"] = (f"[vram] {plan['model']}: target device(s) {eff['gpu_devices']} not "
                           f"present in Vulkan device list {[d['index'] for d in devs]} - "
                           "not launching; fix the device index or the iGPU skip list.")
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


def enforce(plan: dict) -> dict:
    """The single launch gate. Enforces plan["allow"], not plan["status"].

    This used to test `status == "nofit"`, which is why three of the four ways plan_launch
    can fail to produce a verdict (unreadable model, unresolvable params, absent or
    unparseable device list) printed a message and launched anyway. plan["allow"] is set
    False by every one of those paths; nothing read it until here. mode="warn"/"off" still
    lets the load through, loudly, which is the documented escape hatch.
    """
    if plan["allow"]:
        if plan["status"] in ("tight", "unknown"):
            print(plan["message"])
        return plan
    if plan["mode"] == "block":
        raise PreflightError(plan["message"], plan)
    print(plan["message"])
    return plan


def check_or_raise(profile: dict) -> dict:
    """Preflight for the main llama-server launch."""
    return enforce(plan_launch(profile))


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
                               vulkan_index: int, mmproj_path=None,
                               kv_cache_type: str = "f16",
                               n_slots: int = 1,
                               ubatch_size: int = 512) -> dict:
    """Preflight for a small-model launch on a single Vulkan device.

    kv_cache_type / n_slots / ubatch_size used to be guessed here rather than read from the
    lane config, which made every estimate disagree with the launcher: the configured executor
    runs -ctk q8_0 while this charged f16 bytes (a ~2x overestimate of KV), and the vision
    lane's --mmproj file (1.2 GB) was accepted in the signature and then never counted at all.
    Callers now pass the real launch values.

    mmproj goes in as profile["mmproj_path"] + ["vision_capable"], NOT as a post-hoc addition
    to the estimate. effective_params already folds a vision_capable mmproj into `draft_b`
    (same path as the MTP draft model), and preflight adds draft_b into weights_b BEFORE
    computing the verdict - so the projector participates in the fit decision. Adding it to
    the number afterwards would have let a model pass the gate and then fail to allocate,
    which is the exact failure this function exists to prevent.
    """
    profile = {"model_path": str(model_path), "context_size": int(ctx),
               "n_gpu_layers": 999, "gpu_devices": [int(vulkan_index)],
               "tensor_split": "1", "kv_cache_type": str(kv_cache_type or "f16"),
               "flash_attn": "on", "ubatch_size": int(ubatch_size),
               "n_slots": int(n_slots),
               "llama_bin_dir": CONFIG_DEFAULTS["llama_bin_dir"]}
    if mmproj_path and Path(str(mmproj_path)).exists():
        profile["mmproj_path"] = str(mmproj_path)
        profile["vision_capable"] = True
    plan = plan_launch(profile)
    enforce(plan)
    if plan["status"] != "nofit":
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
                      "desc": f"set app.json small_models.{role}.gpu = "
                              f"{best['index']} (Vulkan{best['index']} has "
                              f"{best['free_gb']} GB free)"})

    plan["suggestions"] = extra + plan.get("suggestions", [])
    plan["message"] = _human_msg(plan)
    if plan["mode"] == "block":
        raise PreflightError(plan["message"], plan)
    print(plan["message"])
    return plan

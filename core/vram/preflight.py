from datetime import datetime, timezone
from pathlib import Path

from ..backend import device_prefix
from ..config import ACTIVE_RUNTIME, CONFIG_DEFAULTS
from .constants import GB, PreflightError, _preflight_cfg
from .devices import DeviceQueryError, query_devices
from .estimator import (
    _build_suggestions,
    _eval_fit,
    _shares_for,
    effective_params,
    estimate_footprint,
    load_options,
)
from .gguf_parser import parse_gguf_info
from .host import HostMemoryQueryError, host_memory_snapshot
from .moe import expert_bytes_offloaded, is_moe, resolve_offload, smallest_fitting


def _moe_note(plan: dict) -> str:
    moe = plan.get("moe") or {}
    if not moe.get("n_cpu_moe"):
        return ""
    how = "chosen to fit" if moe.get("auto") else "set"
    return (f" --n-cpu-moe {moe['n_cpu_moe']} ({how}: ~{moe['experts_in_ram_gb']} GB of experts "
            "kept in RAM)")


def _device_label(device: dict, backend: str | None = None) -> str:
    return str(device.get("id") or
               f"{device_prefix(backend or device.get('backend') or CONFIG_DEFAULTS['backend'])}"
               f"{device.get('index')}")


def _human_msg(plan: dict) -> str:
    status = plan["status"]
    name = plan.get("model") or "model"
    if status == "unknown":
        return f"[vram] {name}: VRAM check unavailable - {plan.get('message', '')}"
    est = plan.get("estimate") or {}
    eff = plan.get("effective") or {}
    lines = [f"[vram] {name}: {est.get('total_gb', '?')} GB needed "
             f"(weights {est.get('weights_gb')} + KV@ctx {est.get('kv_gb')} + "
             f"compute {est.get('compute_gb')}) with -ngl {eff.get('n_gpu_layers')}"
             f"{_moe_note(plan)}, "
             f"split \"{eff.get('tensor_split')}\", headroom {est.get('headroom_gb')} GB"]
    for d in plan.get("devices", []):
        if d.get("target"):
            if d.get("short_gb", 0) > 0:
                lines.append(f"  {_device_label(d, eff.get('backend'))}: need {d['need_gb']} GB, "
                             f"free {d['free_gb']} GB -> short {d['short_gb']} GB")
            else:
                lines.append(f"  {_device_label(d, eff.get('backend'))}: need {d['need_gb']} GB, "
                             f"free {d['free_gb']} GB (margin {d['margin_gb']} GB)")
    host = plan.get("host") or {}
    if host and host.get("partial_offload"):
        if host.get("unbounded_cache"):
            lines.append("  system RAM: prompt cache is unbounded (-cram -1); set a finite cache_ram before launch")
        else:
            verdict = (f"margin {host['margin_gb']} GB" if host.get("fit")
                       else f"short {host['short_gb']} GB")
            lines.append(
                f"  system RAM: need {host['required_gb']} GB "
                f"(CPU weights {host['weights_gb']} + cache {host['cache_gb']} + "
                f"reserve {host['reserve_gb']}), available {host['available_gb']} GB "
                f"({verdict})"
            )
        if host.get("swap_warning"):
            lines.append(
                f"  warning: {host['swap_used_gb']} GB swap is already in use; "
                "swap is not counted as model capacity"
            )
    if status == "nofit" and plan.get("suggestions"):
        lines.append("Suggestions (apply in the Model Config panel, then load again):")
        for i, s in enumerate(plan["suggestions"][:5], 1):
            verdict = "then it fits" if s.get("fits_after") else "helps"
            lines.append(f"  {i}. {s['desc']} -> {verdict}")
    return "\n".join(lines)


def _devices_or_fail(eff: dict, plan: dict, force: bool = False) -> list:
    """Device list for the verdict, or [] with the plan marked not-allowed.

    strict=True is the point of this function: an absent/failed/unparseable
    `llama-bench --list-devices` is "we cannot tell", not "zero devices", and it must not
    produce a verdict the gate will trust. mode="warn"/"off" still lets the load through
    (with a loud message), because a driver hiccup should not brick the box.
    """
    try:
        return query_devices(
            eff["llama_bin_dir"], strict=True, force=force,
            backend=eff.get("backend"), runtime_env=eff.get("runtime_env"))
    except DeviceQueryError as e:
        plan["status"] = "unknown"
        plan["allow"] = False
        plan["message"] = (f"[vram] {plan.get('model') or 'model'}: cannot verify VRAM - {e} - "
                           "not launching, because a fit that cannot be checked is not a fit.")
        return []


def _host_plan(eff: dict, *, total_weights_b: int, gpu_candidate_weights_b: int,
               units: int, cfg: dict, snapshot: dict | None = None) -> dict:
    snapshot = snapshot or host_memory_snapshot()
    gpu_fraction = min(max(int(eff["n_gpu_layers"]), 0), units) / max(1, units)
    gpu_weights_b = int(max(0, gpu_candidate_weights_b) * gpu_fraction)
    host_weights_b = max(0, int(total_weights_b) - gpu_weights_b)
    partial_offload = host_weights_b > 0
    cache_b = int(eff.get("cache_ram_mb", 0)) * 1024 * 1024
    reserve_b = int(float(cfg.get("host_headroom_gb", 4.0)) * GB)
    unbounded = bool(eff.get("cache_ram_unbounded")) and partial_offload
    required_b = host_weights_b + cache_b + reserve_b
    available_b = int(snapshot["available_b"])
    margin_b = available_b - required_b
    swap_used_b = int(snapshot.get("swap_used_b") or 0)
    fit = (not partial_offload) or (not unbounded and margin_b >= 0)
    return {
        "fit": fit,
        "partial_offload": partial_offload,
        "unbounded_cache": unbounded,
        "weights_gb": round(host_weights_b / GB, 2),
        "cache_gb": round(cache_b / GB, 2),
        "reserve_gb": round(reserve_b / GB, 2),
        "required_gb": round(required_b / GB, 2),
        "available_gb": round(available_b / GB, 2),
        "total_gb": round(int(snapshot["total_b"]) / GB, 2),
        "margin_gb": round(max(0, margin_b) / GB, 2),
        "short_gb": round(max(0, -margin_b) / GB, 2),
        "swap_total_gb": round(int(snapshot.get("swap_total_b") or 0) / GB, 2),
        "swap_used_gb": round(swap_used_b / GB, 2),
        "swap_warning": partial_offload and swap_used_b >= 512 * 1024 * 1024,
    }


def plan_launch(profile: dict, overrides: dict | None = None,
                devices: list | None = None, fresh: bool = False) -> dict:
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
        "flash_attn", "gpu_devices", "ubatch_size", "backend",
        "cache_ram_mb", "cache_ram_unbounded")}
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
    # MoE: split by experts, not by layer (core/vram/moe.py). The same resolution runs in
    # core/process.py, so the config that is checked is the config that is launched.
    moe = is_moe(profile, info)
    explicit = eff["n_cpu_moe"] if eff.get("n_cpu_moe_set") else None
    eff["n_gpu_layers"], eff["n_cpu_moe"] = resolve_offload(
        profile, info, eff["n_gpu_layers"], explicit, None)
    fp = estimate_footprint(info, eff["context_size"], eff["kv_cache_type"],
                            eff["ubatch_size"], eff["flash_attn"],
                            n_cpu_moe=eff["n_cpu_moe"],
                            n_expert_used=info.get("n_expert") or 0)
    total_weights_b = fp["weights_b"] + eff["draft_b"]
    weights_b = total_weights_b
    moe_ram_b = 0
    if moe and eff["n_cpu_moe"] > 0:
        moe_ram_b = expert_bytes_offloaded(mp, info, fp["weights_b"], eff["n_cpu_moe"])
        weights_b -= moe_ram_b
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

    devs = devices if devices is not None else _devices_or_fail(eff, plan, force=fresh)
    if not devs:
        return plan
    plan["devices"] = [{"index": d["index"],
                        "id": d.get("id") or f"{device_prefix(eff['backend'])}{d['index']}",
                        "backend": d.get("backend", eff["backend"]), "name": d["name"],
                        "total_gb": round(d["total_b"] / GB, 2),
                        "free_gb": round(d["free_b"] / GB, 2),
                        "used_gb": round(d["used_b"] / GB, 2),
                        "target": False} for d in devs]
    tmap = {d["index"]: d for d in devs}
    targets = [tmap.get(i) for i in eff["gpu_devices"]]
    if any(t is None for t in targets):
        plan["allow"] = False
        plan["message"] = (f"[vram] {plan['model']}: target device(s) {eff['gpu_devices']} not "
                           f"present in {eff['backend']} device list {[d['index'] for d in devs]} - "
                           "not launching; fix the device index or the iGPU skip list.")
        return plan
    units = info["n_layer"] + 1
    s_norm = _shares_for(targets, eff["tensor_split"])
    needs, margins = _eval_fit(targets, s_norm, weights_b, units,
                               eff["n_gpu_layers"], fp["kv_total_b"],
                               fp["compute_b"], fp["headroom_b"])
    if moe and explicit is None and eff["n_gpu_layers"] >= units and any(m < 0 for m in margins):
        # no explicit setting: use the least RAM offload that fits the free VRAM
        def _fits_with(n):
            w = fp["weights_b"] + eff["draft_b"] - expert_bytes_offloaded(mp, info, fp["weights_b"], n)
            _, mg = _eval_fit(targets, s_norm, w, units, units, fp["kv_total_b"],
                              fp["compute_b"], fp["headroom_b"])
            return all(m >= 0 for m in mg)
        auto = smallest_fitting(info["n_layer"], _fits_with)
        if auto is not None:
            eff["n_gpu_layers"], eff["n_cpu_moe"] = resolve_offload(profile, info, units, None, auto)
            moe_ram_b = expert_bytes_offloaded(mp, info, fp["weights_b"], auto)
            weights_b = fp["weights_b"] + eff["draft_b"] - moe_ram_b
            plan["auto_n_cpu_moe"] = auto
            needs, margins = _eval_fit(targets, s_norm, weights_b, units, units,
                                       fp["kv_total_b"], fp["compute_b"], fp["headroom_b"])
    if moe:
        plan["effective"].update(n_gpu_layers=eff["n_gpu_layers"], n_cpu_moe=eff["n_cpu_moe"])
        if "auto_n_cpu_moe" in plan:
            fp["total_b"] = weights_b + fp["kv_total_b"] + fp["compute_b"]
            plan["estimate"].update(weights_gb=round(weights_b / GB, 2),
                                    total_gb=round(fp["total_b"] / GB, 2))
        plan["moe"] = {"n_cpu_moe": eff["n_cpu_moe"], "experts_in_ram_gb": round(moe_ram_b / GB, 2),
                       "auto": "auto_n_cpu_moe" in plan}
    plan["load_options"] = load_options(targets, weights_b, units, eff["n_gpu_layers"], fp["kv_total_b"],
                                        fp["compute_b"], fp["headroom_b"])
    for t, need, margin in zip(targets, needs, margins):
        for pd in plan["devices"]:
            if pd["index"] == t["index"]:
                pd.update({"target": True, "need_gb": round(need / GB, 2),
                           "free_gb": round(t["free_b"] / GB, 2),
                           "margin_gb": round(margin / GB, 2),
                           "short_gb": round(max(0.0, -margin) / GB, 2)})
    host_snapshot = None
    try:
        host_snapshot = host_memory_snapshot()
        plan["host"] = _host_plan(
            eff,
            total_weights_b=total_weights_b,
            gpu_candidate_weights_b=weights_b,
            units=units,
            cfg=cfg,
            snapshot=host_snapshot,
        )
    except HostMemoryQueryError as exc:
        plan["status"] = "unknown"
        plan["allow"] = False
        plan["message"] = f"[vram] {plan['model']}: cannot verify system RAM - {exc} - not launching."
        return plan

    vram_fits = all(m >= 0 for m in margins)
    if vram_fits and plan["host"]["fit"]:
        tight = min(margins) < 0.08 * max(t["total_b"] for t in targets)
        plan["status"] = "tight" if (tight or fp["confidence"] == "low") else "fit"
    else:
        plan["status"] = "nofit"
        plan["allow"] = cfg["mode"] != "block"
        if not vram_fits:
            plan["suggestions"] = _build_suggestions(targets, eff, info, fp, s_norm, units)
            # Lowering -ngl can solve VRAM pressure by moving more weights into
            # system memory. Do not present it as a solution if that candidate
            # would fail the same host-RAM admission gate.
            safe_suggestions = []
            for suggestion in plan["suggestions"]:
                if suggestion.get("type") == "n_gpu_layers":
                    candidate_eff = dict(eff, n_gpu_layers=int(suggestion["value"]))
                    candidate_host = _host_plan(
                        candidate_eff,
                        total_weights_b=total_weights_b,
                        gpu_candidate_weights_b=weights_b,
                        units=units,
                        cfg=cfg,
                        snapshot=host_snapshot,
                    )
                    if not candidate_host["fit"]:
                        continue
                safe_suggestions.append(suggestion)
            plan["suggestions"] = safe_suggestions
        if not plan["host"]["fit"]:
            if plan["host"]["unbounded_cache"]:
                plan["suggestions"].append({
                    "type": "cache_ram", "value": 2048, "fits_after": None,
                    "desc": "set a finite cache_ram value (for example 2048 MiB)",
                })
            plan["suggestions"].append({
                "type": "host_ram", "value": None, "fits_after": False,
                "desc": "reduce CPU offload/cache, use a smaller model, or add system RAM",
            })
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
    # fresh: the previous model was only just stopped; a free-VRAM reading cached while it was still resident
    # would size --n-cpu-moe for cards that are now empty
    plan = enforce(plan_launch(profile, fresh=True))
    # the launch command is built after this gate and must use the value that was checked
    if plan.get("auto_n_cpu_moe") is not None:
        profile["_auto_n_cpu_moe"] = plan["auto_n_cpu_moe"]
    else:
        profile.pop("_auto_n_cpu_moe", None)
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
                        f"~{freed:.1f} GB across {sub.get('effective', {}).get('backend', 'GPU')} "
                        f"devices {sub.get('effective', {}).get('gpu_devices')})",
                "fits_after": True}
    except Exception:
        return None


def check_small_model_or_raise(role: str, model_path, ctx: int,
                               gpu_index: int, mmproj_path=None,
                               kv_cache_type: str = "f16",
                               n_slots: int = 1,
                               ubatch_size: int = 512,
                               cache_ram: int = 0) -> dict:
    """Preflight for a small-model launch on one active-runtime GPU.

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
               "n_gpu_layers": 999, "gpu_devices": [int(gpu_index)],
               "tensor_split": "1", "kv_cache_type": str(kv_cache_type or "f16"),
               "flash_attn": "on", "ubatch_size": int(ubatch_size),
               "n_slots": int(n_slots),
               "cache_ram": int(cache_ram), "_small_model": True,
               "llama_bin_dir": ACTIVE_RUNTIME["llama_bin_dir"],
               "backend": ACTIVE_RUNTIME["backend"],
               "runtime_env": dict(ACTIVE_RUNTIME.get("env") or {})}
    if mmproj_path and Path(str(mmproj_path)).exists():
        profile["mmproj_path"] = str(mmproj_path)
        profile["vision_capable"] = True
    plan = plan_launch(profile)
    enforce(plan)
    if plan["status"] != "nofit":
        return plan

    eff = plan.get("effective") or {}
    idx = (eff.get("gpu_devices") or [gpu_index])[0]
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
                              f"{best['index']} ({_device_label(best, eff.get('backend'))} has "
                              f"{best['free_gb']} GB free)"})

    plan["suggestions"] = extra + plan.get("suggestions", [])
    plan["message"] = _human_msg(plan)
    if plan["mode"] == "block":
        raise PreflightError(plan["message"], plan)
    print(plan["message"])
    return plan

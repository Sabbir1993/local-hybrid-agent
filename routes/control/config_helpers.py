from pathlib import Path
from typing import Optional
from core.config import (
    ACTIVE_RUNTIME,
    apply_runtime,
    CONFIG_DEFAULTS,
    CONFIG_INT_FIELDS,
    CONFIG_CHOICE_FIELDS,
    CONFIG_TARGETS,
    MODEL_CONFIG_KEYS,
)
from core import supervision
from core.supervision import SUPERVISION_LIMITS
from core import reasoning
from core.profiles import companions, in_models_dir, load_model_configs, _model_key
from core.state import state


def _apply_config_update(profile: dict, key: str, value) -> Optional[str]:
    if profile is None:
        return "No profile loaded to update"
    if key in SUPERVISION_LIMITS:
        return _apply_supervision_update(key, value)
    if key == "keepalive_interval_s":
        try:
            state.keepalive_interval_s = max(5, min(600, int(value)))
        except (TypeError, ValueError):
            return "keepalive_interval_s must be an integer"
        return None
    if isinstance(value, str) and not value.strip():
        value = CONFIG_DEFAULTS.get(key)
        if value is None:
            return f"{key} cannot be empty"
    if key == "mtp_enabled":
        if isinstance(value, str):
            value = value.strip().lower() in ("true", "1", "on", "yes")
        profile["mtp_enabled"] = bool(value)
        return None
    if key == "kv_unified":
        if isinstance(value, str):
            value = value.strip().lower() in ("true", "1", "on", "yes")
        profile["kv_unified"] = bool(value)
        return None
    if key == "vision_capable":
        if isinstance(value, str):
            value = value.strip().lower() in ("true", "1", "on", "yes")
        profile["vision_capable"] = bool(value)
        return None
    if key in CONFIG_INT_FIELDS:
        lo, hi = CONFIG_INT_FIELDS[key]
        try:
            v = int(value)
        except (TypeError, ValueError):
            return f"{key} must be an integer"
        v = max(lo, min(hi, v))
        if key == "context_size":
            v -= v % 8
        section, field = CONFIG_TARGETS[key]
        if section:
            profile.setdefault(section, {})[field] = v
        else:
            profile[field] = v
        return None
    if key in CONFIG_CHOICE_FIELDS:
        value = str(value).strip()
        if value not in CONFIG_CHOICE_FIELDS[key]:
            return f"{key} must be one of {CONFIG_CHOICE_FIELDS[key]}"
        section, field = CONFIG_TARGETS[key]
        if section:
            profile.setdefault(section, {})[field] = value
        else:
            profile[field] = value
        return None
    if key == "tensor_split":
        value = str(value).strip()
        segs = [s.strip() for s in value.split(",")]
        if not (1 <= len(segs) <= 4) or not all(s.isdigit() and int(s) >= 0 for s in segs):
            return "tensor_split must be comma-separated non-negative integers, e.g. 9,11 (0,1 = GPU 2 only, 1,0 = GPU 1 only)"
        if not any(int(s) > 0 for s in segs):
            return "tensor_split: at least one GPU share must be > 0 (e.g. 0,1 or 1,0)"
        profile.setdefault("tuned", {})["tensor_split"] = ",".join(segs)
        return None
    return f"unknown config field: {key}"


def _apply_supervision_update(key: str, value) -> Optional[str]:
    """Persist one llama-server supervision limit to config/app.json's "supervision" block.

    These are process-level, not per-model, so they must not go through the profile path:
    a health timeout is the same whichever GGUF is loaded. Clamped here with the same bounds
    core/supervision.py reads, so the stored value and the effective value cannot disagree.
    """
    from core.config import update_app_config
    from core.small_model import APP_CONFIG

    try:
        v = int(value)
    except (TypeError, ValueError):
        return f"{key} must be an integer"
    default, lo, hi = SUPERVISION_LIMITS[key]
    v = max(lo, min(hi, v))

    def _mutate(cfg):
        cfg.setdefault("supervision", {})[key] = v

    update_app_config(_mutate)
    # keep the in-memory copy in step so the next read sees it without a restart
    APP_CONFIG["supervision"] = {**(APP_CONFIG.get("supervision") or {}), key: v}
    return None


def check_tool_calling(filename: str) -> bool:
    nl = str(filename or "").lower()
    tool_keywords = [
        "instruct", "coder", "chat", "hermes", "tool", "function", 
        "agent", "qwen", "llama-3", "mistral", "mixtral", "command-r",
        "deepseek", "phi-3", "phi-4", "gemma-2", "heretic"
    ]
    return any(k in nl for k in tool_keywords)


def _reasoning_caps(mode: str) -> dict:
    """Effort-chip capability: "levels" (None..Extra), "toggle" (always-thinking
    template: budget only) or "none" (chip hidden). See core/reasoning.py."""
    return {"reasoning_available": mode != "none", "reasoning_mode": mode}


def _config_for_profile(p: dict) -> dict:
    return {
        # process supervision, not per-model: shown in the same drawer so the two knobs that
        # decide whether a big model loads at all are reachable without editing app.json
        **supervision.view(),
        "context_size": p.get("context_size", CONFIG_DEFAULTS["context_size"]),
        "n_gpu_layers": p.get("tuned", {}).get("n_gpu_layers", p.get("n_gpu_layers", CONFIG_DEFAULTS["n_gpu_layers"])),
        "tensor_split": p.get("tuned", {}).get("tensor_split", p.get("tensor_split", CONFIG_DEFAULTS["tensor_split"])),
        "split_mode": p.get("split_mode", CONFIG_DEFAULTS["split_mode"]),
        "threads": p.get("threads", CONFIG_DEFAULTS["threads"]),
        "threads_batch": p.get("threads_batch", CONFIG_DEFAULTS["threads_batch"]),
        "batch_size": p.get("batch_size", CONFIG_DEFAULTS["batch_size"]),
        "ubatch_size": p.get("ubatch_size", CONFIG_DEFAULTS["ubatch_size"]),
        "n_slots": p.get("n_slots", CONFIG_DEFAULTS["n_slots"]),
        "flash_attn": p.get("flash_attn", CONFIG_DEFAULTS["flash_attn"]),
        "kv_cache_type": p.get("kv_cache_type", CONFIG_DEFAULTS["kv_cache_type"]),
        "kv_unified": bool(p.get("kv_unified", CONFIG_DEFAULTS["kv_unified"])),
        "kv_unified_per_slot": p.get("kv_unified_per_slot", CONFIG_DEFAULTS["kv_unified_per_slot"]),
        "cache_reuse": p.get("cache_reuse", CONFIG_DEFAULTS["cache_reuse"]),
        "cache_ram": p.get("cache_ram", CONFIG_DEFAULTS["cache_ram"]),
        "keepalive_interval_s": state.keepalive_interval_s,
        "llama_bin_dir": p.get("llama_bin_dir", CONFIG_DEFAULTS["llama_bin_dir"]),
        "gpu_devices": p.get("gpu_devices", CONFIG_DEFAULTS["gpu_devices"]),
        "runtime": ACTIVE_RUNTIME["name"],
        "mtp_available": bool(p.get("mtp_draft_path")),
        "mtp_draft_path": p.get("mtp_draft_path"),
        "mtp_enabled": bool(p.get("mtp_enabled", False)) if not p.get("mtp_draft_path") else bool(p.get("mtp_enabled", True)),
        "mtp_draft_n_max": p.get("mtp_draft_n_max", 3),
        "mmproj_available": bool(p.get("mmproj_path")),
        "mmproj_path": p.get("mmproj_path"),
        "vision_capable": bool(p.get("vision_capable", False)) if not p.get("mmproj_path") else bool(p.get("vision_capable", True)),
        "mtp_note": p.get("mtp_note", ""),
        "mmproj_note": p.get("mmproj_note", ""),
        "tools_available": check_tool_calling(str(p.get("name") or p.get("model_path") or "")),
        **_reasoning_caps(reasoning.local_mode(p.get("model_path"))),
    }


def _standalone_profile(target: str) -> Optional[dict]:
    """Profile dict for a selected-but-unloaded model: saved config + MTP & MMProj detection.

    Same merge logic build_dynamic_profile() uses at load time, so values shown
    and saved in the drawer match what the model will actually launch with.
    """
    p = Path(target)
    # only model files under a models root: ?model= is caller-supplied, and an
    # arbitrary path would reveal whether files exist anywhere on the server
    if not in_models_dir(p) or not p.exists():
        return None
    saved = load_model_configs().get(_model_key(p)) or {}
    prof = {"name": p.stem, "model_path": str(p)}
    for k, v in CONFIG_DEFAULTS.items():
        prof.setdefault(k, v)
    for k in MODEL_CONFIG_KEYS:
        if k in saved:
            prof[k] = saved[k]
    # n_gpu_layers / tensor_split live in the "tuned" section while loaded;
    # put saved values there so _config_for_profile/save round-trip correctly
    prof.setdefault("tuned", {})
    if "n_gpu_layers" in saved:
        prof["tuned"]["n_gpu_layers"] = saved["n_gpu_layers"]
    if "tensor_split" in saved:
        prof["tuned"]["tensor_split"] = saved["tensor_split"]
    comp = companions(p)
    mtp, mmproj = comp["mtp"], comp["mmproj"]
    prof["mtp_draft_path"] = str(mtp) if mtp else None
    prof["mmproj_path"] = str(mmproj) if mmproj else None
    prof["mtp_note"], prof["mmproj_note"] = comp["mtp_note"], comp["mmproj_note"]
    if prof.get("mtp_draft_path"):
        prof.setdefault("mtp_enabled", True)
    if prof.get("mmproj_path"):
        prof.setdefault("vision_capable", True)
    return apply_runtime(prof)

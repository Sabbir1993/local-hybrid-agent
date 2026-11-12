import re
from pathlib import Path
from typing import Optional
from core.config import (
    ACTIVE_RUNTIME,
    apply_runtime,
    CONFIG_DEFAULTS,
    CONFIG_INT_FIELDS,
    CONFIG_CHOICE_FIELDS,
    CONFIG_TARGETS,
    MAX_GPUS,
    MODEL_CONFIG_KEYS,
)
from core import supervision
from core.supervision import SUPERVISION_LIMITS
from core import reasoning
from core.profiles import companions, in_models_dir, load_model_configs, _model_key
from core.state import state
from core.engines import engine_of
from core.engines import vllm as vllm_engine

# free-text vLLM settings ("" = not set); they become command-line arguments, so only plain names and paths are accepted
VLLM_TEXT_KEYS = ("vllm_tokenizer", "vllm_tool_parser", "vllm_reasoning_parser")
_EXTRA_ARGS_RX = re.compile(r"^[A-Za-z0-9_./:=,\- ]{1,300}$")
_SAFE_ARG_RX = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_./:\-]{0,199}$")

# machine-level concurrency limits kept in config/app.json: key -> (section path, field, default, min, max)
SERVING_LIMITS = {
    "max_inflight_per_user": ("serving", "max_inflight_per_user", 1, 1, 16),
    "vllm_max_num_seqs_default": ("engines.vllm", "max_num_seqs", 8, 1, 64),
}


def _apply_config_update(profile: dict, key: str, value) -> Optional[str]:
    if profile is None:
        return "No profile loaded to update"
    if key in SUPERVISION_LIMITS:
        return _apply_supervision_update(key, value)
    if key in SERVING_LIMITS:
        return _apply_serving_update(key, value)
    if key == "keepalive_interval_s":
        try:
            state.keepalive_interval_s = max(5, min(600, int(value)))
        except (TypeError, ValueError):
            return "keepalive_interval_s must be an integer"
        return None
    if key in VLLM_TEXT_KEYS:
        v = str(value or "").strip()
        if v and not _SAFE_ARG_RX.match(v):
            return f"{key}: use a Hugging Face repo id, a folder path or a parser name (letters, digits, . _ - / :)"
        profile[key] = v
        return None
    if key == "vllm_extra_args":
        v = " ".join(str(value or "").split())
        if v and not _EXTRA_ARGS_RX.match(v):
            return "vllm_extra_args: only flags and plain values (letters, digits, . _ - / : = ,), e.g. --kv-cache-dtype fp8"
        if "--trust-remote-code" in v:
            return ("vllm_extra_args: --trust-remote-code runs code shipped inside the model folder; add it in "
                    "config/app.json engines.vllm.extra_args if you trust the model")
        profile[key] = v
        return None
    if key == "vllm_enforce_eager":
        if isinstance(value, str):
            value = value.strip().lower() in ("true", "1", "on", "yes")
        profile[key] = bool(value)
        return None
    if key == "engine":
        allowed = vllm_engine.engines_for(profile.get("model_path") or "")
        if allowed and str(value).strip() not in allowed:
            return f"this model can only run on: {', '.join(allowed)}"
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
    if key == "use_mmap":
        if isinstance(value, str):
            value = value.strip().lower() in ("true", "1", "on", "yes")
        profile["use_mmap"] = bool(value)
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
        if not (1 <= len(segs) <= MAX_GPUS) or not all(s.isdigit() and int(s) >= 0 for s in segs):
            return (f"tensor_split must be 1-{MAX_GPUS} comma-separated non-negative integers, one per GPU, "
                    "e.g. 9,11 (a 0 skips that GPU)")
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


def _apply_serving_update(key: str, value) -> Optional[str]:
    """Persist one concurrency limit (SERVING_LIMITS) into its nested section of config/app.json.

    Machine-level like the supervision limits, so it skips the profile path. Clamped to the same range the readers
    assume (llm_stream._Admission for per-user, core/engines/vllm.py for max_num_seqs).
    """
    from core.config import update_app_config
    from core.small_model import APP_CONFIG

    try:
        v = int(value)
    except (TypeError, ValueError):
        return f"{key} must be an integer"
    section, field, _default, lo, hi = SERVING_LIMITS[key]
    v = max(lo, min(hi, v))

    def _walk(cfg):
        for part in section.split("."):
            cfg = cfg.setdefault(part, {})
        return cfg

    update_app_config(lambda cfg: _walk(cfg).__setitem__(field, v))
    # keep the in-memory copy in step so the next read sees it without a restart (other keys of the section stay)
    _walk(APP_CONFIG)[field] = v
    return None


def check_tool_calling(filename: str) -> bool:
    nl = str(filename or "").lower()
    tool_keywords = [
        "instruct", "coder", "chat", "hermes", "tool", "function", 
        "agent", "qwen", "llama-3", "mistral", "mixtral", "command-r",
        "deepseek", "phi-3", "phi-4", "gemma-2", "heretic"
    ]
    return any(k in nl for k in tool_keywords)


def _serving_value(key: str):
    from core.small_model import APP_CONFIG
    section, field, default, _lo, _hi = SERVING_LIMITS[key]
    return (APP_CONFIG.get(section) or {}).get(field, default)


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
        "use_mmap": bool(p.get("use_mmap", CONFIG_DEFAULTS.get("use_mmap", True))),
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
        # engine: llama.cpp or vLLM (core/engines); the vLLM fields are used only when engine == "vllm"
        **_layer_info(p.get("model_path") or ""),
        "engine": engine_of(p),
        "engines": vllm_engine.engines_for(p.get("model_path") or "") or ["llama"],
        "format": vllm_engine.model_format(p.get("model_path") or ""),
        "vllm_available": _vllm_installed(),
        "tensor_parallel_size": int(p.get("tensor_parallel_size") or 0),
        "pipeline_parallel_size": int(p.get("pipeline_parallel_size") or 1),
        "vllm_parallel_effective": list(vllm_engine.parallel_sizes(p)),
        "gpu_memory_utilization_pct": int(p.get("gpu_memory_utilization_pct") or 0),
        "gpu_memory_utilization_default": vllm_engine.settings().get("gpu_memory_utilization"),
        "vllm_dtype": p.get("vllm_dtype") or "auto",
        "vllm_quantization": p.get("vllm_quantization") or "auto",
        "vllm_tokenizer": p.get("vllm_tokenizer") or "",
        "vllm_tool_parser": p.get("vllm_tool_parser") if p.get("vllm_tool_parser") is not None
                            else vllm_engine.settings().get("tool_call_parser", ""),
        "vllm_reasoning_parser": p.get("vllm_reasoning_parser") if p.get("vllm_reasoning_parser") is not None
                                 else vllm_engine.settings().get("reasoning_parser", ""),
        "vllm_enforce_eager": bool(p.get("vllm_enforce_eager", False)),
        "vllm_extra_args": p.get("vllm_extra_args") or "",
        "vllm_max_num_seqs": vllm_engine.max_num_seqs(p),
        "max_inflight_per_user": _serving_value("max_inflight_per_user"),
        "vllm_max_num_seqs_default": vllm_engine.settings().get("max_num_seqs", 8),
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
        from core.profiles.paths import models_roots
        for r in models_roots():
            candidate = (r / target).resolve()
            if in_models_dir(candidate) and candidate.exists():
                p = candidate
                break
        else:
            return None
    if p.is_dir():
        from core.profiles import build_dynamic_profile
        prof = build_dynamic_profile(p)          # Hugging Face model folder (vLLM)
        prof.setdefault("tuned", {})
        return prof
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


def _vllm_installed() -> bool:
    try:
        vllm_engine.find_vllm()
        return True
    except Exception:
        return False


def _layer_info(model_path: str) -> dict:
    """{"total_layers", "layer_mb"} for the Model Config card, so an admin can work out how many layers fit on a GPU.
    GGUF: block_count + 1 (the output layer), which is the number "-ngl" counts up to. Model folder: num_hidden_layers.
    Both None when the file cannot be read."""
    out = {"total_layers": None, "layer_mb": None}
    if not model_path:
        return out
    try:
        mp = Path(model_path)
        if mp.is_dir():
            import json
            n = int(json.loads((mp / "config.json").read_text(encoding="utf-8")).get("num_hidden_layers") or 0)
            size_b = vllm_engine.folder_size_bytes(mp)
        else:
            from core.vram.gguf_parser import parse_gguf_info
            info = parse_gguf_info(mp) or {}
            if not info.get("stat_ok", True) or not info.get("complete", True):
                return out
            if not info.get("n_layer"):
                return out
            n = int(info["n_layer"]) + 1
            size_b = int(info.get("file_bytes") or 0)
        if n > 0:
            out["total_layers"] = n
            if size_b > 0:
                out["layer_mb"] = round(size_b / n / 1048576)
    except Exception:
        pass
    return out

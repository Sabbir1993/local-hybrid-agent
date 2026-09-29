import re
from pathlib import Path
from typing import Optional

from .constants import (
    _KIND_OK,
    BUILTIN_LANES,
    LANE_NAME_RE,
    SD_FILE_KEYS,
    SD_SAMPLER_RE,
    kind_ok,
)
from .registry import registry
from .user_storage import _apply_fallback


def suggest_port() -> int:
    from ..small_model import APP_CONFIG
    used = {int((c or {}).get("port") or 0) for c in (APP_CONFIG.get("small_models") or {}).values()
            if isinstance(c, dict)}
    p = 8094
    while p in used:
        p += 1
    return p


def _set_port_gpu(name: str, cur: dict, data: dict, sm: dict, cpu_ok: bool = False) -> None:
    """Validate + set a local lane's port and GPU (-1 = CPU when allowed)."""
    from ..config import ACTIVE_RUNTIME, LLAMA_SERVER_PORT
    port = int(data.get("port") or cur.get("port") or suggest_port())
    if not (1024 < port < 65536):
        raise ValueError("port must be between 1025 and 65535")
    if port in (LLAMA_SERVER_PORT, 8000):
        raise ValueError(f"port {port} is used by the main server")
    for other, ocfg in sm.items():
        if other != name and isinstance(ocfg, dict) and int(ocfg.get("port") or 0) == port:
            raise ValueError(f"port {port} is already used by '{(ocfg.get('label') or other)}'")
    cur["port"] = port
    if data.get("gpu") is not None:
        gpu = int(data["gpu"])
        if gpu not in (ACTIVE_RUNTIME.get("gpu_devices") or []) and not (cpu_ok and gpu == -1):
            raise ValueError(f"GPU {gpu} isn't available in the active runtime")
        cur["gpu"] = gpu
    cur.setdefault("gpu", ACTIVE_RUNTIME.get("small_model_gpu") or 1)


def _save_sdcpp_lane(name: str, kind: str, cur: dict, data: dict) -> dict:
    """Image/video maker run on this PC by stable-diffusion.cpp (sd-server, Vulkan)."""
    from .. import profiles
    from ..config import update_app_config
    from ..small_model import APP_CONFIG, small_models
    sm = APP_CONFIG.get("small_models") or {}
    base = Path(APP_CONFIG.get("models_dir") or "E:/AI/Models")
    folder = profiles.media_root(kind)
    what = {"diffusion_model": "diffusion model", "llm": "text encoder", "vae": "VAE",
            "llm_vision": "vision weights"}

    def _file(v, key):
        p = Path(str(v))
        full = p if p.is_absolute() else base / p
        if full.suffix.lower() not in (".gguf", ".safetensors"):
            raise ValueError(f"the {what[key]} must be a .gguf or .safetensors file")
        if not profiles.in_media_dir(full, kind):
            raise ValueError(f"the {what[key]} must be in {folder}")
        if not full.exists():
            raise ValueError(f"{what[key]} not found: {full.name}")
        try:
            return str(full.relative_to(base)).replace("\\", "/")
        except ValueError:
            return str(full)

    for key in SD_FILE_KEYS:
        if key in data:
            v = str(data.get(key) or "").strip()
            if v:
                cur[key] = _file(v, key)
            else:
                cur.pop(key, None)
    if not cur.get("diffusion_model"):
        raise ValueError("pick the diffusion model file")
    _set_port_gpu(name, cur, data, sm, cpu_ok=True)
    for key, lo, hi, typ in (("steps", 1, 150, int), ("cfg_scale", 0.0, 30.0, float),
                             ("flow_shift", 0.0, 20.0, float), ("timeout_s", 30, 7200, int)):
        if data.get(key) is not None and data.get(key) != "":
            v = typ(data[key])
            if not lo <= v <= hi:
                raise ValueError(f"{key} must be between {lo} and {hi}")
            cur[key] = v
        elif key in data:
            cur.pop(key, None)
    if data.get("sampler") is not None:
        smp = str(data.get("sampler") or "").strip().lower()
        if smp and not SD_SAMPLER_RE.match(smp):
            raise ValueError("sampler must be a name like euler, euler_a or dpm++2m")
        if smp:
            cur["sampler"] = smp
        else:
            cur.pop("sampler", None)
    for key in ("offload_to_cpu", "vae_tiling", "edit_refs"):
        if data.get(key) is not None:
            cur[key] = bool(data[key])
    if data.get("default_size") is not None:
        ds = str(data.get("default_size") or "").strip().lower()
        if ds and ds not in ("small", "medium", "large", "xlarge"):
            raise ValueError("default size must be small, medium, large or xlarge")
        if ds and cur.get("kind", data.get("kind")) != "video_gen":
            cur["default_size"] = ds
        else:
            cur.pop("default_size", None)
    if data.get("max_refs") not in (None, ""):
        mr = int(data["max_refs"])
        if not 1 <= mr <= 10:
            raise ValueError("max reference pictures must be between 1 and 10")
        cur["max_refs"] = mr
    for k in ("url", "auth_ref", "body_template", "model", "mmproj", "ctx", "np", "idle_unload_s"):
        cur.pop(k, None)
    cur.update({"kind": kind, "engine": "sdcpp"})
    if data.get("label") is not None:
        cur["label"] = str(data["label"]).strip()[:40] or name
    if "fallback" in data:
        _apply_fallback(name, kind, cur, data.get("fallback") or None, registry())

    def _mut(cfg):
        cfg.setdefault("small_models", {})[name] = cur
    update_app_config(_mut)
    small_models.reconfigure(name, cur)
    return cur


def save_local_lane(name: str, data: dict) -> dict:
    """Create/update a shared local lane (admin). Validates, persists, applies live."""
    from .. import profiles
    from ..config import update_app_config
    from ..small_model import APP_CONFIG, BUILTIN_KINDS, small_models
    if name == "main":
        raise ValueError("the main model is configured from the top bar")
    sm = APP_CONFIG.get("small_models") or {}
    is_new = name not in sm
    if is_new and not LANE_NAME_RE.match(name):
        raise ValueError("name must be 2-32 lowercase letters, digits, '-' or '_', starting with a letter")
    if is_new and name in registry():
        raise ValueError(f"'{name}' is already used")
    cur = dict(sm.get(name) or {})
    kind = BUILTIN_KINDS.get(name) or str(data.get("kind") or cur.get("kind") or "chat")
    if kind not in _KIND_OK:
        raise ValueError("type must be chat, vision, embed, image_gen, video_gen or stt")
    if not is_new and cur.get("kind") and cur.get("kind") != kind:
        raise ValueError("a model's type can't be changed - add a new one instead")
    if kind in ("image_gen", "video_gen"):
        if str(data.get("engine") or "sdcpp") != "sdcpp":
            raise ValueError("images and videos on this PC are made by stable-diffusion.cpp (engine sdcpp)")
        return _save_sdcpp_lane(name, kind, cur, data)
    models_base = Path(APP_CONFIG.get("models_dir") or "E:/AI/Models")
    model_ext = ".bin" if kind == "stt" else ".gguf"

    def _model_path(v, what):
        if not v:
            return None
        p = Path(str(v))
        full = p if p.is_absolute() else models_base / p
        if full.suffix.lower() != model_ext:
            raise ValueError(f"{what} must be a {model_ext} file")
        if kind == "stt":
            if not profiles.in_media_dir(full, "stt"):
                raise ValueError(f"{what} must be in the voice models folder ({profiles.media_root('stt')})")
        elif not profiles.in_helper_dir(full) or any(
                profiles.in_media_dir(full, k) for k in profiles.MEDIA_DIR_NAMES):
            raise ValueError(f"{what} must be in the orchestrator folder ({profiles.helper_root()})")
        if not full.exists():
            raise ValueError(f"{what} not found: {full.name}")
        try:
            return str(full.relative_to(models_base)).replace("\\", "/")
        except ValueError:
            return str(full)

    if "model" in data:
        cur["model"] = _model_path(data.get("model"), "model file")
    if kind == "vision" and "mmproj" in data:
        cur["mmproj"] = _model_path(data.get("mmproj"), "image projector (mmproj)")
    if not cur.get("model"):
        raise ValueError("pick a model file")
    if kind == "vision" and not cur.get("mmproj"):
        raise ValueError("an image model also needs its mmproj file")

    _set_port_gpu(name, cur, data, sm, cpu_ok=(kind == "stt"))   # whisper can also run on the CPU
    for k, lo, hi in (("ctx", 512, 262144), ("idle_unload_s", 0, 86400), ("np", 1, 16),
                      ("threads", 1, 64), ("kv_unified_per_slot", 0, 262144)):
        if data.get(k) is not None:
            v = int(data[k])
            if not lo <= v <= hi:
                raise ValueError(f"{k} must be between {lo} and {hi}")
            cur[k] = v
    if kind == "stt":
        cur["engine"] = "whisper"
        cur.pop("ctx", None)
        if "language" in data:
            lang = str(data.get("language") or "auto").strip().lower()
            if lang != "auto" and not re.match(r"^[a-z]{2,3}$", lang):
                raise ValueError("language must be 'auto' or a language code like 'en' or 'bn'")
            cur["language"] = lang
        cur.setdefault("language", "auto")
    else:
        cur.setdefault("ctx", 8192)
    if data.get("label") is not None:
        cur["label"] = str(data["label"]).strip()[:40] or name
    if name not in BUILTIN_KINDS:
        cur["kind"] = kind
    if "fallback" in data:
        _apply_fallback(name, kind, cur, data.get("fallback") or None, registry())

    def _mut(cfg):
        cfg.setdefault("small_models", {})[name] = cur
    update_app_config(_mut)
    small_models.reconfigure(name, cur)
    return cur


def delete_local_lane(name: str, move_jobs_to: Optional[str] = None) -> None:
    from ..config import update_app_config
    from ..small_model import BUILTIN_KINDS, small_models
    if name in BUILTIN_LANES or name in BUILTIN_KINDS:
        raise ValueError("built-in models can't be removed, only reconfigured")
    movable = set()
    if move_jobs_to:
        kind = (registry().get(name) or {}).get("kind")
        tgt = (registry().get(move_jobs_to) or {}).get("kind")
        if kind and tgt and (kind == tgt or kind_ok(kind, tgt)):
            movable.add(move_jobs_to)

    def _mut(cfg):
        (cfg.get("small_models") or {}).pop(name, None)
        rm = cfg.get("role_map") or {}
        for j, ln in list(rm.items()):
            if ln == name:
                if move_jobs_to and move_jobs_to in movable:
                    rm[j] = move_jobs_to
                else:
                    rm.pop(j)
        for other in (cfg.get("small_models") or {}).values():
            if isinstance(other, dict) and other.get("fallback") == name:
                other.pop("fallback", None)
    update_app_config(_mut)
    small_models.reconfigure(name, None)

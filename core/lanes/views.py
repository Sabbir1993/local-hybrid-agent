from typing import Optional

from .constants import JOBS, SD_FILE_KEYS
from .registry import allow_cloud_audio, default_lane, registry, role_map
from .target import Target


def _status_text(row: dict) -> dict:
    """{level: ok|idle|warn|error, text} for the status badge."""
    if row.get("cloud_key"):
        return {"level": "ok", "text": f"Cloud: {row.get('cloud_display') or row['cloud_key']}"}
    if not row.get("local"):
        return {"level": "warn", "text": "Cloud model not set or removed - using its backup model"}
    if row["name"] == "main":
        return ({"level": "ok", "text": "Running"} if row.get("loaded")
                else {"level": "idle", "text": "Not loaded (start it from the top bar)"})
    if row.get("error"):
        return {"level": "error", "text": f"Failed to start: {row['error']}"}
    if row.get("engine") == "sdcpp":
        if not row.get("files_ok"):
            from ..small_model import find_sd_server
            if find_sd_server() is None:
                return {"level": "error", "text": "stable-diffusion.cpp isn't installed yet - see the setup steps"}
            return {"level": "error", "text": "A model file is missing - pick the files again"}
        st = row.get("state")
        if st == "loaded":
            return {"level": "ok", "text": "Loaded - ready to make images" if row["kind"] == "image_gen"
                    else "Loaded - ready to make videos"}
        if st == "loading":
            return {"level": "idle", "text": "Loading..."}
        if row.get("error"):
            return {"level": "error", "text": f"Failed to load: {row['error']}"}
        return {"level": "idle", "text": "Not loaded - press Load to use it"}
    if row.get("engine") == "whisper" and not row.get("files_ok"):
        from ..small_model import find_whisper_server
        if find_whisper_server() is None:
            return {"level": "error", "text": "Whisper isn't installed yet - see the setup steps"}
        return {"level": "error", "text": "Whisper model file missing - pick a .bin model"}
    if not row.get("files_ok"):
        return {"level": "error", "text": "Model file missing - pick a model file"}
    return ({"level": "ok", "text": "Running"} if row.get("loaded")
            else {"level": "idle", "text": "Sleeping (starts on first use)"})


def _media_view() -> dict:
    from .. import profiles
    from ..small_model import (
        APP_CONFIG,
        find_sd_server,
        find_whisper_server,
        sd_search_dirs,
        whisper_search_dirs,
    )
    m = APP_CONFIG.get("media") or {}
    exe = find_whisper_server()
    return {
        "allow_cloud_audio": allow_cloud_audio(),
        "whisper_found": exe is not None,
        "whisper_dirs": [str(p) for p in whisper_search_dirs()],
        "sd_found": find_sd_server() is not None,
        "sd_dirs": [str(p) for p in sd_search_dirs()],
        "helper_dir": str(profiles.helper_root()),
        "media_dirs": {k: str(profiles.media_root(k)) for k in profiles.MEDIA_DIR_NAMES},
        "limits": m.get("limits") or {},
    }


def public_view(user_id: Optional[int] = None, is_admin: bool = False) -> dict:
    """Everything Settings -> Models needs, in plain language."""
    from ..small_model import APP_CONFIG, small_models
    reg = registry(user_id)
    status = small_models.status()
    sm = APP_CONFIG.get("small_models") or {}
    lanes = []
    for name, d in reg.items():
        row = dict(d)
        if d["local"] and name != "main":
            st = status.get(name) or {}
            cfg = sm.get(name) or {}
            row.update({
                "model": cfg.get("model"), "mmproj": cfg.get("mmproj"), "port": cfg.get("port"),
                "gpu": cfg.get("gpu"), "ctx": cfg.get("ctx"), "idle_unload_s": cfg.get("idle_unload_s"),
                "loaded": st.get("loaded"), "files_ok": st.get("available"), "error": st.get("error")
            })
            if d.get("engine") == "whisper":
                row.update({"threads": cfg.get("threads"), "language": cfg.get("language")})
            elif d.get("engine") == "sdcpp":
                row.update({k: cfg.get(k) for k in SD_FILE_KEYS + (
                    "steps", "cfg_scale", "flow_shift", "sampler", "offload_to_cpu", "vae_tiling", "timeout_s",
                    "edit_refs", "max_refs", "default_size")})
                row.update({"state": st.get("state"), "loading_since": st.get("loading_since")})
                inst = small_models.instances.get(name)
                if inst is not None and hasattr(inst, "edit_caps"):
                    row["edit_caps"] = inst.edit_caps()
        elif name == "main":
            from ..state import state
            row.update({
                "loaded": Target("main").available(),
                "model": (state.profile or {}).get("model_path") if state.profile else None
            })
        if not d["local"]:
            from .. import cloud
            row["cloud_binding"] = (cloud.user_lanes(user_id).get(name) or {}).get("cloud")
        row["status"] = _status_text(row)
        row["used_by"] = [j for j, ln in role_map(user_id).items() if ln == name]
        lanes.append(row)
    return {
        "lanes": lanes,
        "jobs": [{"job": j, **{k: v for k, v in s.items()}} for j, s in JOBS.items()],
        "role_map": role_map(user_id),
        "defaults": {j: default_lane(j, user_id, reg) for j in JOBS},
        "can_edit_local": is_admin,
        "media": _media_view(),
    }

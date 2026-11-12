import asyncio
from fastapi import Depends
from core.auth import Principal
from core.deps import get_current_user
from core import reasoning
from core.profiles import companions, discover_models, load_model_configs
from core.engines import vllm as vllm_engine
from core.profiles.paths import model_display_name
from core.vram.gguf_parser import model_files, model_size_bytes
from core.state import state
from core import cloud

from .base import router
from .config_helpers import _reasoning_caps, check_tool_calling


@router.get("/control/profiles")
async def profiles(user: Principal = Depends(get_current_user)):
    # Models/<model name>/*.gguf (one folder per model) plus legacy flat files;
    # companions are header-checked (core/profiles.py companions())
    def _scan():
        out = []
        saved = load_model_configs()
        for gfile, folder in discover_models():
            fmt = vllm_engine.model_format(gfile)
            if fmt == "hf":                       # a Hugging Face model folder: vLLM only, no companion files
                comp = {"mtp": None, "mmproj": None, "mtp_note": "", "mmproj_note": ""}
                size_gb = round(vllm_engine.folder_size_bytes(gfile) / (1024**3), 1)
                parts = 1
            else:
                comp = companions(gfile)
                try:
                    size_gb = round(model_size_bytes(gfile) / (1024**3), 1)      # all parts of a split model
                except OSError:
                    size_gb = None
                parts = len(model_files(gfile)) or 1
            mtp, mmproj = comp["mtp"], comp["mmproj"]
            saved_engine = (saved.get(gfile.name.lower()) or {}).get("engine")
            engines = vllm_engine.engines_for(gfile)
            out.append({
                "format": fmt,
                "engines": engines,
                "engine": saved_engine if saved_engine in engines else (engines[0] if engines else "llama"),
                "type": "model",
                "name": gfile.name,
                "folder": folder,
                "display": (folder or gfile.stem) if fmt != "hf" else vllm_engine.display_name(gfile),
                "parts": parts,
                "path": str(gfile),
                "model_path": str(gfile),
                "model_exists": True,
                "mtp_available": bool(mtp),
                "mtp_draft_path": str(mtp) if mtp else None,
                "mtp_note": comp["mtp_note"],
                "mmproj_available": bool(mmproj),
                "mmproj_path": str(mmproj) if mmproj else None,
                "mmproj_note": comp["mmproj_note"],
                "tools_available": check_tool_calling(gfile.name),
                **_reasoning_caps(reasoning.local_mode(gfile)),
                "size_gb": size_gb,
                "family": folder or gfile.stem.split("-")[0],
            })
        return out

    # header reads touch every model file: keep them off the event loop
    models_out = await asyncio.to_thread(_scan)

    return {"models": models_out, "profiles": [],
            "cloud": [{
                "type": "cloud",
                "provider": cm.provider,
                "provider_name": cm.provider_name,
                "key": cm.key,
                "value": f"cloud:{cm.key}",
                "model": cm.model_id,
                "name": cm.display,
                "display": cm.display,
                "size_gb": None,
                "ctx": cm.ctx,
                "tools_available": True,
                **_reasoning_caps(reasoning.cloud_mode(cm)),
                "vision_capable": any(k in (cm.model_id or "").lower() for k in ["vision", "4o", "gemini", "claude-3", "vl"]),
            } for cm in cloud.cloud_models(user.id)]}


@router.get("/control/available_models")
async def available_models(user: Principal = Depends(get_current_user)):
    """Read-only trimmed model list for the nav bar: shows the currently loaded local model
    (only when an admin has loaded one into GPU VRAM AND it is actively running), followed by cloud models clustered by provider."""
    is_running = state.process is not None and state.process.poll() is None
    loaded_name = state.profile.get("name") if (state.profile and is_running) else None
    loaded_path = (state.profile.get("model_path") or state.profile.get("path") or loaded_name) if (state.profile and is_running) else None

    local_models = []
    # Local model only shows when an admin has loaded one into GPU VRAM and it is actively running
    if loaded_name and is_running:
        # template header read (cached per file): drives the composer's effort chip
        local_mode = await asyncio.to_thread(reasoning.local_mode, loaded_path)
        clean_name = model_display_name(loaded_name)      # no folders, ".gguf" or split-part suffix
        local_models.append({
            "id": loaded_path or loaded_name,
            "display": clean_name,
            "currently_loaded": True,
            "kind": "local",
            "provider": "local",
            "provider_name": "Local",
            "engine": state.engine,
            **_reasoning_caps(local_mode),
        })

    cloud_models = []
    bound_main = cloud.cloud_lane("main", user.id)
    for cm in cloud.cloud_models(user.id):
        is_active = bool(bound_main and bound_main.key == cm.key)
        cloud_models.append({
            "id": f"cloud:{cm.key}",
            "display": cm.display,
            "currently_loaded": is_active,
            "kind": "cloud",
            "ctx": cm.ctx,
            "context_size": cm.ctx,
            "provider": cm.provider,
            "provider_name": cm.provider_name,
            **_reasoning_caps(reasoning.cloud_mode(cm)),
        })

    return {"models": local_models + cloud_models}

"""
routes/lanes.py - Settings -> Models: lanes ("models"), job routing, answer check.

  GET    /control/lanes                   everything the page needs (plain-language)
  POST   /control/lanes                   add/edit a model (local: admin; cloud: own)
  DELETE /control/lanes?name=&move_to=    remove a model, moving its jobs elsewhere
  POST   /control/lanes/roles             map jobs to models (as_default: admin)
  POST   /control/lanes/{name}/test       one tiny request, plain-language result
  POST   /control/lanes/{name}/load       load an image/video model (sd.cpp) - never automatic
  POST   /control/lanes/{name}/stop       unload a local model now
  GET    /control/lanes/files             model files in Models/orchestrator (admin)
  POST   /control/verification            answer-check settings (per user)

Local models run on shared hardware, so creating/editing them needs
model.local.configure; cloud models and job choices are each user's own.
"""

import asyncio
import re
import time
from typing import Optional

from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from core import cloud, lanes, verifier
from core.audit import audit_log
from core.auth import Principal, user_has_permission
from core.config import ACTIVE_RUNTIME
from core.deps import get_current_user

router = APIRouter(tags=["lanes"])

_LOCAL_PERM = "model.local.configure"


def _view(user: Principal) -> dict:
    out = lanes.public_view(user.id, is_admin=user_has_permission(user, _LOCAL_PERM))
    out["cloud_models"] = [{"key": cm.key, "display": cm.display, "provider": cm.provider_name}
                           for cm in cloud.cloud_models(user.id)]
    out["gpus"] = list(ACTIVE_RUNTIME.get("gpu_devices") or [])
    out["suggested_port"] = lanes.suggest_port()
    out["verification"] = verifier.settings(user.id)
    out["fallback_local"] = bool(cloud.cloud_bindings(user.id).get("fallback_local", True))
    return out


_FILES_CACHE = {"t": 0.0, "items": None}


def _helper_files() -> dict:
    """Model files in Models/orchestrator for the add/edit wizard (60 s cache)."""
    from core import profiles
    if _FILES_CACHE["items"] is not None and time.time() - _FILES_CACHE["t"] < 60:
        return _FILES_CACHE["items"]
    items = profiles.discover_helper_files()
    _FILES_CACHE.update(t=time.time(), items=items)
    return items


def _err(msg: str, code: int = 400):
    return JSONResponse({"error": msg}, status_code=code)


@router.get("/control/lanes")
async def lanes_get(user: Principal = Depends(get_current_user)):
    return _view(user)


@router.get("/control/lanes/files")
async def lanes_files(fresh: bool = False, user: Principal = Depends(get_current_user)):
    """Helper / whisper / image / video model files (admins only: they pick local models)."""
    if not user_has_permission(user, _LOCAL_PERM):
        return _err("Only an admin can add models on this PC.", 403)
    if fresh:
        _FILES_CACHE["items"] = None
    return await asyncio.to_thread(_helper_files)


class LaneReq(BaseModel):
    name: str = Field(max_length=32)
    backend: str = "local"                 # local | cloud
    kind: Optional[str] = None             # chat | vision | embed
    label: Optional[str] = Field(default=None, max_length=40)
    fallback: Optional[str] = None
    # local
    model: Optional[str] = None
    mmproj: Optional[str] = None
    port: Optional[int] = None
    gpu: Optional[int] = None
    ctx: Optional[int] = None
    idle_unload_s: Optional[int] = None
    # cloud
    cloud: Optional[str] = None
    jobs: Optional[list[str]] = None       # wizard step 3: map these jobs to it
    timeout_s: Optional[int] = None
    # speech to text (whisper.cpp)
    threads: Optional[int] = None
    language: Optional[str] = Field(default=None, max_length=8)
    # image/video on this PC (stable-diffusion.cpp): engine "sdcpp"
    engine: Optional[str] = Field(default=None, max_length=16)
    diffusion_model: Optional[str] = Field(default=None, max_length=400)
    llm: Optional[str] = Field(default=None, max_length=400)
    vae: Optional[str] = Field(default=None, max_length=400)
    llm_vision: Optional[str] = Field(default=None, max_length=400)
    edit_refs: Optional[bool] = None
    max_refs: Optional[int] = None
    default_size: Optional[str] = Field(default=None, max_length=8)
    steps: Optional[int] = None
    cfg_scale: Optional[float] = None
    flow_shift: Optional[float] = None
    sampler: Optional[str] = Field(default=None, max_length=24)
    offload_to_cpu: Optional[bool] = None
    vae_tiling: Optional[bool] = None


@router.post("/control/lanes")
async def lanes_save(req: LaneReq, user: Principal = Depends(get_current_user)):
    name = req.name.strip().lower()
    data = req.model_dump(exclude_unset=True)
    reg = lanes.registry(user.id)
    is_local = req.backend == "local" or (name in reg and reg[name]["local"])
    try:
        if is_local:
            if not user_has_permission(user, _LOCAL_PERM):
                audit_log(user, action="lanes.local.save", resource=name, result="deny")
                return _err("Only an admin can add or change models that run on this PC. "
                            "You can add your own cloud models.", 403)
            lanes.save_local_lane(name, data)
        else:
            lanes.save_user_lane(user.id, name, data)
        if req.jobs:
            lanes.set_role_map(user.id, {j: name for j in req.jobs})
    except ValueError as e:
        return _err(str(e))
    # never the auth header in the audit trail
    audit_log(user, action="lanes.save", resource=name,
              detail={"backend": "local" if is_local else "cloud", "kind": req.kind}, result="allow")
    return {"ok": True, **_view(user)}


@router.delete("/control/lanes")
async def lanes_delete(name: str, move_to: Optional[str] = None,
                       user: Principal = Depends(get_current_user)):
    reg = lanes.registry(user.id)
    d = reg.get(name)
    if d is None:
        return _err(f"there is no model called '{name}'", 404)
    if move_to and move_to not in reg:
        return _err(f"there is no model called '{move_to}'")
    try:
        if d["owner"] == "user":
            lanes.delete_user_lane(user.id, name, move_to)
        else:
            if not user_has_permission(user, _LOCAL_PERM):
                return _err("Only an admin can remove models that run on this PC.", 403)
            lanes.delete_local_lane(name, move_to)
    except ValueError as e:
        return _err(str(e))
    audit_log(user, action="lanes.delete", resource=name, detail={"move_to": move_to}, result="allow")
    return {"ok": True, **_view(user)}


class RolesReq(BaseModel):
    map: dict[str, Optional[str]]
    as_default: bool = False


@router.post("/control/lanes/roles")
async def lanes_roles(req: RolesReq, user: Principal = Depends(get_current_user)):
    if req.as_default and not user_has_permission(user, _LOCAL_PERM):
        return _err("Only an admin can change the default for everyone.", 403)
    try:
        lanes.set_role_map(user.id, req.map, as_default=req.as_default)
    except ValueError as e:
        return _err(str(e))
    audit_log(user, action="lanes.roles", detail={"map": req.map, "as_default": req.as_default},
              result="allow")
    return {"ok": True, **_view(user)}


def _plain_error(e: Exception) -> str:
    s = str(e)
    low = s.lower()
    m = re.search(r"exited code -?\d+ - (.+?)\)?$", s)
    if m:                                   # the server said why it stopped
        return f"The model server stopped: {m.group(1)}"
    if "401" in s or "403" in s:
        return "The API key was rejected - check it in Cloud providers."
    if "404" in s:
        return "The provider doesn't know that model name."
    if "timed out" in low or "timeout" in low:
        return "It took too long to answer - the model may be too big for this GPU, or the provider is slow."
    if "not fit" in low or "vram" in low or "preflight" in low:
        return "Not enough free GPU memory for this model - pick a smaller model or another GPU."
    if "missing" in low or "not configured" in low:
        return "The model file is missing - pick a model file."
    if "whisper-server not found" in low:
        return "Whisper isn't installed yet - see the setup steps in the model's card."
    if "sd-server not found" in low:
        return "stable-diffusion.cpp isn't installed yet - see the setup steps in the model's card."
    if "isn't loaded" in low:
        return "The model isn't loaded - press Load first."
    if "exited code" in low or "failed to start" in low:
        return "The model server failed to start - check the model file and context size."
    return s[:200]


def _silent_wav(seconds: float = 1.0, rate: int = 16000) -> bytes:
    import struct
    n = int(seconds * rate)
    return (b"RIFF" + struct.pack("<I", 36 + n * 2) + b"WAVEfmt "
            + struct.pack("<IHHIIHH", 16, 1, 1, rate, rate * 2, 2, 16)
            + b"data" + struct.pack("<I", n * 2) + bytes(n * 2))


async def _test_media(name: str, d: dict, user: Principal, full: bool) -> dict:
    """Plain-language test of an image / video / speech model."""
    import base64
    from core import media
    t0 = time.time()
    kind = d["kind"]
    where = "cloud" if d["cloud_key"] else "local"

    def _ms():
        return int((time.time() - t0) * 1000)
    try:
        if d["cloud_key"]:
            # a real cloud generation costs money: only check the key and the model name
            cm = cloud.cloud_lane(name, user.id)
            c = cloud._client_for(cm)
            if cm.is_google:
                r = await c.get(f"https://generativelanguage.googleapis.com/v1beta/models/{cm.model_id}",
                                headers=media._cloud_headers(cm), timeout=30)
            else:
                r = await c.get(cm.url(f"/models/{cm.model_id}"), headers=media._cloud_headers(cm), timeout=30)
                if r.status_code == 404:        # some providers only list models
                    r = await c.get(cm.url("/models"), headers=media._cloud_headers(cm), timeout=30)
            r.raise_for_status()
            return {"ok": True, "ms": _ms(), "where": where,
                    "sample": "The key works and the provider knows this model. Nothing was generated, "
                              "so this test was free."}
        from core.small_model import small_models
        inst = small_models.instances.get(name)
        if inst is None:
            return {"ok": False, "error": "This model isn't configured.", "where": where}
        if kind == "stt":
            if not user_has_permission(user, "model.local.load"):
                return {"ok": False, "error": "You don't have permission to start local models.", "where": where}
            text = await inst.transcribe(_silent_wav())
            heard = repr(text[:60]) if text.strip() else "no words, as expected"
            return {"ok": True, "ms": _ms(), "where": where,
                    "sample": f"Whisper is working (a silent test clip gave {heard})."}
        if not media._is_sdcpp(inst):
            return {"ok": False, "error": "This isn't an image/video model.", "where": where}
        if not inst.is_up():
            return {"ok": False, "where": where, "needs_load": True,
                    "error": "Load the model first (the Load button), then test it."}
        if kind == "video_gen" and not full:
            return {"ok": True, "ms": _ms(), "where": where, "needs_confirm": True,
                    "sample": "The model is loaded. A full test makes a real short video and can take minutes."}
        blobs = await media.sdcpp_generate(inst, "image" if kind == "image_gen" else "video",
                                           "a small red circle on a white background",
                                           {"width": 512, "height": 512, "seconds": 1,
                                            "steps": min(int(inst.cfg.get("steps") or 20), 8)})
        data = blobs[0]
        mime = (media.sniff(data) or ("application/octet-stream", ""))[0]
        out = {"ok": True, "ms": _ms(), "where": where,
               "sample": f"Got {'an image' if mime.startswith('image/') else 'a video'} back "
                         f"({len(data) // 1024} KB, {mime})."}
        if mime.startswith("image/") and len(data) <= 6 * 1024 * 1024:
            out["image"] = f"data:{mime};base64,{base64.b64encode(data).decode()}"
        return out
    except Exception as e:
        plain = (_plain_error(e) if isinstance(e, RuntimeError) and not isinstance(e, media.MediaError)
                 else media.plain_error(e))
        return {"ok": False, "error": plain, "where": where}


@router.post("/control/lanes/{name}/test")
async def lanes_test(name: str, full: bool = False, user: Principal = Depends(get_current_user)):
    reg = lanes.registry(user.id)
    d = reg.get(name)
    if d is None:
        return _err(f"there is no model called '{name}'", 404)
    if d["kind"] in lanes.MEDIA_KINDS:
        return await _test_media(name, d, user, full)
    if d["cloud_key"]:
        cm = cloud.cloud_lane(name, user.id)
        r = await cloud.probe(cm, "Reply with the single word: ready")
        if r.get("ok"):
            return {"ok": True, "ms": r.get("ms"), "sample": r.get("sample"), "where": "cloud"}
        return {"ok": False, "error": _plain_error(Exception(r.get("error") or "")), "where": "cloud"}
    if d["local"] and name != "main" and not user_has_permission(user, "model.local.load"):
        return _err("You don't have permission to start local models.", 403)
    t = lanes.Target(name)
    t0 = time.time()
    try:
        c = await t.client()
        if d["kind"] == "embed":
            r = await c.post("/v1/embeddings", json={"input": ["ready"]}, timeout=60)
            r.raise_for_status()
            dim = len(((r.json().get("data") or [{}])[0]).get("embedding") or [])
            sample = f"embedding size {dim}"
        else:
            r = await c.post("/v1/chat/completions", json={
                "messages": [{"role": "user", "content": "Reply with the single word: ready"}],
                "max_tokens": 16, "temperature": 0}, timeout=60)
            r.raise_for_status()
            sample = lanes.message_text(r.json()).strip()[:200]
    except Exception as e:
        return {"ok": False, "error": _plain_error(e), "where": "local"}
    return {"ok": True, "ms": int((time.time() - t0) * 1000), "sample": sample, "where": "local"}


_LOAD_TASKS: set = set()


async def _bg_load(inst) -> None:
    try:
        await inst.load()
    except Exception as e:
        print(f"[lanes] loading {inst.role} failed: {type(e).__name__}: {str(e)[:200]}")


@router.post("/control/lanes/{name}/load")
async def lanes_load(name: str, user: Principal = Depends(get_current_user)):
    """Load a local image/video model (stable-diffusion.cpp). The only way it starts:
    a request never loads it. Returns at once; the page polls /control/lanes."""
    from core.small_model import small_models
    from core import media
    if not user_has_permission(user, "model.local.load"):
        audit_log(user, action="lanes.load", resource=name, result="deny")
        return _err("You don't have permission to load models on this PC - ask an admin.", 403)
    inst = small_models.instances.get(name)
    if inst is None:
        return _err(f"'{name}' is not a local model", 404)
    if not media._is_sdcpp(inst):
        return _err("Only image and video models on this PC are loaded by hand - "
                    "the others start by themselves when needed.")
    if not inst.is_up() and not inst.loading_since:
        # one image model at a time: another loaded (or loading) one must be unloaded first
        if inst.kind == "image_gen":
            other = next((o for n, o in small_models.instances.items()
                          if n != name and media._is_sdcpp(o) and o.kind == "image_gen"
                          and (o.is_up() or o.loading_since)), None)
            if other is not None:
                label = other.cfg.get("label") or other.role
                return _err(f"'{label}' is already {'loaded' if other.is_up() else 'loading'} - "
                            "only one image model can be loaded at a time. Unload it first.", 409)
        if not inst.available:
            from core.small_model import find_sd_server
            return _err("stable-diffusion.cpp isn't installed yet - see the setup steps."
                        if find_sd_server() is None else "A model file is missing - pick the files again.")
        inst.loading_since = time.time()          # visible as "loading" right away
        t = asyncio.create_task(_bg_load(inst))
        _LOAD_TASKS.add(t)
        t.add_done_callback(_LOAD_TASKS.discard)
        audit_log(user, action="lanes.load", resource=name, result="allow")
    return {"ok": True, **_view(user)}


@router.post("/control/lanes/{name}/stop")
async def lanes_stop(name: str, user: Principal = Depends(get_current_user)):
    from core.small_model import small_models
    if not user_has_permission(user, "model.local.load"):
        return _err("You don't have permission to stop local models.", 403)
    inst = small_models.instances.get(name)
    if inst is None:
        return _err(f"'{name}' is not a local helper model", 404)
    if getattr(inst, "busy", 0):
        return _err("It's making something right now - wait for it to finish or cancel it first.", 409)
    if inst.is_up():
        inst._stop()
    audit_log(user, action="lanes.stop", resource=name, result="allow")
    return {"ok": True, **_view(user)}


class MediaSettingsReq(BaseModel):
    allow_cloud_audio: Optional[bool] = None
    image_per_day: Optional[int] = Field(default=None, ge=0, le=10000)
    video_per_day: Optional[int] = Field(default=None, ge=0, le=1000)


@router.post("/control/media-settings")
async def media_settings_save(req: MediaSettingsReq, user: Principal = Depends(get_current_user)):
    """Admin: the cloud speech switch + daily cloud generation limits (app.json "media")."""
    if not user_has_permission(user, _LOCAL_PERM):
        return _err("Only an admin can change these settings.", 403)
    from core.config import update_app_config
    from core.small_model import APP_CONFIG
    upd = req.model_dump(exclude_none=True)

    def _apply(m: dict) -> None:
        if "allow_cloud_audio" in upd:
            m["allow_cloud_audio"] = upd["allow_cloud_audio"]
        for k in ("image_per_day", "video_per_day"):
            if k in upd:
                m.setdefault("limits", {})[k] = upd[k]
    update_app_config(lambda cfg: _apply(cfg.setdefault("media", {})))
    _apply(APP_CONFIG.setdefault("media", {}))
    audit_log(user, action="media.settings", detail=upd, result="allow")
    return {"ok": True, **_view(user)}


class VerifyReq(BaseModel):
    mode: Optional[str] = None
    apply_to: Optional[str] = None
    max_rounds: Optional[int] = Field(default=None, ge=0, le=3)
    min_length: Optional[int] = Field(default=None, ge=0, le=100000)


@router.post("/control/verification")
async def verification_save(req: VerifyReq, user: Principal = Depends(get_current_user)):
    cur = dict(cloud.verification(user.id))
    upd = req.model_dump(exclude_none=True)
    if "mode" in upd and upd["mode"] not in verifier.MODES:
        return _err("mode must be off, badge or gate")
    if "apply_to" in upd and upd["apply_to"] not in verifier.APPLY_TO:
        return _err("apply_to must be both, chat or agent")
    cur.update(upd)
    cloud.write_user_section(user.id, "verification", cur)
    audit_log(user, action="verification.save", detail=upd, result="allow")
    return {"ok": True, **_view(user)}

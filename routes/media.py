"""
routes/media.py - images, videos and speech to text (core/media.py).

  GET    /media/status            which of image / video / transcribe are set up
  POST   /media/generate          start an image or video job -> {job_id}
  GET    /media/jobs/{id}         SSE: queued -> progress -> done | error
  DELETE /media/jobs/{id}         cancel (stops waiting; the service may finish anyway)
  POST   /media/transcribe        multipart WAV -> {text}

A cloud video can cost money, so the first request for one answers 409
{needs_confirm} and the client asks the user before sending confirm_cost.

Pictures (mode img2img = change one picture, edit = combine references) only go
to the image model on this PC; they are checked and re-encoded first
(core/media_images.py) and never stored or logged.
"""

import json
from typing import Optional

from fastapi import APIRouter, Depends, File as FastAPIFile, Form, UploadFile
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel, Field

from core import lanes, media, media_images
from core.audit import audit_log
from core.auth import Principal, user_has_permission
from core.deps import get_current_user

router = APIRouter(tags=["media"])


def _err(msg: str, code: int = 400, **extra):
    return JSONResponse({"error": msg, **extra}, status_code=code)


@router.get("/media/status")
async def media_status(user: Principal = Depends(get_current_user)):
    return media.status(user.id)


class PictureIn(BaseModel):
    b64: Optional[str] = Field(default=None, max_length=14 * 1024 * 1024)
    path: Optional[str] = Field(default=None, max_length=400)


class GenerateReq(BaseModel):
    kind: str = Field(pattern="^(image|video)$")
    prompt: str = Field(min_length=1, max_length=4000)
    negative: Optional[str] = Field(default=None, max_length=2000)
    aspect: Optional[str] = Field(default=None, pattern="^(square|wide|tall)$")
    size: Optional[str] = Field(default=None, pattern="^(small|medium|large|xlarge)$")
    width: Optional[int] = Field(default=None, ge=64, le=4096)
    height: Optional[int] = Field(default=None, ge=64, le=4096)
    seconds: Optional[int] = Field(default=None, ge=1, le=60)
    seed: Optional[int] = Field(default=None, ge=0, le=2 ** 31 - 1)
    lane: Optional[str] = Field(default=None, max_length=32)
    confirm_cost: bool = False
    mode: Optional[str] = Field(default=None, pattern="^(img2img|edit)$")
    images: Optional[list[PictureIn]] = Field(default=None, max_length=media_images.MAX_INPUTS)
    strength: Optional[float] = Field(default=None, ge=0.05, le=1.0)


def _first_target(job: str, user_id: int, lane: Optional[str]):
    route = [t for t in lanes.targets(job, user_id) if t.available()]
    if lane:
        route.sort(key=lambda t: t.lane != lane)
    return route[0] if route else None


@router.post("/media/generate")
async def media_generate(req: GenerateReq, user: Principal = Depends(get_current_user)):
    job_name = media.KINDS[req.kind]
    if req.lane:
        err = lanes.validate_mapping(job_name, req.lane, user.id)
        if err:
            return _err(err)
    pics = []
    if req.images or req.mode:
        if req.kind != "image" or not req.images or not req.mode:
            return _err("Pictures need a mode (change one picture or combine pictures), for images only.")
        route = media.edit_targets(user.id, req.mode, req.lane)
        if not route:
            return _err(media.edit_blocked(user.id, req.mode), 409, needs_edit_model=True)
        limit = 1 if req.mode == "img2img" else route[0].inst.edit_caps()["max_refs"]
        try:
            pics = media_images.prepare_inputs([i.model_dump(exclude_none=True) for i in req.images], limit)
        except media_images.InputError as e:
            return _err(str(e))
        first = route[0]
    else:
        first = _first_target(job_name, user.id, req.lane)
    if first is None:
        return _err(f"{lanes.JOBS[job_name]['label']} isn't set up yet - add a model in "
                    "Settings -> Models & Jobs.", 409, not_setup=True)
    inst = first.inst
    if media._is_sdcpp(inst) and not inst.is_up():
        # the local image/video model is loaded by hand only; with no backup, say so up front
        route = (media.edit_targets(user.id, req.mode, req.lane) if pics
                 else [t for t in lanes.targets(job_name, user.id) if t.available()])
        if len(route) <= 1:
            label = lanes.registry(user.id).get(first.lane, {}).get("label") or first.lane
            return _err(str(media.NotLoadedError(first.lane, label, req.kind)), 409,
                        not_loaded={"lane": first.lane, "label": label,
                                    "loading": bool(inst.loading_since),
                                    "can_load": user_has_permission(user, "model.local.load")})
    if req.kind == "video" and first.is_cloud and not req.confirm_cost:
        return _err("This video is made in the cloud and may cost money.", 409, needs_confirm=True,
                    provider=first.cm.provider_name, model=first.cm.display)
    opts = req.model_dump(include={"negative", "aspect", "size", "width", "height", "seconds", "seed"},
                          exclude_none=True)
    if pics:
        opts.update(inputs=pics, mode=req.mode, strength=req.strength)
    try:
        job = media.start_job(user, req.kind, req.prompt, opts, req.lane)
    except media.MediaError as e:
        return _err(str(e), 429)
    return {"job_id": job.id, "kind": req.kind,
            "model": lanes.registry(user.id).get(first.lane, {}).get("label"),
            "cloud": first.is_cloud, "on_pc": media._is_sdcpp(first.inst)}


@router.get("/media/jobs/{job_id}")
async def media_job_events(job_id: str, user: Principal = Depends(get_current_user)):
    job = media.get_job(job_id, user.id)
    if job is None:
        return _err("That job isn't here any more (jobs are kept for 30 minutes).", 404)

    async def _stream():
        async for ev, data in media.job_events(job):
            yield f"event: {ev}\ndata: {json.dumps(data)}\n\n"

    return StreamingResponse(_stream(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@router.delete("/media/jobs/{job_id}")
async def media_job_cancel(job_id: str, user: Principal = Depends(get_current_user)):
    return {"ok": media.cancel_job(job_id, user.id)}


@router.post("/media/transcribe")
async def media_transcribe(file: UploadFile = FastAPIFile(...), language: Optional[str] = Form(default=None),
                           user: Principal = Depends(get_current_user)):
    cap = int(media.media_cfg().get("max_audio_mb") or 25) * 1024 * 1024
    data = await file.read(cap + 1)
    if len(data) > cap:
        return _err(f"That recording is too long ({cap // (1024 * 1024)} MB max).", 413)
    try:
        res = await media.transcribe(user, data, language)
    except media.MediaError as e:
        return _err(str(e), 409 if "isn't set up" in str(e) else 400)
    # metadata only: never the audio or the words
    audit_log(user, action="media.transcribe", resource=res["source"],
              detail={"bytes": len(data), "chars": len(res["text"]), "ms": res["ms"]}, result="allow")
    return res

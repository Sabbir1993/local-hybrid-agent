"""routes/speech_corpus.py - the voice-collection page (/voice) and its admin controls (/control/speech/*).

Public part: reachable without an account, gated by the shared access code in the link (/voice?k=<code>) and by the
admin's on/off switch (everything answers 404 while it is off). /start trades the code + name + employee ID for a
short-lived session token; uploads carry that token. Listed in tests/test_auth_invariant.py PUBLIC_HTTP.

Admin part: every route carries require_permission("speech.manage") itself (the router is included without
router-level auth because of the public part); deleting a speaker also needs the MFA step-up.
"""
import hashlib
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, Depends, File as FastAPIFile, Form, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel, Field

from core.audit import audit_log
from core.auth import Principal
from core.deps import require_permission, require_verified
from core.limits import buckets
from core.speech_corpus import prompts as corpus_prompts
from core.speech_corpus import settings as corpus_settings
from core.speech_corpus.audio import ClipError, check_clip
from core.speech_corpus.store import AGE_BANDS, DEVICES, GENDERS, REGIONS, CorpusError, get_store

router = APIRouter()

_manage = require_permission("speech.manage")
_manage_verified = require_verified("speech.manage")

MAX_UPLOAD_BYTES = 1536 * 1024          # 30 s of 16 kHz mono 16-bit is ~960 KB
START_PER_MIN_PER_IP = 30               # the whole office may share one public IP
BAD_CODE_PER_MIN_PER_IP = 10
UPLOADS_PER_MIN_PER_SESSION = 30


def _err(msg: str, code: int = 400):
    return JSONResponse({"error": msg}, status_code=code)


def _not_found():
    return JSONResponse({"error": "Not found"}, status_code=404)


def _ip(request: Request) -> str:
    return request.client.host if request.client else "?"


def _store():
    """The open store, or None when collection is off or the folder is unusable."""
    if not corpus_settings.is_enabled():
        return None
    try:
        return get_store(corpus_settings.corpus_dir())
    except corpus_settings.CorpusConfigError:
        return None


# ---------------------------------------------------------------- public: the page and its API

@router.get("/voice")
async def voice_page():
    if _store() is None:
        return _not_found()
    from routes.pages import _react_index
    return _react_index()


@router.get("/media/voice-collect/options")
async def voice_options():
    """Choices for the form (labels only), so the page and the server never disagree."""
    if _store() is None:
        return _not_found()
    return {"regions": REGIONS, "genders": sorted(GENDERS - {""}), "age_bands": sorted(AGE_BANDS - {""}),
            "devices": sorted(DEVICES - {""}), "lines_per_session": corpus_settings.lines_per_session(),
            "max_clip_seconds": corpus_settings.max_clip_seconds(), **corpus_settings.window()}


def _window_refusal():
    """A JSON 403 when the link's open window is not now, else None. Carries the dates so the page can say them."""
    w = corpus_settings.window()
    if w["state"] == "open":
        return None
    msg = ("This link is not open yet." if w["state"] == "not_yet"
           else "This link has expired. Ask for a new link or a later date.")
    return JSONResponse({"error": msg, **w}, status_code=403)


class StartBody(BaseModel):
    code: str = Field(default="", max_length=64)
    name: str = Field(default="", max_length=120)
    employee_id: str = Field(default="", max_length=40)
    region: str = Field(default="", max_length=40)
    district: str = Field(default="", max_length=60)
    gender: str = Field(default="", max_length=10)
    age_band: str = Field(default="", max_length=10)
    device: str = Field(default="", max_length=20)
    consent: bool = False


@router.post("/media/voice-collect/start")
async def voice_start(body: StartBody, request: Request):
    store = _store()
    if store is None:
        return _not_found()
    ip = _ip(request)
    closed = _window_refusal()
    if closed is not None:
        return closed
    if not buckets.allow("voice-start:" + ip, START_PER_MIN_PER_IP):
        return _err("Too many people are starting at once. Please wait a minute and try again.", 429)
    if not corpus_settings.code_ok(body.code):
        if not buckets.allow("voice-badcode:" + ip, BAD_CODE_PER_MIN_PER_IP):
            return _err("Too many wrong links. Please wait a minute.", 429)
        return _err("This link is not valid any more. Ask for the current link.", 403)
    if not body.consent:
        return _err("Please agree to the consent statement first.")
    try:
        c = store.upsert_contributor(name=body.name, employee_id=body.employee_id, region=body.region,
                                     district=body.district, gender=body.gender, age_band=body.age_band,
                                     device=body.device)
        if store.sessions_today(c["id"]) >= corpus_settings.sessions_per_day():
            return _err("You have recorded a lot today - thank you! Please come back tomorrow.", 429)
        lines = corpus_prompts.pick(store, c["id"], corpus_settings.lines_per_session())
        token = store.open_session(c["id"], [p["id"] for p in lines])
    except CorpusError as e:
        return _err(str(e), e.status)
    return {"token": token, "speaker_code": c["speaker_code"], "instruction": "read" if c["region"] == "standard" else "dialect",
            "lines": [{"id": p["id"], "text": p["text"]} for p in lines],
            "max_clip_seconds": corpus_settings.max_clip_seconds()}


@router.post("/media/voice-collect/recording")
async def voice_recording(token: str = Form(default=""), prompt_id: int = Form(default=0),
                          file: UploadFile = FastAPIFile(...)):
    store = _store()
    if store is None:
        return _err("Voice collection is closed right now.", 404)
    key = "voice-up:" + hashlib.sha256(token.encode("utf-8", "replace")).hexdigest()[:24]
    if not buckets.allow(key, UPLOADS_PER_MIN_PER_SESSION):
        return _err("Slow down a little, then try again.", 429)
    data = await file.read(MAX_UPLOAD_BYTES + 1)
    if len(data) > MAX_UPLOAD_BYTES:
        return _err("The recording is too large. Please record a shorter take.", 413)
    try:
        store.session_for(token)                    # a dead session is told so before the audio is judged
        info = check_clip(data, corpus_settings.max_clip_seconds())
        return store.save_recording(token, int(prompt_id), data, info)
    except ClipError as e:
        return _err(str(e), 422)
    except CorpusError as e:
        return _err(str(e), e.status)


class FinishBody(BaseModel):
    token: str = Field(default="", max_length=100)


@router.post("/media/voice-collect/finish")
async def voice_finish(body: FinishBody):
    store = _store()
    if store is None:
        return _not_found()
    try:
        return store.finish_session(body.token)
    except CorpusError as e:
        return _err(str(e), e.status)


# ---------------------------------------------------------------- admin

def _status() -> dict:
    c = corpus_settings.cfg()
    folder_error = None
    folder = str(c.get("dir") or "")
    try:
        folder = str(corpus_settings.corpus_dir())
    except corpus_settings.CorpusConfigError as e:
        folder_error = str(e)
    code = corpus_settings.current_code()      # only ever returned to speech.manage holders (every caller is admin)
    return {"enabled": corpus_settings.is_enabled(), "has_code": corpus_settings.has_code(), "path": "/voice",
            "link_path": f"/voice?k={code}" if code else None,
            "dir": folder, "dir_error": folder_error, "lines_per_session": corpus_settings.lines_per_session(),
            "max_clip_seconds": corpus_settings.max_clip_seconds(), "sessions_per_day": corpus_settings.sessions_per_day(),
            **corpus_settings.window()}


@router.get("/control/speech/status")
async def speech_status(user: Principal = Depends(_manage)):
    return _status()


class EnabledBody(BaseModel):
    enabled: bool


@router.post("/control/speech/enabled")
async def speech_enabled(body: EnabledBody, request: Request, user: Principal = Depends(_manage)):
    try:
        corpus_settings.set_enabled(body.enabled)
    except corpus_settings.CorpusConfigError as e:
        audit_log(user, action="speech.enable", result="deny", detail={"enabled": body.enabled, "reason": str(e)},
                  ip=_ip(request))
        return _err(str(e), 409)
    audit_log(user, action="speech.enable", detail={"enabled": body.enabled}, ip=_ip(request))
    return _status()


class WindowBody(BaseModel):
    valid_from: Optional[float] = None      # epoch seconds UTC; null / 0 = no start limit
    valid_until: Optional[float] = None     # epoch seconds UTC; null / 0 = no end


@router.post("/control/speech/window")
async def speech_window(body: WindowBody, request: Request, user: Principal = Depends(_manage)):
    try:
        w = corpus_settings.set_window(body.valid_from, body.valid_until)
    except corpus_settings.CorpusConfigError as e:
        return _err(str(e), 400)
    audit_log(user, action="speech.window", detail={"valid_from": w["valid_from"], "valid_until": w["valid_until"]},
              ip=_ip(request))
    return _status()


@router.post("/control/speech/code")
async def speech_code(request: Request, user: Principal = Depends(_manage)):
    code = corpus_settings.rotate_code()
    audit_log(user, action="speech.code_rotate", ip=_ip(request))          # never the code itself
    st = _status()
    # link_saved: whether the keychain kept it (the panel can show it again later) - the link works either way
    return {**st, "code": code, "link_path": f"/voice?k={code}", "link_saved": st["link_path"] is not None}


def _admin_store():
    return get_store(corpus_settings.corpus_dir())


@router.get("/control/speech/stats")
async def speech_stats(user: Principal = Depends(_manage)):
    try:
        return _admin_store().stats()
    except corpus_settings.CorpusConfigError as e:
        return _err(str(e), 409)


SAMPLE_PROMPTS = Path(__file__).resolve().parents[1] / "scripts" / "stt_corpus_prompts" / "starter.jsonl"


@router.get("/control/speech/prompts/sample")
async def speech_prompts_sample(user: Principal = Depends(_manage)):
    """The starter set as a download: a ready example of the import format (and a usable first batch)."""
    if not SAMPLE_PROMPTS.is_file():
        return _err("The sample file is missing from this installation.", 404)
    return FileResponse(SAMPLE_PROMPTS, media_type="application/x-ndjson", filename="voice-lines-sample.jsonl",
                        headers={"Cache-Control": "no-store"})


class PromptsBody(BaseModel):
    jsonl: str = Field(default="", max_length=8 * 1024 * 1024)


@router.post("/control/speech/prompts")
async def speech_prompts(body: PromptsBody, request: Request, user: Principal = Depends(_manage)):
    try:
        res = corpus_prompts.import_jsonl(_admin_store(), body.jsonl)
    except corpus_settings.CorpusConfigError as e:
        return _err(str(e), 409)
    except CorpusError as e:
        return _err(str(e), e.status)
    audit_log(user, action="speech.prompts_import", ip=_ip(request),
              detail={"added": res["added"], "duplicates": res["duplicates"], "rejected": res["rejected_count"]})
    return res


class DeleteBody(BaseModel):
    employee_id: str = Field(default="", max_length=40)


@router.post("/control/speech/contributor/delete")
async def speech_delete_contributor(body: DeleteBody, request: Request,
                                    user: Principal = Depends(_manage_verified)):
    try:
        res = _admin_store().delete_contributor(body.employee_id)
    except corpus_settings.CorpusConfigError as e:
        return _err(str(e), 409)
    except CorpusError as e:
        return _err(str(e), e.status)
    # the speaker code only: the name / employee ID are personal data and are now gone
    audit_log(user, action="speech.contributor_delete", resource=res["speaker_code"],
              detail={"recordings": res["recordings_deleted"]}, ip=_ip(request))
    return res

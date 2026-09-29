import re
import time
from fastapi import Depends
from core import cloud, lanes
from core.auth import Principal, user_has_permission
from core.deps import get_current_user

from .helpers import _err, router


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

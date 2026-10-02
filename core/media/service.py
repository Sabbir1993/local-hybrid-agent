import asyncio
import re
import sys
import time
from typing import Optional

from .. import lanes
from .cloud_backend import _cloud_generate, _cloud_transcribe
from .constants import (
    KINDS,
    MediaError,
    NotLoadedError,
    ProgressFn,
    _cloud_text,
    _is_sdcpp,
    media_cfg,
    plain_error,
)
from .sdcpp_backend import clean_opts, edit_blocked, edit_targets, sdcpp_generate
from .storage import _clean_transcript, check_wav, markdown_for, save_file


async def _policy_check(user, texts: list, any_cloud: bool) -> None:
    from .. import input_guard
    hit = await input_guard.check_async([t for t in texts if t], user, any_cloud)
    if hit:
        raise MediaError(hit.get("message") or "Blocked by your organization's policy.")


def status(user_id: Optional[int]) -> dict:
    """Which media jobs are set up for this user (the UI hides what isn't)."""
    out = {}
    for short, job in (("image", "image_gen"), ("video", "video_gen"), ("transcribe", "transcribe")):
        ts = [t for t in lanes.targets(job, user_id) if t.available()]
        reg = lanes.registry(user_id)
        first = ts[0] if ts else None
        inst = first.inst if first else None
        sd = _is_sdcpp(inst)
        out[short] = {
            "ready": bool(ts),
            "model": reg[first.lane]["label"] if first else None,
            "where": first.source if first else None,
            "cloud": bool(first and first.is_cloud),
            "provider": first.cm.provider_name if first and first.is_cloud else None,
            "lane": first.lane if first else None,
            "needs_load": bool(sd and not inst.is_up()),
            "state": inst.state() if sd else None,
            "default_size": (inst.cfg.get("default_size") or "large") if sd and short == "image" else None,
        }
    caps = {"img2img": bool(edit_targets(user_id, "img2img")), "refs": False, "max_refs": 0}
    refs = edit_targets(user_id, "edit")
    if refs:
        caps.update(refs=True, max_refs=refs[0].inst.edit_caps()["max_refs"])
    out["image"]["edit"] = caps
    out["max_audio_mb"] = int(media_cfg().get("max_audio_mb") or 25)
    return out


async def generate(kind: str, user, prompt: str, opts: Optional[dict] = None,
                   progress: ProgressFn = None, lane: Optional[str] = None,
                   enforce_quota: bool = True) -> dict:
    """Make an image or a video. -> {files, markdown, source, model, ms}."""
    from .jobs import check_cloud_quota
    if kind not in KINDS:
        raise MediaError("kind must be image or video")
    job = KINDS[kind]
    prompt = str(prompt or "").strip()
    if not prompt:
        raise MediaError("Describe what you'd like to see first.")
    if len(prompt) > 4000:
        raise MediaError("That description is too long (4000 characters max).")
    opts = clean_opts(kind, opts)
    uid = getattr(user, "id", None)
    if opts.get("inputs"):
        route = edit_targets(uid, opts["mode"], lane)
        if not route:
            raise MediaError(edit_blocked(uid, opts["mode"]))
        caps = route[0].inst.edit_caps()
        if opts["mode"] == "edit" and len(opts["inputs"]) > caps["max_refs"]:
            raise MediaError(f"This model takes at most {caps['max_refs']} pictures at a time.")
    else:
        route = [t for t in lanes.targets(job, uid) if t.available()]
        if lane:
            route.sort(key=lambda t: t.lane != lane)
    if not route:
        raise MediaError(f"{lanes.JOBS[job]['label']} isn't set up yet - add a model in "
                         "Settings -> Models & Jobs.")
    await _policy_check(user, [prompt, opts.get("negative")], any(t.is_cloud for t in route))

    errors = []
    not_loaded = None
    n_not_loaded = 0
    reg = lanes.registry(uid)
    for i, t in enumerate(route):
        label = reg.get(t.lane, {}).get("label") or t.lane
        if progress and i:
            await progress(f"Trying the backup: {label}", None)
        t0 = time.time()
        try:
            if t.is_cloud:
                if enforce_quota:
                    q_fn = getattr(sys.modules.get("core.media"), "check_cloud_quota", check_cloud_quota)
                    q_fn(uid, kind)
                blobs = await _cloud_generate(kind, t.cm, _cloud_text(prompt),
                                              {**opts, "negative": _cloud_text(opts.get("negative") or "")},
                                              progress)
            elif _is_sdcpp(t.inst):
                if not t.inst.is_up():
                    raise NotLoadedError(t.lane, label, kind)
                blobs = await sdcpp_generate(t.inst, kind, prompt, opts, progress)
            else:
                raise MediaError(f"'{label}' isn't an image/video model")
            sf_fn = getattr(sys.modules.get("core.media"), "save_file", save_file)
            files = [sf_fn(uid, b, prompt, kind) for b in blobs[:4]]
            where = f"{t.cm.provider_name} (cloud)" if t.is_cloud else "this PC"
            return {"files": files, "markdown": markdown_for(files, prompt), "model": label,
                    "source": t.source, "where": where, "ms": int((time.time() - t0) * 1000),
                    "lane": t.lane, "cloud": t.is_cloud}
        except asyncio.CancelledError:
            raise
        except Exception as e:
            if isinstance(e, NotLoadedError):
                not_loaded = not_loaded or e
                n_not_loaded += 1
            msg = plain_error(e)
            errors.append(f"{label}: {msg}")
            print(f"[media] {job} via {t.describe()} failed: {type(e).__name__}", file=sys.stderr)
    if not_loaded is not None and n_not_loaded == len(errors):
        raise not_loaded
    raise MediaError(" / ".join(errors) if len(errors) > 1 else errors[0])


async def transcribe(user, wav: bytes, language: Optional[str] = None) -> dict:
    """-> {text, model, source, ms, audio_s, rtf}.

    audio_s = spoken seconds (from the WAV header), rtf = wall_time /
    audio_s (< 1 means faster than realtime - the number Phase 1 chunk
    sizing is tuned against).
    """
    check_wav(wav)
    uid = getattr(user, "id", None)
    lang = (language or "").strip().lower() or None
    if lang and lang != "auto" and not re.fullmatch(r"[a-z]{2,3}", lang):
        lang = None
    route = [t for t in lanes.targets("transcribe", uid) if t.available()]
    if not route:
        raise MediaError("Speech to text isn't set up yet - add a model in Settings -> Models & Jobs.")
    errors = []
    reg = lanes.registry(uid)
    for t in route:
        label = reg.get(t.lane, {}).get("label") or t.lane
        t0 = time.time()
        try:
            if t.is_cloud:
                text = await _cloud_transcribe(t.cm, wav, lang)
            else:
                inst = t.inst
                if inst is None or not hasattr(inst, "transcribe"):
                    raise MediaError(f"'{label}' isn't a speech-to-text model")
                text = await inst.transcribe(wav, lang)
            wall = time.time() - t0
            out = {"text": _clean_transcript(text), "model": label, "source": t.source,
                   "ms": int(wall * 1000)}
            secs = wav_seconds(wav)
            if secs:
                out["audio_s"] = round(secs, 1)
                out["rtf"] = round(wall / secs, 2)
            return out
        except asyncio.CancelledError:
            raise
        except Exception as e:
            errors.append(f"{label}: {plain_error(e)}")
            print(f"[media] transcribe via {t.describe()} failed: {type(e).__name__}", file=sys.stderr)
    raise MediaError(" / ".join(errors))


def wav_seconds(wav: bytes) -> Optional[float]:
    """Spoken seconds from a WAV header (None when unreadable)."""
    try:
        import struct
        if len(wav) < 44 or wav[:4] != b"RIFF" or wav[8:12] != b"WAVE":
            return None
        channels = struct.unpack("<H", wav[22:24])[0]
        rate = struct.unpack("<I", wav[24:28])[0]
        bits = struct.unpack("<H", wav[34:36])[0]
        pos = wav.find(b"data", 12)   # extra chunks (LIST, bext, …) may precede data
        if pos < 0 or not rate or not channels or not bits:
            return None
        size = struct.unpack("<I", wav[pos + 4:pos + 8])[0]
        secs = size / (rate * channels * (bits / 8))
        return secs if secs > 0 else None
    except Exception:
        return None


async def warmup(user) -> dict:
    """Pre-load the local speech-to-text server so the first real recording
    skips the model-load wait. -> {model, source, already_loaded}."""
    uid = getattr(user, "id", None)
    route = [t for t in lanes.targets("transcribe", uid)
             if not t.is_cloud and t.available()]
    if not route:
        raise MediaError("Speech to text isn't set up yet - add a model in Settings -> Models & Jobs.")
    t = route[0]
    inst = t.inst
    if inst is None or not hasattr(inst, "ensure_loaded"):
        raise MediaError("Speech to text isn't set up yet - add a model in Settings -> Models & Jobs.")
    already = inst.is_up()
    await inst.ensure_loaded()
    reg = lanes.registry(uid)
    return {"model": reg.get(t.lane, {}).get("label") or t.lane, "source": t.source,
            "already_loaded": bool(already), "warmed": True}

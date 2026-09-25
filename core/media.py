"""
core/media.py - images, videos and speech to text through the lane registry.

Jobs (core/lanes.py): image_gen, video_gen, transcribe. Each one is off until a
model is added in Settings -> Models & Jobs; call sites never name a lane:

  generate("image" | "video", user, prompt, opts, progress)  -> result dict
  transcribe(user, wav_bytes, language)                      -> result dict

Engines behind a lane:
  sdcpp     stable-diffusion.cpp's sd-server on this PC (Vulkan). Loaded by hand
            (Settings -> Models & Jobs / the chat card), never by a request
  whisper   whisper.cpp server on this PC (core/small_model.WhisperInstance)
  cloud     OpenAI-style APIs (images/generations, videos, audio/transcriptions)
            or Google's native Imagen / Veo API

Before anything goes out the prompt passes the policy check (input_guard, which
also blocks card numbers). Results are saved to the user's private folder
(common/user_<id>/generated) and served by /agent/raw.
"""

import asyncio
import base64
import re
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Awaitable, Callable, Optional

import httpx

from . import lanes

KINDS = {"image": "image_gen", "video": "video_gen"}
_MAX_BYTES = {"image": 40 * 1024 * 1024, "video": 400 * 1024 * 1024}
_POLL_S = 3.0

ProgressFn = Optional[Callable[[str, Optional[int]], Awaitable[None]]]


class MediaError(RuntimeError):
    """A plain-language failure shown to the user as-is."""


class NotLoadedError(MediaError):
    """The image/video model runs on this PC but nobody has loaded it yet."""

    def __init__(self, lane: str, label: str, kind: str = "image"):
        what = "image" if kind == "image" else "video"
        super().__init__(f"The {what} model ({label}) isn't loaded - load it in Settings -> Models & Jobs, "
                         f"or with the Load button in the {what} card.")
        self.lane = lane
        self.label = label


def _is_sdcpp(inst) -> bool:
    return inst is not None and hasattr(inst, "est_bytes") and hasattr(inst, "load")


def media_cfg() -> dict:
    from .small_model import APP_CONFIG
    m = APP_CONFIG.get("media")
    return m if isinstance(m, dict) else {}


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
            # a local sd.cpp model must be loaded by hand before it can be used
            "needs_load": bool(sd and not inst.is_up()),
            "state": inst.state() if sd else None,
            # the model's size when the chat leaves it on "Model default"
            "default_size": (inst.cfg.get("default_size") or "large") if sd and short == "image" else None,
        }
    # what the image model on this PC can do with pictures (the UI offers only that)
    caps = {"img2img": bool(edit_targets(user_id, "img2img")), "refs": False, "max_refs": 0}
    refs = edit_targets(user_id, "edit")
    if refs:
        caps.update(refs=True, max_refs=refs[0].inst.edit_caps()["max_refs"])
    out["image"]["edit"] = caps
    out["max_audio_mb"] = int(media_cfg().get("max_audio_mb") or 25)
    return out


# ---------------- file handling ----------------

def sniff(data: bytes) -> Optional[tuple]:
    """(mime, ext) from magic bytes, never from what the service claims."""
    from .small_model import image_mime
    m = image_mime(data)
    if m:
        return m, {"image/png": ".png", "image/jpeg": ".jpg", "image/webp": ".webp", "image/gif": ".gif"}[m]
    if len(data) > 12 and data[4:8] == b"ftyp":
        return "video/mp4", ".mp4"
    if data[:4] == b"\x1a\x45\xdf\xa3":
        return "video/webm", ".webm"
    return None


def _slug(text: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", str(text or "").lower()).strip("-")
    return (s[:40].rstrip("-") or "media")


def save_file(user_id: int, data: bytes, prompt: str, want: str) -> dict:
    from .agent_tools import user_common_root
    kind = sniff(data)
    if kind is None:
        raise MediaError(f"The reply wasn't an {'image' if want == 'image' else 'video'} file.")
    mime, ext = kind
    if want == "image" and not mime.startswith("image/"):
        raise MediaError("Asked for an image but got a video back.")
    if want == "video" and not (mime.startswith("video/") or mime == "image/gif"):
        raise MediaError("Asked for a video but got an image back - check which service this model uses.")
    d = user_common_root(user_id) / "generated"
    d.mkdir(parents=True, exist_ok=True)
    # "_" isn't a PAN separator (core/pan.py): "20260926-010055" reads as a 14-digit
    # card number and gets masked on its way to a cloud model, breaking the link
    stem = f"{datetime.now():%Y%m%d_%H%M%S}_{_slug(prompt)}"
    p = d / (stem + ext)
    n = 1
    while p.exists():
        n += 1
        p = d / f"{stem}-{n}{ext}"
    p.write_bytes(data)
    return {"path": f"generated/{p.name}", "name": p.name, "mime": mime, "size": len(data)}


def markdown_for(files: list, prompt: str) -> str:
    """Chat/agent markdown for saved results: inline image, or a video player marker."""
    from urllib.parse import quote
    alt = re.sub(r"[\[\]\r\n]+", " ", str(prompt or ""))[:80].strip() or "generated image"
    out = []
    for f in files:
        if f["mime"].startswith("image/"):
            # one preview; clicking it opens the viewer, which has the download button
            out.append(f"![{alt}](/agent/raw?path={quote(f['path'])})")
        else:
            out.append(f"[VIDEO: {f['path']}]")
            out.append(f"[DOWNLOAD: {f['path']}]")
    return "\n\n".join(out)


# ---------------- policy ----------------

async def _policy_check(user, texts: list, any_cloud: bool) -> None:
    from . import input_guard
    hit = await input_guard.check_async([t for t in texts if t], user, any_cloud)
    if hit:
        raise MediaError(hit.get("message") or "Blocked by your organization's policy.")


def _cloud_text(text: str) -> str:
    """Belt and braces: card numbers never reach a cloud media API."""
    from . import pan
    if not text or not pan.enabled("pan_cloud_egress"):
        return text
    return pan.mask_pans(text)[0]


# ---------------- generation ----------------

def _aspect(opts: dict) -> str:
    w, h = int(opts.get("width") or 0), int(opts.get("height") or 0)
    if w and h:
        return "wide" if w > h * 1.15 else "tall" if h > w * 1.15 else "square"
    return str(opts.get("aspect") or "square")


_SIZES = {"square": (1024, 1024), "wide": (1536, 1024), "tall": (1024, 1536)}
_VIDEO_SIZES = {"square": (720, 720), "wide": (1280, 720), "tall": (720, 1280)}
# image size choice (local models): side of the square; other shapes keep the same pixel count
SIZE_SIDES = {"small": 512, "medium": 768, "large": 1024, "xlarge": 1280}
_SHAPE = {"square": 1.0, "wide": 1.5, "tall": 2 / 3}


def size_for(size: str, aspect: str) -> tuple:
    """(width, height) for a size choice + shape, multiples of 32 (static/js/media.js mirrors this)."""
    side, r = SIZE_SIDES[size], _SHAPE.get(aspect, 1.0)
    return _mult32(side * r ** 0.5), _mult32(side / r ** 0.5)


def clean_opts(kind: str, opts: Optional[dict]) -> dict:
    """Only known option fields, clamped - they go to external services."""
    o = dict(opts or {})
    out = {}
    aspect = str(o.get("aspect") or "").lower()
    if aspect in _SIZES:
        out["aspect"] = aspect
        w, h = (_SIZES if kind == "image" else _VIDEO_SIZES)[aspect]
        size = str(o.get("size") or "").lower()
        if kind == "image" and size in SIZE_SIDES:
            out["size"] = size
            w, h = size_for(size, aspect)
        out.setdefault("width", w)
        out.setdefault("height", h)
    for k, lo, hi in (("width", 64, 4096), ("height", 64, 4096), ("seed", 0, 2 ** 31 - 1), ("seconds", 1, 60)):
        if o.get(k) not in (None, ""):
            try:
                out[k] = max(lo, min(hi, int(o[k])))
            except (TypeError, ValueError):
                pass
    if kind == "video":
        out.setdefault("seconds", 4)
    neg = str(o.get("negative") or "").strip()
    if neg:
        out["negative"] = neg[:2000]
    # pictures to change / combine: already checked + re-encoded by media_images.prepare_inputs
    pics = [p for p in (o.get("inputs") or []) if isinstance(p, dict) and isinstance(p.get("png"), bytes)]
    mode = str(o.get("mode") or "")
    if kind == "image" and pics and mode in EDIT_MODES:
        out["inputs"] = pics[:10]
        out["mode"] = mode
        if mode == "img2img":
            out["inputs"] = pics[:1]
            try:
                out["strength"] = max(0.05, min(1.0, float(o.get("strength") or 0.6)))
            except (TypeError, ValueError):
                out["strength"] = 0.6
        size = str(o.get("size") or "").lower()
        if size in SIZE_SIDES:
            out["size"] = size          # the result keeps the first picture's shape at this size
    return out


EDIT_MODES = ("img2img", "edit")
_EDIT_WORD = {"img2img": "change a picture", "edit": "combine pictures"}


def edit_targets(uid, mode: str, lane: Optional[str] = None) -> list:
    """Models that can take pictures for `mode`, best first. Only sd.cpp on this PC:
    pictures never go to a cloud provider (they can't be checked for card numbers)."""
    out = []
    for t in lanes.targets("image_gen", uid):
        if t.is_cloud or not t.available() or not _is_sdcpp(t.inst):
            continue
        caps = t.inst.edit_caps()
        if caps["img2img" if mode == "img2img" else "refs"]:
            out.append(t)
    if lane:
        out.sort(key=lambda t: t.lane != lane)
    return out


def edit_blocked(uid, mode: str) -> str:
    """Why nothing can `mode` for this user (plain words for the card)."""
    local = [t for t in lanes.targets("image_gen", uid)
             if not t.is_cloud and t.available() and _is_sdcpp(t.inst)]
    if not local:
        return ("Working from pictures only works with the image model on this PC - "
                "pictures are never sent to a cloud model.")
    label = lanes.registry(uid).get(local[0].lane, {}).get("label") or local[0].lane
    return (f"'{label}' can't {_EDIT_WORD.get(mode, mode)} yet - add its vision weights "
            "(for Qwen Image 2.1: mmproj-Qwen3VL-8B-Instruct-F16.gguf) in Settings -> Models & Jobs.")


async def generate(kind: str, user, prompt: str, opts: Optional[dict] = None,
                   progress: ProgressFn = None, lane: Optional[str] = None,
                   enforce_quota: bool = True) -> dict:
    """Make an image or a video. -> {files, markdown, source, model, ms}.
    Walks the job's route (mapped model, then its backups). Raises MediaError."""
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
        # only models on this PC that can do it: the pictures never leave this PC
        route = edit_targets(uid, opts["mode"], lane)
        if not route:
            raise MediaError(edit_blocked(uid, opts["mode"]))
        caps = route[0].inst.edit_caps()
        if opts["mode"] == "edit" and len(opts["inputs"]) > caps["max_refs"]:
            raise MediaError(f"This model takes at most {caps['max_refs']} pictures at a time.")
    else:
        route = [t for t in lanes.targets(job, uid) if t.available()]
        if lane:
            # the user picked a model in the options row: it goes first, backups stay
            route.sort(key=lambda t: t.lane != lane)
    if not route:
        raise MediaError(f"{lanes.JOBS[job]['label']} isn't set up yet - add a model in "
                         "Settings -> Models & Jobs.")
    await _policy_check(user, [prompt, opts.get("negative")], any(t.is_cloud for t in route))

    errors = []
    not_loaded = None
    reg = lanes.registry(uid)
    for i, t in enumerate(route):
        label = reg.get(t.lane, {}).get("label") or t.lane
        if progress and i:
            await progress(f"Trying the backup: {label}", None)
        t0 = time.time()
        try:
            if t.is_cloud:
                if enforce_quota:
                    check_cloud_quota(uid, kind)
                blobs = await _cloud_generate(kind, t.cm, _cloud_text(prompt),
                                              {**opts, "negative": _cloud_text(opts.get("negative") or "")},
                                              progress)
            elif _is_sdcpp(t.inst):
                if not t.inst.is_up():
                    raise NotLoadedError(t.lane, label, kind)
                blobs = await sdcpp_generate(t.inst, kind, prompt, opts, progress)
            else:
                raise MediaError(f"'{label}' isn't an image/video model")
            files = [save_file(uid, b, prompt, kind) for b in blobs[:4]]
            where = f"{t.cm.provider_name} (cloud)" if t.is_cloud else "this PC"
            return {"files": files, "markdown": markdown_for(files, prompt), "model": label,
                    "source": t.source, "where": where, "ms": int((time.time() - t0) * 1000),
                    "lane": t.lane, "cloud": t.is_cloud}
        except asyncio.CancelledError:
            raise
        except Exception as e:
            if isinstance(e, NotLoadedError):
                not_loaded = not_loaded or e
            msg = plain_error(e)
            errors.append(f"{label}: {msg}")
            print(f"[media] {job} via {t.describe()} failed: {type(e).__name__}", file=sys.stderr)
    if not_loaded is not None and len(errors) == 1:
        raise not_loaded
    raise MediaError(" / ".join(errors) if len(errors) > 1 else errors[0])


def plain_error(e: Exception) -> str:
    if isinstance(e, MediaError):
        return str(e)
    if isinstance(e, httpx.TimeoutException):
        return "It took too long - the service may still be working, or it's stuck."
    if isinstance(e, httpx.ConnectError):
        return "Couldn't reach the service - is it running?"
    if isinstance(e, httpx.HTTPStatusError):
        code = e.response.status_code
        if code in (401, 403):
            return "The key or auth header was rejected."
        if code == 404:
            return "The service doesn't know that address or model (404)."
        if code == 429:
            return "The provider says too many requests or out of credit (429)."
        if code == 400:
            return f"The service didn't accept the request (400): {_snippet(e.response.text)}"
        return f"The service answered with an error ({code})."
    from .net_guard import BlockedURLError
    if isinstance(e, BlockedURLError):
        return f"Address not allowed: {e}"
    return f"{type(e).__name__}: {str(e)[:200]}"


def _snippet(text: str, n: int = 200) -> str:
    from . import pan
    return pan.mask_pans(re.sub(r"\s+", " ", str(text or "")).strip()[:n])[0]


# ---------------- engine: stable-diffusion.cpp on this PC ----------------

def _mult32(v: int) -> int:
    # Qwen Image 2.1 needs multiples of 32; every other sd.cpp model accepts them too
    return max(64, int(v) // 32 * 32)


def _fit_1mp(w: int, h: int, side: int = 1024) -> tuple:
    """A size with the picture's shape and about side x side pixels (1 megapixel by default)."""
    scale = (side * side / max(1, w * h)) ** 0.5
    return _mult32(w * scale), _mult32(h * scale)


def sdcpp_body(inst, kind: str, prompt: str, opts: dict) -> dict:
    """Native sd-server request (/sdcpp/v1/img_gen | vid_gen). Only documented fields."""
    cfg = inst.cfg
    video = kind == "video"
    pics = opts.get("inputs") or []
    # the size picked in the chat, else the model's default size (Settings)
    size = opts.get("size") or (None if video else cfg.get("default_size"))
    if pics and not (opts.get("width") and opts.get("height")):
        w, h = _fit_1mp(pics[0]["w"], pics[0]["h"],         # keep the picture's shape
                        SIZE_SIDES.get(size or "", 1024))
    elif size in SIZE_SIDES and not video:
        w, h = size_for(size, str(opts.get("aspect") or "square"))
    else:
        w = _mult32(opts.get("width") or (832 if video else 1024))
        h = _mult32(opts.get("height") or (480 if video else 1024))
    # opts["steps"] is internal only (the Settings test uses fewer); clean_opts drops it from users
    sp = {"sample_steps": int(opts.get("steps") or cfg.get("steps") or (8 if video else 20))}
    if cfg.get("sampler"):
        sp["sample_method"] = str(cfg["sampler"])
    if cfg.get("flow_shift") is not None:
        sp["flow_shift"] = float(cfg["flow_shift"])
    if cfg.get("cfg_scale") is not None:
        sp["guidance"] = {"txt_cfg": float(cfg["cfg_scale"])}
    body = {"prompt": prompt, "negative_prompt": opts.get("negative") or "",
            "width": w, "height": h,
            "seed": int(opts["seed"]) if opts.get("seed") is not None else -1,
            "sample_params": sp}
    if video:
        fps = int(cfg.get("fps") or 16)
        # Wan wants 4n+1 frames
        frames = max(5, int(opts.get("seconds") or 4) * fps // 4 * 4 + 1)
        body.update({"video_frames": frames, "fps": fps, "output_format": "webm"})
    else:
        body.update({"batch_count": 1, "output_format": "png"})
    if pics:
        from .media_images import data_url
        if opts.get("mode") == "img2img":
            body["init_image"] = data_url(pics[0])
            body["strength"] = float(opts.get("strength") or 0.6)
        else:
            body["ref_images"] = [data_url(p) for p in pics]      # <image1>, <image2>... in order
    return body


def _sampling_steps(body: dict) -> set:
    """Step counts the sampling bar can show (a changed picture runs only strength x steps)."""
    n = int((body.get("sample_params") or {}).get("sample_steps") or 0)
    out = {n}
    if body.get("init_image") is not None:
        k = n * float(body.get("strength") or 1)
        out |= {int(k), int(k) + 1}
    return {x for x in out if x > 0}


def _clock(s: float) -> str:
    s = max(0, int(round(s)))
    return f"{s // 60} min {s % 60:02d} s" if s >= 60 else f"{s} s"


def _step_progress(step, t_sent: float, steps: set, kind: str) -> tuple:
    """(text, pct) from sd.cpp's step bar. Bars of other sizes (tiled VAE decode...) = finishing."""
    if not step or step[3] < t_sent:
        return "Preparing (reading the description)", 3
    i, n, spi, _ = step
    if n not in steps:
        return f"Finishing the {kind}", 95
    left = (n - i) * spi
    text = f"Step {i} of {n}" + (f" · {spi:.1f} s/step · about {_clock(left)} left" if spi and i < n else "")
    if i >= n:
        text = f"Finishing the {kind}"
    return text, min(95, 5 + int(88 * i / max(1, n)))


async def sdcpp_generate(inst, kind: str, prompt: str, opts: dict, progress: ProgressFn = None,
                         poll_s: float = 1.0) -> list:
    """Submit to the local sd-server, poll the job, return the file bytes.
    Cancelling (asyncio.CancelledError) cancels the job there too."""
    if not inst.is_up():
        raise NotLoadedError(inst.role, inst.cfg.get("label") or inst.role, kind)
    path = "/sdcpp/v1/vid_gen" if kind == "video" else "/sdcpp/v1/img_gen"
    inst.busy += 1
    inst.last_used = time.time()
    job_id = None
    try:
        body = sdcpp_body(inst, kind, prompt, opts)
        steps = _sampling_steps(body)
        t_sent = time.time()
        r = await inst.client.post(path, json=body, timeout=60)
        if r.status_code == 429:
            raise MediaError(f"The {kind} model is busy with other requests - try again in a moment.")
        r.raise_for_status()
        j = r.json()
        job_id = str(j.get("id") or "")
        if not job_id:
            raise MediaError(f"sd-server didn't return a job id. It said: {_snippet(r.text)}")
        poll = str(j.get("poll_url") or f"/sdcpp/v1/jobs/{job_id}")
        if not poll.startswith("/sdcpp/v1/jobs/"):
            poll = f"/sdcpp/v1/jobs/{job_id}"         # stay on the local server
        deadline = time.time() + inst.timeout_s
        last = None
        while True:
            if time.time() > deadline:
                raise MediaError(f"The {kind} took longer than {inst.timeout_s // 60} minutes - "
                                 "try fewer steps or a smaller size.")
            g = await inst.client.get(poll, timeout=30)
            g.raise_for_status()
            d = g.json()
            st = str(d.get("status") or "").lower()
            if st == "completed":
                break
            if st in ("failed", "cancelled"):
                err = d.get("error")
                if isinstance(err, dict):
                    err = err.get("message")
                raise MediaError(f"The {kind} model failed: {_snippet(err or st, 200)}")
            if progress:
                qp = d.get("queue_position")
                pct = None
                if st == "queued" and qp:
                    text = f"Waiting in line ({qp} ahead)"
                elif st == "generating":
                    text, pct = _step_progress(getattr(inst, "step", None), t_sent, steps, kind)
                else:
                    text = "Starting"
                if text != last:
                    await progress(text, pct)
                    last = text
            await asyncio.sleep(poll_s)
        res = d.get("result") or {}
        found = [x.get("b64_json") for x in (res.get("images") or []) if isinstance(x, dict)]
        if res.get("b64_json"):
            found.append(res["b64_json"])
        blobs = []
        for v in found[:4]:
            if isinstance(v, str) and v:
                try:
                    blobs.append(base64.b64decode(re.sub(r"\s+", "", v), validate=False))
                except Exception:
                    continue
        blobs = [b for b in blobs if sniff(b) and len(b) <= _MAX_BYTES[kind]]
        if not blobs:
            raise MediaError(f"sd-server finished but sent no {kind} back.")
        job_id = None                                  # finished: nothing to cancel
        return blobs
    except asyncio.CancelledError:
        if job_id:
            try:
                await asyncio.shield(inst.client.post(f"/sdcpp/v1/jobs/{job_id}/cancel", timeout=10))
            except Exception:
                pass
        raise
    finally:
        inst.busy = max(0, inst.busy - 1)
        inst.last_used = time.time()


# ---------------- engine: cloud ----------------

def _cloud_headers(cm, json_body: bool = True) -> dict:
    from .cloud import CloudClient
    h = CloudClient(cm).headers()
    if not json_body:
        h.pop("Content-Type", None)
    if cm.is_google:
        h.pop("Authorization", None)
        h["x-goog-api-key"] = cm.api_key
    return h


def _google_base() -> str:
    return "https://generativelanguage.googleapis.com/v1beta"


async def _cloud_generate(kind: str, cm, prompt: str, opts: dict, progress: ProgressFn) -> list:
    from .cloud import _client_for
    c = _client_for(cm)
    aspect = _aspect(opts)
    if progress:
        await progress(f"Sent to {cm.provider_name}", None)
    if cm.is_google:
        ratio = {"square": "1:1", "wide": "16:9", "tall": "9:16"}[aspect]
        if kind == "image":
            r = await c.post(f"{_google_base()}/models/{cm.model_id}:predict", headers=_cloud_headers(cm),
                             json={"instances": [{"prompt": prompt}],
                                   "parameters": {"sampleCount": 1, "aspectRatio": ratio}}, timeout=300)
            r.raise_for_status()
            preds = r.json().get("predictions") or []
            out = [base64.b64decode(p["bytesBase64Encoded"]) for p in preds if p.get("bytesBase64Encoded")]
            if not out:
                raise MediaError("Google returned no image (the prompt may have been filtered).")
            return out
        params = {"aspectRatio": "9:16" if aspect == "tall" else "16:9"}
        if opts.get("seconds"):
            params["durationSeconds"] = int(opts["seconds"])
        if opts.get("negative"):
            params["negativePrompt"] = opts["negative"]
        r = await c.post(f"{_google_base()}/models/{cm.model_id}:predictLongRunning",
                         headers=_cloud_headers(cm), json={"instances": [{"prompt": prompt}], "parameters": params},
                         timeout=120)
        r.raise_for_status()
        op = r.json().get("name")
        if not op:
            raise MediaError("Google didn't start the video job.")
        t0 = time.time()
        while True:
            await asyncio.sleep(_POLL_S * 2)
            s = await c.get(f"{_google_base()}/{op}", headers=_cloud_headers(cm), timeout=60)
            s.raise_for_status()
            d = s.json()
            if d.get("error"):
                raise MediaError(f"Google: {_snippet((d['error'] or {}).get('message'))}")
            if d.get("done"):
                break
            if progress:
                await progress(f"Google is making the video · {int(time.time() - t0)}s", None)
        samples = (((d.get("response") or {}).get("generateVideoResponse") or {}).get("generatedSamples") or [])
        uris = [((x.get("video") or {}).get("uri")) for x in samples]
        uris = [u for u in uris if u]
        if not uris:
            raise MediaError("Google finished but returned no video (the prompt may have been filtered).")
        g = await c.get(uris[0], headers=_cloud_headers(cm), timeout=300, follow_redirects=True)
        g.raise_for_status()
        return [g.content]

    # OpenAI-style
    if kind == "image":
        w, h = _SIZES[aspect]
        body = {"model": cm.model_id, "prompt": prompt, "n": 1, "size": f"{w}x{h}"}
        r = await c.post(cm.url("/images/generations"), headers=_cloud_headers(cm), json=body, timeout=300)
        r.raise_for_status()
        out = []
        for item in (r.json().get("data") or [])[:4]:
            if item.get("b64_json"):
                out.append(base64.b64decode(item["b64_json"]))
            elif item.get("url"):
                out.append(await _fetch_public(item["url"]))
        if not out:
            raise MediaError("The provider returned no image.")
        return out
    w, h = _VIDEO_SIZES["tall" if aspect == "tall" else "wide"]
    form = {"model": cm.model_id, "prompt": prompt, "seconds": str(int(opts.get("seconds") or 4)),
            "size": f"{w}x{h}"}
    r = await c.post(cm.url("/videos"), headers=_cloud_headers(cm, json_body=False), data=form, timeout=120)
    r.raise_for_status()
    vid = r.json().get("id")
    if not vid:
        raise MediaError("The provider didn't start the video job.")
    while True:
        await asyncio.sleep(_POLL_S * 2)
        s = await c.get(cm.url(f"/videos/{vid}"), headers=_cloud_headers(cm), timeout=60)
        s.raise_for_status()
        d = s.json()
        st = str(d.get("status") or "")
        if st == "failed":
            err = (d.get("error") or {}).get("message") if isinstance(d.get("error"), dict) else d.get("error")
            raise MediaError(f"The provider couldn't make it: {_snippet(err or 'failed')}")
        if st == "completed":
            break
        if progress:
            pct = d.get("progress")
            await progress(f"{cm.provider_name}: {st.replace('_', ' ') or 'working'}",
                           int(pct) if isinstance(pct, (int, float)) else None)
    g = await c.get(cm.url(f"/videos/{vid}/content"), headers=_cloud_headers(cm), timeout=300,
                    follow_redirects=True)
    g.raise_for_status()
    return [g.content]


async def _fetch_public(url: str) -> bytes:
    from .net_guard import guarded_get
    r = await asyncio.get_running_loop().run_in_executor(
        None, lambda: guarded_get(url, timeout=120, max_bytes=_MAX_BYTES["image"]))
    if r.status_code != 200:
        raise MediaError(f"Couldn't download the result ({r.status_code}).")
    return r.content


# ---------------- speech to text ----------------

def check_wav(data: bytes) -> None:
    if len(data) < 44 or data[:4] != b"RIFF" or data[8:12] != b"WAVE":
        raise MediaError("Send the audio as WAV (the app converts recordings and files for you).")
    cap = int(media_cfg().get("max_audio_mb") or 25) * 1024 * 1024
    if len(data) > cap:
        raise MediaError(f"That recording is too long ({cap // (1024 * 1024)} MB max - about 13 minutes).")


async def transcribe(user, wav: bytes, language: Optional[str] = None) -> dict:
    """-> {text, model, source, ms}. Local whisper first unless the user mapped a
    cloud model (only possible when an admin allowed cloud speech)."""
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
            return {"text": _clean_transcript(text), "model": label, "source": t.source,
                    "ms": int((time.time() - t0) * 1000)}
        except asyncio.CancelledError:
            raise
        except Exception as e:
            errors.append(f"{label}: {plain_error(e)}")
            print(f"[media] transcribe via {t.describe()} failed: {type(e).__name__}", file=sys.stderr)
    raise MediaError(" / ".join(errors))


def _clean_transcript(text: str) -> str:
    # whisper marks silence/noise like "[BLANK_AUDIO]" or "(music)"
    t = re.sub(r"\[(BLANK_AUDIO|MUSIC|NOISE|SILENCE)\]", "", str(text or ""), flags=re.I)
    return re.sub(r"[ \t]+", " ", t).strip()


async def _cloud_transcribe(cm, wav: bytes, lang: Optional[str]) -> str:
    from .cloud import _client_for
    if cm.is_google:
        raise MediaError("Google speech isn't supported yet - use an OpenAI-style provider.")
    data = {"model": cm.model_id, "response_format": "json"}
    if lang and lang != "auto":
        data["language"] = lang
    r = await _client_for(cm).post(cm.url("/audio/transcriptions"), headers=_cloud_headers(cm, json_body=False),
                                   files={"file": ("audio.wav", wav, "audio/wav")}, data=data, timeout=600)
    r.raise_for_status()
    try:
        return str(r.json().get("text") or "")
    except ValueError:
        return r.text


# ---------------- jobs (routes/media.py) ----------------

class Job:
    def __init__(self, user, kind: str, prompt: str, opts: dict, lane: Optional[str]):
        import uuid
        self.id = uuid.uuid4().hex[:16]
        self.user = user
        self.user_id = user.id
        self.kind = kind
        self.prompt = prompt
        self.opts = opts
        self.lane = lane
        self.created = time.time()
        self.events: list = []
        self.done = False
        self.changed = asyncio.Event()
        self.task: Optional[asyncio.Task] = None

    def push(self, ev: str, data: dict) -> None:
        self.events.append((ev, data))
        self.changed.set()


_JOBS: dict = {}
_KEEP_S = 30 * 60
_MAX_RUNNING = {"image": 2, "video": 1}


def _gc() -> None:
    now = time.time()
    for jid, j in list(_JOBS.items()):
        if j.done and now - j.created > _KEEP_S:
            _JOBS.pop(jid, None)


def running(user_id: int, kind: str) -> int:
    return sum(1 for j in _JOBS.values() if j.user_id == user_id and j.kind == kind and not j.done)


def used_today(user_id: int, kind: str) -> int:
    """Cloud generations this user made today (from the audit log)."""
    try:
        from .auth_db import db
        start = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0).timestamp()
        row = db().execute(
            "SELECT COUNT(*) FROM audit_log WHERE user_id=? AND action=? AND resource='cloud' "
            "AND result='allow' AND ts>=?", (user_id, f"media.{kind}", start)).fetchone()
        return int(row[0] or 0)
    except Exception:
        return 0


def daily_limit(kind: str) -> int:
    lim = media_cfg().get("limits") or {}
    try:
        return int(lim.get(f"{kind}_per_day", 50 if kind == "image" else 5))
    except (TypeError, ValueError):
        return 0


def start_job(user, kind: str, prompt: str, opts: dict, lane: Optional[str] = None) -> Job:
    _gc()
    if kind not in KINDS:
        raise MediaError("kind must be image or video")
    if running(user.id, kind) >= _MAX_RUNNING[kind]:
        raise MediaError(f"You already have {'a video' if kind == 'video' else 'two images'} in progress - "
                         "wait for it to finish or cancel it.")
    job = Job(user, kind, prompt, opts, lane)
    _JOBS[job.id] = job
    job.push("queued", {"kind": kind})
    job.task = asyncio.create_task(_run(job))
    return job


def check_cloud_quota(user_id: int, kind: str) -> None:
    lim = daily_limit(kind)
    if lim > 0 and used_today(user_id, kind) >= lim:
        raise MediaError(f"Daily limit reached for cloud {kind}s ({lim}). It resets at midnight.")


async def _run(job: Job) -> None:
    from .audit import audit_log
    t0 = time.time()

    async def _progress(text: str, pct: Optional[int]) -> None:
        job.push("progress", {"text": text, "pct": pct, "elapsed": int(time.time() - t0)})

    pics = job.opts.get("inputs") or []
    # metadata only: never the pictures themselves
    pic_meta = ({"mode": job.opts.get("mode"), "inputs": len(pics),
                 "input_bytes": sum(len(p.get("png") or b"") for p in pics)} if pics else {})
    try:
        res = await generate(job.kind, job.user, job.prompt, job.opts, _progress, job.lane)
        audit_log(job.user, action=f"media.{job.kind}", resource=res["source"],
                  detail={"prompt_chars": len(job.prompt), "lane": res["lane"], "ms": res["ms"], **pic_meta},
                  result="allow")
        job.push("done", res)
    except asyncio.CancelledError:
        job.push("error", {"message": "Cancelled.", "cancelled": True})
    except NotLoadedError as e:
        job.push("error", {"message": str(e), "not_loaded": {"lane": e.lane, "label": e.label}})
    except Exception as e:
        audit_log(job.user, action=f"media.{job.kind}", resource="failed",
                  detail={"prompt_chars": len(job.prompt)}, result="error")
        job.push("error", {"message": plain_error(e)})
    finally:
        job.opts.pop("inputs", None)        # don't keep the pictures in memory for 30 minutes
        job.done = True
        job.changed.set()


def get_job(job_id: str, user_id: int) -> Optional[Job]:
    j = _JOBS.get(job_id)
    return j if j and j.user_id == user_id else None


def cancel_job(job_id: str, user_id: int) -> bool:
    j = get_job(job_id, user_id)
    if not j or j.done or not j.task:
        return False
    j.task.cancel()
    return True


async def job_events(job: Job, heartbeat_s: float = 15.0):
    """Yields (event, data) from the start, then live until the job ends."""
    i = 0
    while True:
        while i < len(job.events):
            yield job.events[i]
            i += 1
        if job.done:
            return
        job.changed.clear()
        try:
            await asyncio.wait_for(job.changed.wait(), timeout=heartbeat_s)
        except asyncio.TimeoutError:
            yield ("ping", {"elapsed": int(time.time() - job.created)})


async def run_to_end(job: Job) -> dict:
    """Wait for a job (agent tools). -> its done payload, or raises MediaError.
    If the caller is cancelled (the chat was stopped), the job is cancelled too."""
    try:
        async for ev, data in job_events(job):
            if ev == "done":
                return data
            if ev == "error":
                raise MediaError(data.get("message") or "failed")
    except asyncio.CancelledError:
        if job.task and not job.done:
            job.task.cancel()          # -> sd-server's own cancel (sdcpp_generate)
        raise
    raise MediaError("the job ended without a result")

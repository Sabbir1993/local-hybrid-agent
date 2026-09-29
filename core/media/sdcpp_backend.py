import asyncio
import base64
import re
import time
from typing import Optional

from .constants import (
    _EDIT_WORD,
    _MAX_BYTES,
    _SHAPE,
    _SIZES,
    _VIDEO_SIZES,
    EDIT_MODES,
    MediaError,
    NotLoadedError,
    ProgressFn,
    SIZE_SIDES,
    _aspect,
    _fit_1mp,
    _is_sdcpp,
    _mult32,
    _snippet,
    size_for,
)
from .storage import sniff


def clean_opts(kind: str, opts: Optional[dict]) -> dict:
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
            out["size"] = size
    return out


def edit_targets(uid, mode: str, lane: Optional[str] = None) -> list:
    from .. import lanes
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
    from .. import lanes
    local = [t for t in lanes.targets("image_gen", uid)
             if not t.is_cloud and t.available() and _is_sdcpp(t.inst)]
    if not local:
        return ("Working from pictures only works with the image model on this PC - "
                "pictures are never sent to a cloud model.")
    label = lanes.registry(uid).get(local[0].lane, {}).get("label") or local[0].lane
    return (f"'{label}' can't {_EDIT_WORD.get(mode, mode)} yet - add its vision weights "
            "(for Qwen Image 2.1: mmproj-Qwen3VL-8B-Instruct-F16.gguf) in Settings -> Models & Jobs.")


def sdcpp_body(inst, kind: str, prompt: str, opts: dict) -> dict:
    cfg = inst.cfg
    video = kind == "video"
    pics = opts.get("inputs") or []
    size = opts.get("size") or (None if video else cfg.get("default_size"))
    if pics and not (opts.get("width") and opts.get("height")):
        w, h = _fit_1mp(pics[0]["w"], pics[0]["h"], SIZE_SIDES.get(size or "", 1024))
    elif size in SIZE_SIDES and not video:
        w, h = size_for(size, str(opts.get("aspect") or "square"))
    else:
        w = _mult32(opts.get("width") or (832 if video else 1024))
        h = _mult32(opts.get("height") or (480 if video else 1024))
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
        frames = max(5, int(opts.get("seconds") or 4) * fps // 4 * 4 + 1)
        body.update({"video_frames": frames, "fps": fps, "output_format": "webm"})
    else:
        body.update({"batch_count": 1, "output_format": "png"})
    if pics:
        from ..media_images import data_url
        if opts.get("mode") == "img2img":
            body["init_image"] = data_url(pics[0])
            body["strength"] = float(opts.get("strength") or 0.6)
        else:
            body["ref_images"] = [data_url(p) for p in pics]
    return body


def _sampling_steps(body: dict) -> set:
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
            poll = f"/sdcpp/v1/jobs/{job_id}"
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
        job_id = None
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

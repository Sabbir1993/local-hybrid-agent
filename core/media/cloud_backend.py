import asyncio
import base64
import sys
import time
from typing import Optional

from .constants import _POLL_S, MediaError, ProgressFn, _snippet
from .sdcpp_backend import _SIZES, _VIDEO_SIZES, _aspect
from .storage import _fetch_public


def _get_poll_s() -> float:
    mod = sys.modules.get("core.media")
    return getattr(mod, "_POLL_S", _POLL_S) if mod else _POLL_S


def _cloud_headers(cm, json_body: bool = True) -> dict:
    from ..cloud import CloudClient
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
    from ..cloud import _client_for
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
            await asyncio.sleep(_get_poll_s() * 2)
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
        await asyncio.sleep(_get_poll_s() * 2)
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


async def _cloud_transcribe(cm, wav: bytes, lang: Optional[str]) -> str:
    from ..cloud import _client_for
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

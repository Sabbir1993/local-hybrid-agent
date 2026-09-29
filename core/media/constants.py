import re
from typing import Awaitable, Callable, Optional

KINDS = {"image": "image_gen", "video": "video_gen"}
_MAX_BYTES = {"image": 40 * 1024 * 1024, "video": 400 * 1024 * 1024}
_POLL_S = 3.0

ProgressFn = Optional[Callable[[str, Optional[int]], Awaitable[None]]]

_SIZES = {"square": (1024, 1024), "wide": (1536, 1024), "tall": (1024, 1536)}
_VIDEO_SIZES = {"square": (720, 720), "wide": (1280, 720), "tall": (720, 1280)}
SIZE_SIDES = {"small": 512, "medium": 768, "large": 1024, "xlarge": 1280}
_SHAPE = {"square": 1.0, "wide": 1.5, "tall": 2 / 3}

EDIT_MODES = ("img2img", "edit")
_EDIT_WORD = {"img2img": "change a picture", "edit": "combine pictures"}


def _mult32(v: int) -> int:
    return max(64, int(v) // 32 * 32)


def _fit_1mp(w: int, h: int, side: int = 1024) -> tuple:
    scale = (side * side / max(1, w * h)) ** 0.5
    return _mult32(w * scale), _mult32(h * scale)


def _aspect(opts: dict) -> str:
    w, h = int(opts.get("width") or 0), int(opts.get("height") or 0)
    if w and h:
        return "wide" if w > h * 1.15 else "tall" if h > w * 1.15 else "square"
    return str(opts.get("aspect") or "square")


def size_for(size: str, aspect: str) -> tuple:
    side, r = SIZE_SIDES[size], _SHAPE.get(aspect, 1.0)
    return _mult32(side * r ** 0.5), _mult32(side / r ** 0.5)


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
    from ..small_model import APP_CONFIG
    m = APP_CONFIG.get("media")
    return m if isinstance(m, dict) else {}


def _cloud_text(text: str) -> str:
    """Belt and braces: card numbers never reach a cloud media API."""
    from .. import pan
    if not text or not pan.enabled("pan_cloud_egress"):
        return text
    return pan.mask_pans(text)[0]


def _snippet(text: str, n: int = 200) -> str:
    from .. import pan
    return pan.mask_pans(re.sub(r"\s+", " ", str(text or "")).strip()[:n])[0]


def plain_error(e: Exception) -> str:
    import httpx
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
    from ..net_guard import BlockedURLError
    if isinstance(e, BlockedURLError):
        return f"Address not allowed: {e}"
    return f"{type(e).__name__}: {str(e)[:200]}"

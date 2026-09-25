"""
core/media_images.py - pictures given to the image model (change a picture /
combine pictures). Local only: they go to sd-server on this PC, never to a cloud
provider, because pictures can't be checked for card numbers like text can.

prepare_inputs(items) -> [{"png": bytes, "w": int, "h": int}]
  items: [{"b64": "<base64 or data URL>"} | {"path": "generated/x.png"}]

Every picture is decoded with Pillow (PNG / JPEG / WEBP only, magic bytes must
match), scaled down to MAX_SIDE, and re-encoded as a fresh PNG - which drops
EXIF (GPS location, camera serial) and anything hidden in the file. Nothing is
written to disk.
"""

import base64
import binascii
import io
import re
import warnings
from typing import Optional

MAX_INPUTS = 10
MAX_BYTES = 10 * 1024 * 1024          # one picture, decoded
MAX_TOTAL = 40 * 1024 * 1024
MAX_SIDE = 1536
MAX_PIXELS = 40_000_000               # decompression-bomb guard (≈ 6300 × 6300)
_FORMATS = {"image/png": "PNG", "image/jpeg": "JPEG", "image/webp": "WEBP"}


class InputError(ValueError):
    """A picture that can't be used - the message is safe to show."""


def _decode_b64(s: str, n: int) -> bytes:
    s = str(s or "").strip()
    if s.startswith("data:"):
        if ";base64," not in s[:100]:
            raise InputError(f"picture {n} isn't base64")
        s = s.split(";base64,", 1)[1]
    if len(s) > MAX_BYTES * 4 // 3 + 16:
        raise InputError(f"picture {n} is too large ({MAX_BYTES // (1024 * 1024)} MB max)")
    try:
        return base64.b64decode(re.sub(r"\s+", "", s), validate=True)
    except (binascii.Error, ValueError):
        raise InputError(f"picture {n} isn't valid base64") from None


def _read_path(path: str, n: int) -> bytes:
    from .agent_tools import _common_resolve
    try:
        p = _common_resolve(str(path or "").strip())     # the user's own folder only
    except Exception:
        raise InputError(f"picture {n}: that file isn't in your files") from None
    if not p.is_file():
        raise InputError(f"picture {n}: file not found")
    if p.stat().st_size > MAX_BYTES:
        raise InputError(f"picture {n} is too large ({MAX_BYTES // (1024 * 1024)} MB max)")
    return p.read_bytes()


def clean_picture(data: bytes, n: int = 1) -> dict:
    """Decode, check, shrink and re-encode one picture -> {png, w, h}."""
    from PIL import Image, ImageOps
    from .small_model import image_mime
    if len(data) > MAX_BYTES:
        raise InputError(f"picture {n} is too large ({MAX_BYTES // (1024 * 1024)} MB max)")
    mime = image_mime(data)
    if mime not in _FORMATS:
        raise InputError(f"picture {n} must be a PNG, JPEG or WEBP")
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            im = Image.open(io.BytesIO(data), formats=[_FORMATS[mime]])
            w, h = im.size
            if w < 16 or h < 16 or w * h > MAX_PIXELS:
                raise InputError(f"picture {n} has an unusable size ({w}×{h})")
            im.load()
            im = ImageOps.exif_transpose(im)
    except InputError:
        raise
    except (Image.DecompressionBombError, Image.DecompressionBombWarning):
        raise InputError(f"picture {n} is too big to open") from None
    except Exception:
        raise InputError(f"picture {n} couldn't be read - is it a real image?") from None
    has_alpha = im.mode in ("RGBA", "LA", "PA") or (im.mode == "P" and "transparency" in im.info)
    im = im.convert("RGBA" if has_alpha else "RGB")
    im.thumbnail((MAX_SIDE, MAX_SIDE), Image.LANCZOS)
    out = io.BytesIO()
    im.save(out, format="PNG", optimize=False)      # fresh file: no EXIF / text chunks carried over
    return {"png": out.getvalue(), "w": im.width, "h": im.height}


def prepare_inputs(items: Optional[list], limit: int = MAX_INPUTS) -> list:
    items = list(items or [])
    if not items:
        return []
    limit = max(1, min(MAX_INPUTS, int(limit or MAX_INPUTS)))
    if len(items) > limit:
        raise InputError(f"at most {limit} pictures at a time")
    out, total = [], 0
    for n, it in enumerate(items, 1):
        it = it if isinstance(it, dict) else {}
        if it.get("b64"):
            data = _decode_b64(it["b64"], n)
        elif it.get("path"):
            data = _read_path(it["path"], n)
        else:
            raise InputError(f"picture {n} is empty")
        total += len(data)
        if total > MAX_TOTAL:
            raise InputError(f"the pictures are too large together ({MAX_TOTAL // (1024 * 1024)} MB max)")
        out.append(clean_picture(data, n))
    return out


def data_url(pic: dict) -> str:
    return "data:image/png;base64," + base64.b64encode(pic["png"]).decode()

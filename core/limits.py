"""
core/limits.py - inbound request limits: body size and per-user request rate.

There was no body-size limit anywhere and no rate limit outside login, so two things were
possible for anyone holding `chat.use`:

  - one POST carrying an enormous `messages` array. The PAN regex (core/pan.py) then runs over
    every element of every message, so the cost is paid twice over;
  - an unbounded number of requests per second. Nothing capped cloud spend either, and only
    the local lane went through the admission gate -- a user whose main lane is cloud got no
    concurrency limit at all.

Body limits are per-path rather than one global number. The upload endpoints legitimately
carry tens of megabytes (knowledge-base files are capped at 50 MB, images arrive base64-encoded
at ~1.33x), so a flat low limit would have broken them, while a flat high limit would have left
the JSON endpoints just as exposed. 8 MB is far more than any chat/agent payload needs.

Note on /agent/upload: routes/agent/constants.py allows 20 files of 50 MB, so one request could
carry ~1 GB. Per-file checks cannot catch that, only a total-body limit can, so that path is
capped at 64 MB and a large multi-file upload is expected to be split.

Config lives under the "serving" block in config/app.json:

    "serving": {
      "max_body_bytes": 8388608,
      "body_limit_paths": {"/knowledge/upload": 54525952},
      "rate_limit_per_min": 60,
      "rate_limit_paths": ["/agent/run", "/chat/run"],
      "max_inflight_cloud": 4
    }
"""

import threading
import time
from typing import Optional

from starlette.responses import JSONResponse

from .small_model import APP_CONFIG

_SAFE_METHODS = {"GET", "HEAD", "OPTIONS"}

# Generative surfaces: a token bucket bounds how fast one user can spend money or GPU time.
# Read-only polling (/control/monitor, /control/gpu, status endpoints) is deliberately absent
# so a Settings tab left open cannot rate-limit the app out from under the user.
DEFAULT_RATE_PATHS = (
    "/agent/run",
    "/chat/run",
    "/chat/compact",
    "/chat/completions",
    "/completions",
    "/agent/vision",
)

# Upload endpoints keep their real headroom; everything else falls back to the base limit.
DEFAULT_BODY_PATH_OVERRIDES = {
    "/knowledge/upload": 52 * 1024 * 1024,
    "/knowledge/upload/chunk": 9 * 1024 * 1024,
    "/agent/upload": 64 * 1024 * 1024,      # see the module docstring on the 20x50 MB case
    "/agent/vision": 24 * 1024 * 1024,      # 15 MB base64 field, ~20 MB on the wire
    "/media/generate": 24 * 1024 * 1024,
    "/media/transcribe": 24 * 1024 * 1024,
    "/agent/preview-html": 4 * 1024 * 1024,
}


class _BodyTooLarge(Exception):
    pass


def serving_cfg() -> dict:
    return APP_CONFIG.get("serving") or {}


def body_limit_for(path: str) -> int:
    """The limit in bytes for one request path."""
    cfg = serving_cfg()
    try:
        base = int(cfg.get("max_body_bytes", 8 * 1024 * 1024))
    except (TypeError, ValueError):
        base = 8 * 1024 * 1024
    if base <= 0:                                  # 0 disables the limit
        return 0
    overrides = cfg.get("body_limit_paths")
    if not isinstance(overrides, dict):
        overrides = DEFAULT_BODY_PATH_OVERRIDES
    try:
        return int(overrides.get(path, base))
    except (TypeError, ValueError):
        return base


def _rate_paths() -> tuple:
    cfg = serving_cfg()
    paths = cfg.get("rate_limit_paths")
    if not isinstance(paths, (list, tuple)):
        paths = DEFAULT_RATE_PATHS
    return tuple(str(p) for p in paths)


def _rate_per_min() -> int:
    try:
        return int(serving_cfg().get("rate_limit_per_min", 60))
    except (TypeError, ValueError):
        return 60


def _credential_key(scope) -> str:
    """Rate-limit bucket identity.

    The session cookie or API token, so the bucket follows the signed-in user rather than
    everyone behind one NAT address. Falls back to the peer address for unauthenticated
    callers (login), which is the right granularity there."""
    cookies = {}
    for part in _header(scope, "cookie").split(";"):
        if "=" in part:
            k, v = part.split("=", 1)
            cookies[k.strip()] = v.strip()
    tok = cookies.get("a770_session") or ""
    if not tok:
        auth = _header(scope, "authorization")
        if auth.lower().startswith("bearer "):
            tok = auth[7:].strip()
    if tok:
        # hashed: the bucket map must never hold a live credential as a dict key
        import hashlib
        return "s:" + hashlib.sha256(tok.encode("utf-8", "replace")).hexdigest()[:24]
    peer = scope.get("client") or ("?", 0)
    return "ip:" + str(peer[0])


class _Buckets:
    """Token bucket per key: `rate` tokens refilled per minute, up to `rate` in hand.

    A bucket rather than a fixed window, so a burst up to the per-minute allowance is allowed
    and a sustained flood is not. Entries idle past the refill horizon are dropped, so the map
    does not grow with every credential that has ever appeared."""
    def __init__(self):
        self._m: dict = {}
        self._lock = threading.Lock()

    def allow(self, key: str, per_min: int) -> bool:
        if per_min <= 0:                            # 0 disables rate limiting
            return True
        now = time.monotonic()
        with self._lock:
            tokens, seen = self._m.get(key, (float(per_min), now))
            if now - seen > 0:
                tokens = min(float(per_min), tokens + (now - seen) * per_min / 60.0)
            if tokens >= 1.0:
                tokens -= 1.0
                self._m[key] = (tokens, now)
                self._prune(now, per_min)
                return True
            self._m[key] = (tokens, now)
            return False

    def _prune(self, now: float, per_min: int) -> None:
        horizon = 60.0          # a key untouched for a full minute starts from a full bucket
        for k in [k for k, (_t, seen) in self._m.items() if now - seen > horizon]:
            self._m.pop(k, None)

    def reset(self) -> None:
        with self._lock:
            self._m.clear()


buckets = _Buckets()


class RequestLimitsMiddleware:
    """ASGI middleware: caps the inbound body and the request rate.

    Deliberately plain ASGI rather than BaseHTTPMiddleware, so it can wrap the `receive`
    callable. Checking Content-Length alone would not do: a chunked request carries no such
    header and could still be unbounded."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope.get("type") != "http":
            return await self.app(scope, receive, send)
        path = scope.get("path") or "/"

        if scope.get("method") not in _SAFE_METHODS:
            per_min = _rate_per_min()
            if per_min > 0 and any(path == p or path.startswith(p) for p in _rate_paths()):
                if not buckets.allow(_credential_key(scope), per_min):
                    return await _json(send, 429, {
                        "error": f"rate limit: at most {per_min} requests per minute on this endpoint"})

        limit = body_limit_for(path)
        if limit <= 0:
            return await self.app(scope, receive, send)

        declared = _header(scope, "content-length")
        if declared:
            try:
                if int(declared) > limit:
                    return await _json(send, 413, {
                        "error": f"request body too large (max {limit // (1024 * 1024)} MB)"})
            except ValueError:
                pass

        seen = 0

        async def limited_receive():
            nonlocal seen
            message = await receive()
            if message.get("type") == "http.request":
                seen += len(message.get("body") or b"")
                if seen > limit:
                    raise _BodyTooLarge()
            return message

        try:
            return await self.app(scope, limited_receive, send)
        except _BodyTooLarge:
            # the response has not started: the app was still reading the body
            return await _json(send, 413, {
                "error": f"request body too large (max {limit // (1024 * 1024)} MB)"})


async def _json(send, status: int, payload: dict) -> None:
    import json as _json_mod
    body = _json_mod.dumps(payload).encode("utf-8")
    await send({"type": "http.response.start", "status": status,
                "headers": [(b"content-type", b"application/json"),
                            (b"content-length", str(len(body)).encode())]})
    await send({"type": "http.response.body", "body": body})


def _header(scope, name: str) -> str:
    for k, v in scope.get("headers") or []:
        if k.decode("latin-1").lower() == name:
            return v.decode("latin-1")
    return ""


# ---------------- cloud concurrency + daily request cap ----------------

def max_inflight_cloud() -> int:
    try:
        return int(serving_cfg().get("max_inflight_cloud", 4))
    except (TypeError, ValueError):
        return 4


class CloudGate:
    """Concurrency cap for cloud lanes.

    The admission gate in routes/common/llm_stream.py is sized from the local llama-server's
    slot count and deliberately skipped every non-local client, so a user whose main lane was
    cloud had no limit at all -- and cloud calls cost money. This is a plain global cap: slot
    fairness is meaningless off-box, but bounding concurrent in-flight calls is not."""
    def __init__(self):
        self._sem: Optional[object] = None
        self._n = 0
        self._lock = threading.Lock()

    def sem(self):
        n = max_inflight_cloud()
        with self._lock:
            if self._sem is None or n != self._n:
                import asyncio
                self._sem, self._n = asyncio.Semaphore(max(1, n)), n
            return self._sem

    def enabled(self) -> bool:
        return max_inflight_cloud() > 0


cloud_gate = CloudGate()


def daily_cloud_requests(user_id: int) -> int:
    """Cloud requests made by this user since midnight.

    Counted from the audit log, the same way core/media/jobs.py counts cloud media. A count of
    *tokens* or money is not possible today: core/db/usage.py's requests table has no user_id
    column, so per-user spend cannot be attributed even in principle. Adding one is the
    prerequisite for a real spend cap.
    """
    from datetime import datetime

    from . import auth_db
    try:
        start = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0).timestamp()
        return int(auth_db.count_audit(since=start, user_id=user_id,
                                       action="cloud.request", result="allow") or 0)
    except Exception:
        return 0


def daily_cloud_limit() -> int:
    try:
        return int(serving_cfg().get("cloud_requests_per_day", 500))
    except (TypeError, ValueError):
        return 500


def check_cloud_request_quota(user_id: int) -> Optional[str]:
    """None when the user may make a cloud request, else the reason they may not."""
    lim = daily_cloud_limit()
    if lim <= 0:
        return None
    used = daily_cloud_requests(user_id)
    if used >= lim:
        return f"daily cloud request limit reached ({lim}); it resets at midnight"
    return None
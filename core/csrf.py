"""
core/csrf.py - Double-submit-cookie CSRF protection for cookie-based sessions.

On login, a non-httponly a770_csrf cookie is set alongside the session
cookie. The frontend echoes its value back as an X-CSRF-Token header on
every mutating request; this middleware rejects mismatches. GET/HEAD/OPTIONS
are exempt (safe methods, no state change).
"""

import hmac

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse

from .auth import CSRF_COOKIE, SESSION_COOKIE

_SAFE_METHODS = {"GET", "HEAD", "OPTIONS"}
_EXEMPT_PATHS = {"/auth/login"}  # no session yet to CSRF-protect against on first login


class CSRFMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        if request.method not in _SAFE_METHODS and request.url.path not in _EXEMPT_PATHS:
            session_cookie = request.cookies.get(SESSION_COOKIE)
            if session_cookie:
                cookie_token = request.cookies.get(CSRF_COOKIE)
                header_token = request.headers.get("X-CSRF-Token")
                if not cookie_token or not header_token or not hmac.compare_digest(cookie_token, header_token):
                    return JSONResponse({"error": "csrf token missing or invalid"}, status_code=403)
        return await call_next(request)


# Baseline headers for every response. Handlers that set their own (e.g. the
# sandboxed CSP on /agent/raw) keep theirs. SAMEORIGIN, not DENY: the preview
# pane frames /agent/raw from this origin.
_SECURITY_HEADERS = (
    (b"x-frame-options", b"SAMEORIGIN"),
    (b"x-content-type-options", b"nosniff"),
    (b"referrer-policy", b"same-origin"),
    (b"permissions-policy", b"camera=(), geolocation=(), payment=()"),
)

# Strict script policy for the app's own pages: no inline script or handlers (all JS is
# in /static), so injected markup can't run. Styles stay 'unsafe-inline' (inline style
# attributes throughout the UI, and mermaid's generated <style>); they can't execute.
# img/media allow https: for click-to-load images and videos in chat.
CSP = "; ".join((
    "default-src 'self'",
    "script-src 'self' https://cdn.jsdelivr.net https://cdn.sheetjs.com",
    "style-src 'self' 'unsafe-inline'",
    "img-src 'self' data: blob: https:",
    "media-src 'self' blob: https:",
    "font-src 'self' data:",
    "connect-src 'self'",
    "frame-src 'self' blob: https://www.youtube-nocookie.com",
    "worker-src 'self' blob:",
    "object-src 'none'",
    "base-uri 'self'",
    "form-action 'self'",
    "frame-ancestors 'self'",
)).encode()
# only documents need a CSP; on e.g. a PDF, object-src 'none' breaks the browser's viewer
_CSP_TYPES = (b"text/html", b"image/svg+xml")


class SecurityHeadersMiddleware:
    """Pure ASGI (not BaseHTTPMiddleware) so streaming/SSE responses pass through untouched."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)

        async def _send(message):
            if message["type"] == "http.response.start":
                headers = list(message.get("headers") or [])
                present = {k.lower() for k, _ in headers}
                headers += [(k, v) for k, v in _SECURITY_HEADERS if k not in present]
                ctype = next((v for k, v in headers if k.lower() == b"content-type"), b"").lower()
                if b"content-security-policy" not in present and ctype.startswith(_CSP_TYPES):
                    headers.append((b"content-security-policy", CSP))
                if scope.get("scheme") == "https" and b"strict-transport-security" not in present:
                    headers.append((b"strict-transport-security", b"max-age=31536000"))
                message["headers"] = headers
            await send(message)

        await self.app(scope, receive, _send)

"""
core/csrf.py - Double-submit-cookie CSRF protection for cookie-based sessions.

On login, a non-httponly a770_csrf cookie is set alongside the session
cookie. The frontend echoes its value back as an X-CSRF-Token header on
every mutating request; this middleware rejects mismatches. GET/HEAD/OPTIONS
are exempt (safe methods, no state change).
"""

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
                if not cookie_token or not header_token or cookie_token != header_token:
                    return JSONResponse({"error": "csrf token missing or invalid"}, status_code=403)
        return await call_next(request)

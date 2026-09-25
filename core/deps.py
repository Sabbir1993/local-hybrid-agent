"""
core/deps.py - FastAPI dependencies for authentication and permission checks.

Applied per-route (not one blanket middleware) since different endpoints
need different permissions -- composes more legibly than a monolithic
path-to-permission map.
"""

from fastapi import Depends, HTTPException, Request

from .audit import audit_log
from .auth import (API_TOKEN_PREFIX, SESSION_COOKIE, Principal, user_has_permission,
                   verify_api_token, verify_session)
from .request_context import set_current_user, set_current_device


# Reachable while a password change is pending: the pages themselves (their UI opens
# the change form) and the auth endpoints needed to do it.
_PASSWORD_CHANGE_PATHS = {"/", "/settings", "/auth/me", "/auth/change_password", "/auth/logout"}


def _bearer(request: Request) -> str:
    h = request.headers.get("authorization") or ""
    return h[7:].strip() if h[:7].lower() == "bearer " else ""


async def get_current_user(request: Request) -> Principal:
    bearer = _bearer(request)
    if bearer.startswith(API_TOKEN_PREFIX):
        # API tokens can't manage sessions/passwords (or mint more tokens via /auth)
        if request.url.path.startswith("/auth/"):
            raise HTTPException(status_code=403, detail="API tokens are not accepted here")
        principal = verify_api_token(bearer, request.client.host if request.client else None)
    else:
        token = request.cookies.get(SESSION_COOKIE)
        # the UI marks its status/monitor polls so they don't count as user activity
        background = request.headers.get("x-a770-background") == "1"
        principal = verify_session(token, touch=not background)
    if not principal:
        raise HTTPException(status_code=401, detail="not authenticated")
    if principal.must_change_password and request.url.path not in _PASSWORD_CHANGE_PATHS:
        # PCI DSS 8.3.5: a first-use / reset password must be changed before anything else
        raise HTTPException(status_code=403, detail="password change required")
    request.state.principal = principal
    set_current_user(principal.id)
    dev_id = request.headers.get("x-device-id") or request.headers.get("X-Device-Id")
    dev_name = request.headers.get("x-device-name") or request.headers.get("X-Device-Name")
    if dev_id:
        set_current_device(dev_id, dev_name)
    return principal


def require_permission(key: str):
    async def _dep(request: Request, user: Principal = Depends(get_current_user)) -> Principal:
        ip = request.client.host if request.client else None
        if not user_has_permission(user, key):
            audit_log(user, action=key, permission_key=key, result="deny", ip=ip)
            raise HTTPException(status_code=403, detail=f"missing permission: {key}")
        return user
    return _dep

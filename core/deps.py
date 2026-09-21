"""
core/deps.py - FastAPI dependencies for authentication and permission checks.

Applied per-route (not one blanket middleware) since different endpoints
need different permissions -- composes more legibly than a monolithic
path-to-permission map.
"""

from fastapi import Depends, HTTPException, Request

from .audit import audit_log
from .auth import SESSION_COOKIE, Principal, user_has_permission, verify_session
from .request_context import set_current_user, set_current_device


async def get_current_user(request: Request) -> Principal:
    token = request.cookies.get(SESSION_COOKIE)
    principal = verify_session(token)
    if not principal:
        raise HTTPException(status_code=401, detail="not authenticated")
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

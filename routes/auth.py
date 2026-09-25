"""routes/auth.py - login/logout/me/change-password."""

import time
from types import SimpleNamespace

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel

from core import auth_db
from core.audit import audit_log
from core.auth import (
    CSRF_COOKIE,
    SESSION_ABSOLUTE_MAX_S,
    SESSION_COOKIE,
    Principal,
    create_session,
    new_csrf_token,
    password_policy_error,
    revoke_session,
    session_idle_s,
    verify_session,
)
from core.auth_provider import get_auth_provider, hash_password, verify_password
from core.deps import get_current_user

router = APIRouter(prefix="/auth", tags=["auth"])


class LoginBody(BaseModel):
    username: str
    password: str


class ChangePasswordBody(BaseModel):
    current_password: str
    new_password: str


def _set_auth_cookies(response: Response, raw_session: str, csrf: str, secure: bool) -> None:
    response.set_cookie(
        SESSION_COOKIE, raw_session, httponly=True, samesite="strict",
        # idle expiry is enforced server-side (verify_session); a cookie that expired after
        # the idle window would log out users who are actively working
        secure=secure, max_age=SESSION_ABSOLUTE_MAX_S, path="/",
    )
    response.set_cookie(
        CSRF_COOKIE, csrf, httponly=False, samesite="strict",
        secure=secure, max_age=SESSION_ABSOLUTE_MAX_S, path="/",
    )


# Per-IP throttle on top of per-account lockout, so one client can't spray a
# password across many usernames. In-memory: resets on restart, single process.
_IP_WINDOW_S = 15 * 60
_IP_MAX_FAILURES = 30
_ip_failures: dict = {}


_IP_TRACK_MAX = 10_000   # bound memory under a spray from many source addresses


def _ip_blocked(ip) -> bool:
    now = time.time()
    if len(_ip_failures) > _IP_TRACK_MAX:
        for k in [k for k, v in _ip_failures.items() if not v or now - v[-1] >= _IP_WINDOW_S]:
            _ip_failures.pop(k, None)
    hits = [t for t in _ip_failures.get(ip, ()) if now - t < _IP_WINDOW_S]
    if hits:
        _ip_failures[ip] = hits
    else:
        _ip_failures.pop(ip, None)
    return len(hits) >= _IP_MAX_FAILURES


@router.post("/login")
async def login(body: LoginBody, request: Request, response: Response):
    provider = get_auth_provider()
    ip = request.client.host if request.client else None
    if _ip_blocked(ip):
        audit_log(None, action="login.throttled", resource=body.username, result="deny", ip=ip)
        raise HTTPException(status_code=429, detail="too many failed logins - try again later")
    user = provider.verify_credentials(body.username, body.password)
    if not user:
        _ip_failures.setdefault(ip, []).append(time.time())
        audit_log(None, action="login.failed", resource=body.username, result="deny", ip=ip)
        raise HTTPException(status_code=401, detail="invalid username or password")

    raw_session = create_session(user, ip=ip, user_agent=request.headers.get("user-agent"))
    csrf = new_csrf_token()
    secure = request.url.scheme == "https"
    _set_auth_cookies(response, raw_session, csrf, secure)

    audit_log(SimpleNamespace(id=user.id, username=user.username), action="login.success",
              resource=user.username, result="allow", ip=ip)
    roles = auth_db.get_user_role_names(user.id)
    perms = sorted(auth_db.get_user_permission_keys(user.id))
    return {
        "user": {"id": user.id, "username": user.username, "display_name": user.display_name,
                  "is_super_admin": user.is_super_admin, "must_change_password": user.must_change_password},
        "roles": roles,
        "permissions": perms if not user.is_super_admin else sorted(auth_db.PERMISSIONS.keys()),
    }


@router.post("/logout")
async def logout(request: Request, response: Response):
    token = request.cookies.get(SESSION_COOKIE)
    principal = verify_session(token)
    revoke_session(token)
    if principal:
        audit_log(principal, action="logout", resource=principal.username, result="allow",
                  ip=request.client.host if request.client else None)
    response.delete_cookie(SESSION_COOKIE, path="/")
    response.delete_cookie(CSRF_COOKIE, path="/")
    return {"ok": True}


@router.get("/me")
async def me(user: Principal = Depends(get_current_user)):
    perms = sorted(user.permission_keys) if not user.is_super_admin else sorted(auth_db.PERMISSIONS.keys())
    return {
        "user": {"id": user.id, "username": user.username, "display_name": user.display_name,
                  "is_super_admin": user.is_super_admin, "must_change_password": user.must_change_password},
        "roles": user.role_names,
        "permissions": perms,
        "idle_seconds": session_idle_s(),
    }


@router.post("/change_password")
async def change_password(body: ChangePasswordBody, request: Request, response: Response,
                           user: Principal = Depends(get_current_user)):
    row = auth_db.get_user_by_id(user.id)
    if not row or row["auth_provider"] != "local" or not row["password_hash"]:
        raise HTTPException(status_code=400, detail="password login not available for this account")
    if not verify_password(body.current_password, row["password_hash"]):
        audit_log(user, action="password.change", resource=user.username, result="deny",
                  detail={"reason": "wrong current password"})
        raise HTTPException(status_code=401, detail="current password is incorrect")
    err = password_policy_error(body.new_password, user.username)
    if err:
        raise HTTPException(status_code=400, detail=err)
    if verify_password(body.new_password, row["password_hash"]):
        raise HTTPException(status_code=400, detail="new password must differ from the current one")
    new_hash = hash_password(body.new_password)
    auth_db.db().execute(
        "UPDATE users SET password_hash = ?, must_change_password = 0, updated_at = ? WHERE id = ?",
        (new_hash, time.time(), user.id),
    )
    auth_db.db().commit()
    auth_db.revoke_all_sessions_for_user(user.id)
    audit_log(user, action="password.change", resource=user.username, result="allow",
              ip=request.client.host if request.client else None)

    # Re-issue a fresh session for this request so the caller isn't logged out
    # by the mass-revoke that just happened above.
    from core.auth_provider import UserRecord
    fresh = UserRecord(id=user.id, username=user.username, display_name=user.display_name,
                        is_active=True, is_super_admin=user.is_super_admin, must_change_password=False)
    raw_session = create_session(fresh, ip=request.client.host if request.client else None,
                                  user_agent=request.headers.get("user-agent"))
    csrf = new_csrf_token()
    _set_auth_cookies(response, raw_session, csrf, secure=request.url.scheme == "https")
    return {"ok": True}

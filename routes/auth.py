"""routes/auth.py - login/logout/me/change-password."""

import hashlib
import secrets
import time
from types import SimpleNamespace

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel

from core import auth_db
from core import totp
from core.audit import audit_log
from core.auth import (
    CSRF_COOKIE,
    IDLE_MAX_MINUTES,
    IDLE_MIN_MINUTES,
    SESSION_ABSOLUTE_MAX_S,
    SESSION_COOKIE,
    Principal,
    _hash_token,
    create_session,
    mfa_required_policy,
    new_csrf_token,
    password_policy_error,
    revoke_session,
    session_policy,
    verify_session,
)
from core.auth_provider import get_auth_provider, hash_password, verify_account_password, verify_password
from core.config import update_app_config
from core.deps import get_current_user, require_verified
from core.small_model import APP_CONFIG

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

    who = SimpleNamespace(id=user.id, username=user.username)
    # Second factor before any session exists: password OK + TOTP on -> a
    # short-lived single-use ticket, never a session. Password OK + enforcement
    # on + no TOTP yet -> an enrollment ticket (bootstrap only).
    if auth_db.totp_enabled(user.id):
        ticket = _issue_mfa_ticket(user.id, "verify", ip)
        audit_log(who, action="login.mfa_required", resource=user.username, result="allow", ip=ip)
        return {"mfa_required": True, "ticket": ticket}
    row = auth_db.get_user_by_id(user.id)
    if mfa_required_policy() and row is not None and row["auth_provider"] == "local":
        ticket = _issue_mfa_ticket(user.id, "enroll", ip)
        audit_log(who, action="login.mfa_enrollment_required", resource=user.username, result="allow", ip=ip)
        return {"mfa_required": True, "enroll_required": True, "ticket": ticket}

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
        "idle_enabled": session_policy()["enabled"],
        "idle_seconds": session_policy()["minutes"] * 60,
    }


class SessionPolicyBody(BaseModel):
    enabled: bool
    minutes: int


@router.get("/session-policy")
async def get_session_policy(user: Principal = Depends(require_verified("users.manage"))):
    return {**session_policy(), "min": IDLE_MIN_MINUTES, "max": IDLE_MAX_MINUTES}


@router.put("/session-policy")
async def set_session_policy(body: SessionPolicyBody, request: Request,
                             user: Principal = Depends(require_verified("users.manage"))):
    """Admin: turn idle sign-out on/off and set the idle time. Takes effect for every user's next request
    (the page re-reads it from /auth/me on load); the 7-day absolute session cap always applies."""
    if not IDLE_MIN_MINUTES <= body.minutes <= IDLE_MAX_MINUTES:
        raise HTTPException(status_code=400,
                            detail=f"idle time must be {IDLE_MIN_MINUTES}-{IDLE_MAX_MINUTES} minutes")
    before = session_policy()

    def _apply(cfg):
        sec = cfg.setdefault("security", {})
        sec["session_idle_enabled"] = body.enabled
        sec["session_idle_minutes"] = body.minutes
    update_app_config(_apply)
    APP_CONFIG.setdefault("security", {}).update(session_idle_enabled=body.enabled,
                                                 session_idle_minutes=body.minutes)
    audit_log(user, action="security.session_policy", resource="session_idle",
              detail={"from": before, "to": session_policy()}, result="allow",
              ip=request.client.host if request.client else None)
    return {**session_policy(), "min": IDLE_MIN_MINUTES, "max": IDLE_MAX_MINUTES}


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
    # by the mass-revoke that just happened above. The verified-session flag
    # carries over: this request already proved both factors to get here.
    from core.auth_provider import UserRecord
    fresh = UserRecord(id=user.id, username=user.username, display_name=user.display_name,
                        is_active=True, is_super_admin=user.is_super_admin, must_change_password=False)
    raw_session = create_session(fresh, ip=request.client.host if request.client else None,
                                  user_agent=request.headers.get("user-agent"),
                                  mfa_verified=user.mfa_verified)
    csrf = new_csrf_token()
    _set_auth_cookies(response, raw_session, csrf, secure=request.url.scheme == "https")
    return {"ok": True}


# ---------------- TOTP second factor (MFA-2: enrollment lifecycle) ----------------
# Guessing a 6-digit code is far cheaper than guessing a password, so confirm
# attempts get their own throttle (per user, in-memory like the IP throttle):
# 10 bad codes in 5 minutes -> 429 until the window passes.
_MFA_WINDOW_S = 5 * 60
_MFA_MAX_FAILURES = 10
_mfa_failures: dict = {}


def _mfa_throttled(user_id) -> bool:
    now = time.time()
    hits = [t for t in _mfa_failures.get(user_id, ()) if now - t < _MFA_WINDOW_S]
    if hits:
        _mfa_failures[user_id] = hits
    else:
        _mfa_failures.pop(user_id, None)
    return len(hits) >= _MFA_MAX_FAILURES


def _local_password_row(user: Principal):
    """The users row when this account may manage MFA: local-password accounts,
    or directory accounts while the ldap provider is live (the password is
    re-checked against the directory, never stored). SSO-managed accounts
    enroll through their identity provider."""
    row = auth_db.get_user_by_id(user.id)
    if not row:
        return None
    if row["auth_provider"] == "local" and row["password_hash"]:
        return row
    if row["auth_provider"] == "ldap":
        return row
    return None


class MfaEnrollBody(BaseModel):
    password: str


class MfaCodeBody(BaseModel):
    code: str


@router.get("/mfa/status")
async def mfa_status(user: Principal = Depends(get_current_user)):
    return {"enabled": auth_db.totp_enabled(user.id),
            "backup_remaining": auth_db.remaining_backup_codes(user.id),
            "enforced": mfa_required_policy()}


@router.post("/mfa/enroll")
async def mfa_enroll(body: MfaEnrollBody, request: Request, user: Principal = Depends(get_current_user)):
    """Stage a fresh secret + backup codes (shown ONCE in the response).
    Nothing is enabled until /mfa/confirm proves the authenticator. Password
    re-entry stops a briefly-unattended logged-in browser from binding an
    attacker's authenticator."""
    row = _local_password_row(user)
    if row is None:
        raise HTTPException(status_code=400, detail="MFA enrollment is not available for this account")
    if auth_db.totp_enabled(user.id):
        raise HTTPException(status_code=400, detail="MFA is already enabled - disable it first to re-enroll")
    if not verify_account_password(user.id, body.password or ""):
        audit_log(user, action="mfa.enroll", resource=user.username, result="deny",
                   detail={"reason": "wrong password"}, ip=request.client.host if request.client else None)
        raise HTTPException(status_code=401, detail="password is incorrect")
    secret = totp.generate_secret()
    auth_db.set_totp_secret(user.id, secret)
    codes = totp.generate_backup_codes(8)
    auth_db.store_backup_codes(user.id, codes)
    audit_log(user, action="mfa.enroll", resource=user.username, result="allow",
               ip=request.client.host if request.client else None)
    return {"secret": secret, "otpauth_uri": totp.otpauth_uri(secret, user.username),
            "backup_codes": codes}


@router.post("/mfa/confirm")
async def mfa_confirm(body: MfaCodeBody, request: Request, user: Principal = Depends(get_current_user)):
    """Prove the authenticator with a TOTP code (backup codes don't count --
    they prove nothing about the app). Enables the staged secret."""
    if auth_db.totp_enabled(user.id):
        raise HTTPException(status_code=400, detail="MFA is already enabled")
    st = auth_db.get_totp(user.id)
    if not st["secret"]:
        raise HTTPException(status_code=400, detail="nothing to confirm - enroll first")
    ip = request.client.host if request.client else None
    if _mfa_throttled(user.id):
        audit_log(user, action="mfa.confirm", resource=user.username, result="deny",
                   detail={"reason": "throttled"}, ip=ip)
        raise HTTPException(status_code=429, detail="too many bad codes - try again later")
    counter = totp.verify(st["secret"], body.code or "")
    if counter is None:
        _mfa_failures.setdefault(user.id, []).append(time.time())
        audit_log(user, action="mfa.confirm", resource=user.username, result="deny", ip=ip)
        raise HTTPException(status_code=401, detail="invalid code")
    auth_db.enable_totp(user.id)
    auth_db.record_totp_counter(user.id, counter)
    _mfa_failures.pop(user.id, None)
    audit_log(user, action="mfa.confirm", resource=user.username, result="allow", ip=ip)
    return {"ok": True, "backup_remaining": auth_db.remaining_backup_codes(user.id)}


@router.post("/mfa/disable")
async def mfa_disable(body: MfaEnrollBody, request: Request, user: Principal = Depends(get_current_user)):
    """Wipe secret + codes. Password re-entry, same reason as enroll -- plus a
    verified session when MFA is on, so a stale pre-enrollment session plus a
    known password is not enough to strip the second factor."""
    row = _local_password_row(user)
    if row is None:
        raise HTTPException(status_code=400, detail="MFA management is not available for this account")
    if auth_db.totp_enabled(user.id) and not user.mfa_verified:
        raise HTTPException(status_code=403, detail="mfa_step_up_required")
    if not verify_account_password(user.id, body.password or ""):
        audit_log(user, action="mfa.disable", resource=user.username, result="deny",
                   detail={"reason": "wrong password"}, ip=request.client.host if request.client else None)
        raise HTTPException(status_code=401, detail="password is incorrect")
    was_on = auth_db.totp_enabled(user.id)
    auth_db.disable_totp(user.id)
    audit_log(user, action="mfa.disable", resource=user.username, result="allow",
               detail={"was_enabled": was_on}, ip=request.client.host if request.client else None)
    return {"ok": True}


# ---------------- TOTP second step (MFA-3: tickets, verify, step-up) ----------------
# A ticket is what a password buys when the account needs a second factor: a
# short-lived (5 min), single-use, IP-bound token good for exactly one purpose
# ("verify" or "enroll"). It is NOT a session: it authorizes no endpoint except
# the MFA ones below, and guessing codes against it burns it.
_MFA_TICKET_TTL_S = 5 * 60
_MFA_TICKET_MAX_FAILS = 5
_mfa_tickets: dict = {}


def _issue_mfa_ticket(user_id: int, purpose: str, ip) -> str:
    now = time.time()
    for digest, t in [x for x in _mfa_tickets.items() if now - x[1]["issued"] >= _MFA_TICKET_TTL_S]:
        _mfa_tickets.pop(digest, None)
    raw = "mfa_" + secrets.token_urlsafe(24)
    _mfa_tickets[hashlib.sha256(raw.encode()).hexdigest()] = {
        "user_id": user_id, "purpose": purpose, "issued": now, "ip": ip, "fails": 0,
    }
    return raw


def _take_mfa_ticket(raw: str, purpose: str, ip):
    """(user_id, record) for a live ticket of this purpose, else (None, None).
    A wrong code burns one of 5 guesses; the 5th burns the ticket, so a leaked
    ticket is worth at most 5 codes inside 5 minutes."""
    if not raw:
        return None, None
    digest = hashlib.sha256(raw.encode()).hexdigest()
    t = _mfa_tickets.get(digest)
    if t is None or t["purpose"] != purpose or t["ip"] != ip:
        return None, None
    if time.time() - t["issued"] >= _MFA_TICKET_TTL_S:
        _mfa_tickets.pop(digest, None)
        return None, None
    return t["user_id"], t


def _burn_ticket_fail(raw: str) -> None:
    digest = hashlib.sha256((raw or "").encode()).hexdigest()
    t = _mfa_tickets.get(digest)
    if t is None:
        return
    t["fails"] += 1
    if t["fails"] >= _MFA_TICKET_MAX_FAILS:
        _mfa_tickets.pop(digest, None)


def _consume_mfa_ticket(raw: str) -> None:
    _mfa_tickets.pop(hashlib.sha256((raw or "").encode()).hexdigest(), None)


def _login_success_response(user_id: int, username: str):
    roles = auth_db.get_user_role_names(user_id)
    perms = sorted(auth_db.get_user_permission_keys(user_id))
    row = auth_db.get_user_by_id(user_id)
    return {
        "user": {"id": user_id, "username": username,
                  "display_name": (row["display_name"] if row and row["display_name"] else username),
                  "is_super_admin": bool(row and row["is_super_admin"]),
                  "must_change_password": bool(row and row["must_change_password"])},
        "roles": roles,
        "permissions": perms if not (row and row["is_super_admin"]) else sorted(auth_db.PERMISSIONS.keys()),
    }


class MfaVerifyBody(BaseModel):
    ticket: str
    code: str


def _check_second_factor(user_id: int, code: str) -> bool:
    """TOTP (with replay recording) or a single-use backup code. True consumes."""
    st = auth_db.get_totp(user_id)
    if st["secret"]:
        counter = totp.verify(st["secret"], code or "", last_counter=st["last_counter"])
        if counter is not None:
            auth_db.record_totp_counter(user_id, counter)
            return True
    return auth_db.consume_backup_code(user_id, code or "")


@router.post("/mfa/verify")
async def mfa_verify(body: MfaVerifyBody, request: Request, response: Response):
    """Second step of login: ticket + TOTP/backup code -> verified session.
    Deliberately pre-session (PUBLIC_HTTP-listed like /auth/login): the ticket
    is the only credential it accepts."""
    ip = request.client.host if request.client else None
    user_id, _t = _take_mfa_ticket(body.ticket or "", "verify", ip)
    if user_id is None:
        raise HTTPException(status_code=401, detail="invalid or expired ticket - sign in again")
    row = auth_db.get_user_by_id(user_id)
    if not row or not row["is_active"]:
        _consume_mfa_ticket(body.ticket)
        raise HTTPException(status_code=401, detail="invalid or expired ticket - sign in again")
    if not _check_second_factor(user_id, body.code or ""):
        _burn_ticket_fail(body.ticket)
        audit_log(SimpleNamespace(id=user_id, username=row["username"]), action="login.mfa_failed",
                   resource=row["username"], result="deny", ip=ip)
        raise HTTPException(status_code=401, detail="invalid code")
    _consume_mfa_ticket(body.ticket)
    from core.auth_provider import UserRecord
    user = UserRecord(id=row["id"], username=row["username"],
                      display_name=row["display_name"] or row["username"],
                      is_active=True, is_super_admin=bool(row["is_super_admin"]),
                      must_change_password=bool(row["must_change_password"]))
    raw_session = create_session(user, ip=ip, user_agent=request.headers.get("user-agent"), mfa_verified=True)
    _set_auth_cookies(response, raw_session, new_csrf_token(), secure=request.url.scheme == "https")
    audit_log(SimpleNamespace(id=user.id, username=user.username), action="login.success",
               resource=user.username, result="allow", ip=ip)
    return _login_success_response(user.id, user.username)


class MfaTicketBody(BaseModel):
    ticket: str


@router.post("/mfa/ticket/enroll")
async def mfa_ticket_enroll(body: MfaTicketBody, request: Request):
    """Bootstrap for enforced accounts: the enrollment ticket (issued at login,
    which already verified the password minutes ago) stages a secret + backup
    codes, shown once. Nothing is enabled until ticket/confirm proves the app."""
    ip = request.client.host if request.client else None
    user_id, _t = _take_mfa_ticket(body.ticket or "", "enroll", ip)
    if user_id is None:
        raise HTTPException(status_code=401, detail="invalid or expired ticket - sign in again")
    row = auth_db.get_user_by_id(user_id)
    if not row or not row["is_active"]:
        raise HTTPException(status_code=401, detail="invalid or expired ticket - sign in again")
    if auth_db.totp_enabled(user_id):
        raise HTTPException(status_code=400, detail="MFA is already enabled")
    secret = totp.generate_secret()
    auth_db.set_totp_secret(user_id, secret)
    codes = totp.generate_backup_codes(8)
    auth_db.store_backup_codes(user_id, codes)
    audit_log(SimpleNamespace(id=user_id, username=row["username"]), action="mfa.enroll",
               resource=row["username"], result="allow", ip=ip)
    return {"secret": secret, "otpauth_uri": totp.otpauth_uri(secret, row["username"]),
            "backup_codes": codes}


@router.post("/mfa/ticket/confirm")
async def mfa_ticket_confirm(body: MfaVerifyBody, request: Request, response: Response):
    """Bootstrap completion: enrollment ticket + TOTP code (backup codes don't
    count) -> enabled + verified session in one step."""
    ip = request.client.host if request.client else None
    user_id, _t = _take_mfa_ticket(body.ticket or "", "enroll", ip)
    if user_id is None:
        raise HTTPException(status_code=401, detail="invalid or expired ticket - sign in again")
    row = auth_db.get_user_by_id(user_id)
    if not row or not row["is_active"]:
        _consume_mfa_ticket(body.ticket)
        raise HTTPException(status_code=401, detail="invalid or expired ticket - sign in again")
    st = auth_db.get_totp(user_id)
    if auth_db.totp_enabled(user_id) or not st["secret"]:
        raise HTTPException(status_code=400, detail="nothing to confirm - enroll first")
    counter = totp.verify(st["secret"], body.code or "")
    if counter is None:
        _burn_ticket_fail(body.ticket)
        audit_log(SimpleNamespace(id=user_id, username=row["username"]), action="mfa.confirm",
                   resource=row["username"], result="deny", ip=ip)
        raise HTTPException(status_code=401, detail="invalid code")
    auth_db.enable_totp(user_id)
    auth_db.record_totp_counter(user_id, counter)
    _consume_mfa_ticket(body.ticket)
    from core.auth_provider import UserRecord
    user = UserRecord(id=row["id"], username=row["username"],
                      display_name=row["display_name"] or row["username"],
                      is_active=True, is_super_admin=bool(row["is_super_admin"]),
                      must_change_password=bool(row["must_change_password"]))
    raw_session = create_session(user, ip=ip, user_agent=request.headers.get("user-agent"), mfa_verified=True)
    _set_auth_cookies(response, raw_session, new_csrf_token(), secure=request.url.scheme == "https")
    audit_log(SimpleNamespace(id=user.id, username=user.username), action="login.success",
               resource=user.username, result="allow", ip=ip)
    return _login_success_response(user.id, user.username)


@router.post("/mfa/step-up")
async def mfa_step_up(body: MfaCodeBody, request: Request, user: Principal = Depends(get_current_user)):
    """Elevate this session: TOTP or backup code -> the session row is marked
    verified (one-way). Lets an opt-in user with a stale session reach the
    fenced endpoints without a full re-login."""
    ip = request.client.host if request.client else None
    if user.mfa_verified:
        return {"ok": True, "already": True}
    if not auth_db.totp_enabled(user.id):
        raise HTTPException(status_code=400, detail="MFA is not enabled for this account")
    if _mfa_throttled(user.id):
        audit_log(user, action="mfa.step_up", resource=user.username, result="deny",
                   detail={"reason": "throttled"}, ip=ip)
        raise HTTPException(status_code=429, detail="too many bad codes - try again later")
    if not _check_second_factor(user.id, body.code or ""):
        _mfa_failures.setdefault(user.id, []).append(time.time())
        audit_log(user, action="mfa.step_up", resource=user.username, result="deny", ip=ip)
        raise HTTPException(status_code=401, detail="invalid code")
    token = request.cookies.get(SESSION_COOKIE)
    if token:
        auth_db.set_session_mfa_verified(_hash_token(token))
    _mfa_failures.pop(user.id, None)
    audit_log(user, action="mfa.step_up", resource=user.username, result="allow", ip=ip)
    return {"ok": True}


class MfaPolicyBody(BaseModel):
    required: bool


@router.get("/mfa/policy")
async def get_mfa_policy(user: Principal = Depends(require_verified("users.manage"))):
    return {"required": mfa_required_policy()}


@router.put("/mfa/policy")
async def set_mfa_policy(body: MfaPolicyBody, request: Request,
                         user: Principal = Depends(require_verified("users.manage"))):
    """Admin: require TOTP enrollment for every local-password account. No
    lockout paradox: an account with no MFA yet bootstraps via the login
    enrollment ticket, so enforcement bites at next login, never mid-session.
    Existing unverified sessions lose the fenced endpoints until step-up."""
    before = mfa_required_policy()

    def _apply(cfg):
        sec = cfg.setdefault("security", {})
        sec["mfa_required"] = bool(body.required)
    update_app_config(_apply)
    APP_CONFIG.setdefault("security", {}).update(mfa_required=bool(body.required))
    audit_log(user, action="security.mfa_policy", resource="mfa_required",
              detail={"from": before, "to": mfa_required_policy()}, result="allow",
              ip=request.client.host if request.client else None)
    return {"required": mfa_required_policy()}

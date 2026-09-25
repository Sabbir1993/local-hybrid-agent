"""
core/auth.py - Session lifecycle on top of auth_db + auth_provider.

Cookie holds a random raw token; only its sha256 is ever stored in
auth_sessions, mirroring the never-store-plaintext discipline used for
passwords -- a DB read alone can't mint a valid session.
"""

import hashlib
import secrets
import time
from dataclasses import dataclass
from typing import Optional

from . import auth_db
from .auth_provider import UserRecord

SESSION_COOKIE = "a770_session"
CSRF_COOKIE = "a770_csrf"
SESSION_TTL_S = 15 * 60          # sliding idle timeout: PCI DSS 8.2.8 caps it at 15 minutes
SESSION_ABSOLUTE_MAX_S = 7 * 24 * 3600  # hard cap regardless of activity


def session_idle_s() -> int:
    """Idle timeout, from app.json security.session_idle_minutes; never above 15 min."""
    try:
        from .small_model import APP_CONFIG
        mins = float((APP_CONFIG.get("security") or {}).get("session_idle_minutes") or 15)
    except Exception:
        mins = 15
    return int(max(1.0, min(mins, 15.0)) * 60)


@dataclass
class Principal:
    id: int
    username: str
    display_name: str
    is_super_admin: bool
    must_change_password: bool
    role_names: list
    permission_keys: set
    via_token: bool = False           # authenticated by an API token, not a browser session
    token_id: Optional[int] = None


def _hash_token(raw: str) -> str:
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def create_session(user: UserRecord, ip: Optional[str], user_agent: Optional[str]) -> str:
    raw = secrets.token_urlsafe(32)
    auth_db.create_session_row(_hash_token(raw), user.id, time.time() + session_idle_s(), ip, user_agent)
    return raw


def _to_principal(user_id: int) -> Optional[Principal]:
    row = auth_db.get_user_by_id(user_id)
    if not row or not row["is_active"]:
        return None
    roles = auth_db.get_user_role_names(user_id)
    perms = auth_db.get_user_permission_keys(user_id)
    return Principal(
        id=row["id"], username=row["username"], display_name=row["display_name"] or row["username"],
        is_super_admin=bool(row["is_super_admin"]), must_change_password=bool(row["must_change_password"]),
        role_names=roles, permission_keys=perms,
    )


def verify_session(raw_token: Optional[str], touch: bool = True) -> Optional[Principal]:
    """touch=False validates without sliding the idle expiry (background polls must not
    keep an unattended session alive - PCI DSS 8.2.8)."""
    if not raw_token:
        return None
    token_hash = _hash_token(raw_token)
    row = auth_db.get_session_row(token_hash)
    if not row or row["revoked_at"] is not None:
        return None
    now = time.time()
    if row["expires_at"] <= now:
        return None
    if now - row["created_at"] > SESSION_ABSOLUTE_MAX_S:
        auth_db.revoke_session_row(token_hash)
        return None
    if touch:
        auth_db.touch_session(token_hash, now + session_idle_s())
    return _to_principal(row["user_id"])


# ---------------- API tokens ----------------
API_TOKEN_PREFIX = "a770_pat_"
API_TOKEN_MAX_DAYS = 90
# Account/role/DB administration stays behind an interactive, MFA-able login:
# a leaked token must never be able to mint admins or read auth.db.
API_TOKEN_FORBIDDEN_PERMS = frozenset({"users.manage", "roles.manage", "database.manage"})


def create_api_token(user_id: int, name: str, permissions, days: int,
                     created_by: Optional[int]) -> tuple:
    """Returns (raw_token, token_id). The raw token is shown once and never stored."""
    raw = API_TOKEN_PREFIX + secrets.token_urlsafe(32)
    days = max(1, min(int(days), API_TOKEN_MAX_DAYS))
    tid = auth_db.create_api_token_row(
        _hash_token(raw), raw[:len(API_TOKEN_PREFIX) + 6], name, user_id, created_by,
        sorted(set(permissions) - API_TOKEN_FORBIDDEN_PERMS), time.time() + days * 86400)
    return raw, tid


def verify_api_token(raw_token: Optional[str], ip: Optional[str] = None) -> Optional[Principal]:
    if not raw_token or not raw_token.startswith(API_TOKEN_PREFIX):
        return None
    row = auth_db.get_api_token_by_hash(_hash_token(raw_token))
    if not row or row["revoked_at"] is not None or row["expires_at"] <= time.time():
        return None
    base = _to_principal(row["user_id"])
    if not base or base.must_change_password:
        return None
    import json
    scopes = set(json.loads(row["permissions"] or "[]")) - API_TOKEN_FORBIDDEN_PERMS
    held = set(auth_db.PERMISSIONS) if base.is_super_admin else base.permission_keys
    auth_db.touch_api_token(row["id"], ip)
    # effective rights = token scopes AND the user's current rights: removing a role
    # from the user narrows every token they own. Never a super-admin bypass.
    return Principal(id=base.id, username=base.username, display_name=base.display_name,
                     is_super_admin=False, must_change_password=False, role_names=base.role_names,
                     permission_keys=scopes & held, via_token=True, token_id=row["id"])


def revoke_session(raw_token: Optional[str]) -> None:
    if raw_token:
        auth_db.revoke_session_row(_hash_token(raw_token))


def user_has_permission(principal: Principal, key: str) -> bool:
    """Single choke point for the super-admin bypass -- never duplicate this check elsewhere."""
    if principal.is_super_admin:
        return True
    return key in principal.permission_keys


MIN_PASSWORD_LEN = 12


def password_policy_error(password: str, username: str = "") -> Optional[str]:
    """PCI DSS 8.3.6: at least 12 characters with both letters and digits."""
    pw = password or ""
    if len(pw) < MIN_PASSWORD_LEN:
        return f"password must be at least {MIN_PASSWORD_LEN} characters"
    if not any(c.isalpha() for c in pw) or not any(c.isdigit() for c in pw):
        return "password must contain both letters and digits"
    if username and pw.lower() == username.lower():
        return "password must not be the username"
    return None


def new_csrf_token() -> str:
    return secrets.token_urlsafe(24)

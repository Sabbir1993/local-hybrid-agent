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
SESSION_TTL_S = 12 * 3600        # sliding idle timeout
SESSION_ABSOLUTE_MAX_S = 7 * 24 * 3600  # hard cap regardless of activity


@dataclass
class Principal:
    id: int
    username: str
    display_name: str
    is_super_admin: bool
    must_change_password: bool
    role_names: list
    permission_keys: set


def _hash_token(raw: str) -> str:
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def create_session(user: UserRecord, ip: Optional[str], user_agent: Optional[str]) -> str:
    raw = secrets.token_urlsafe(32)
    auth_db.create_session_row(_hash_token(raw), user.id, time.time() + SESSION_TTL_S, ip, user_agent)
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


def verify_session(raw_token: Optional[str]) -> Optional[Principal]:
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
    auth_db.touch_session(token_hash, now + SESSION_TTL_S)
    return _to_principal(row["user_id"])


def revoke_session(raw_token: Optional[str]) -> None:
    if raw_token:
        auth_db.revoke_session_row(_hash_token(raw_token))


def user_has_permission(principal: Principal, key: str) -> bool:
    """Single choke point for the super-admin bypass -- never duplicate this check elsewhere."""
    if principal.is_super_admin:
        return True
    return key in principal.permission_keys


def new_csrf_token() -> str:
    return secrets.token_urlsafe(24)

"""
core/auth_provider.py - Credential verification, behind a swappable interface.

Everything downstream (sessions, RBAC, audit log) depends only on the
UserRecord this returns, never on *how* the user was authenticated. Swapping
to SSLWIRELESS corporate SSO later means writing a new AuthProvider and
flipping config/app.json -> {"auth": {"provider": "..."}}; no session/RBAC
code changes.
"""

from dataclasses import dataclass
from typing import Optional, Protocol

from argon2 import PasswordHasher
from argon2.exceptions import VerifyMismatchError, InvalidHash

from . import auth_db

_hasher = PasswordHasher()


@dataclass
class UserRecord:
    id: int
    username: str
    display_name: str
    is_active: bool
    is_super_admin: bool
    must_change_password: bool


def hash_password(plain: str) -> str:
    return _hasher.hash(plain)


def verify_password(plain: str, stored_hash: str) -> bool:
    try:
        _hasher.verify(stored_hash, plain)
        return True
    except (VerifyMismatchError, InvalidHash):
        return False


class AuthProvider(Protocol):
    def verify_credentials(self, username: str, password: str) -> Optional[UserRecord]: ...
    def supports_password_login(self) -> bool: ...


class LocalAuthProvider:
    """Verifies username/password against auth.db users.password_hash."""

    def supports_password_login(self) -> bool:
        return True

    def verify_credentials(self, username: str, password: str) -> Optional[UserRecord]:
        row = auth_db.get_user_by_username(username)
        if not row or not row["is_active"] or not row["password_hash"]:
            return None
        if row["auth_provider"] != "local":
            return None
        if auth_db.is_locked(row):
            return None             # same generic failure: don't reveal the lock
        if not verify_password(password, row["password_hash"]):
            if auth_db.record_failed_login(row["id"]):
                from .audit import audit_log
                audit_log(None, action="login.locked", resource=row["username"], result="deny",
                          detail={"lockout_s": auth_db.LOCKOUT_S})
            return None
        auth_db.touch_login(row["id"])
        return UserRecord(
            id=row["id"], username=row["username"], display_name=row["display_name"] or row["username"],
            is_active=bool(row["is_active"]), is_super_admin=bool(row["is_super_admin"]),
            must_change_password=bool(row["must_change_password"]),
        )


class CorporateSSOAuthProvider:
    """Placeholder for future SSLWIRELESS LDAP/AD/OIDC integration. Not implemented."""

    def supports_password_login(self) -> bool:
        return False

    def verify_credentials(self, username: str, password: str) -> Optional[UserRecord]:
        raise NotImplementedError("Corporate SSO is not configured yet")


def get_auth_provider() -> AuthProvider:
    # Single switch point for the future SSO cutover (read from config/app.json later).
    return LocalAuthProvider()

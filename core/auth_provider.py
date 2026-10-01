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
_dummy_hash: Optional[str] = None


def _burn_hash_time(password: str) -> None:
    """Spend one argon2 verify on a failed lookup so response time doesn't reveal
    whether the username exists / is active / is locked."""
    global _dummy_hash
    if _dummy_hash is None:
        _dummy_hash = _hasher.hash("timing-equalizer")
    verify_password(password or "", _dummy_hash)


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
        if (not row or not row["is_active"] or not row["password_hash"]
                or row["auth_provider"] != "local" or auth_db.is_locked(row)):
            _burn_hash_time(password)
            return None             # same generic failure: don't reveal which check failed
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
    """Placeholder for future OIDC-based corporate SSO. Not implemented.

    (Directory-password SSO lives in LdapAuthProvider below; this stub remains
    for a browser-redirect OIDC flow, which additionally needs public HTTPS.)"""

    def supports_password_login(self) -> bool:
        return False

    def verify_credentials(self, username: str, password: str) -> Optional[UserRecord]:
        raise NotImplementedError("Corporate SSO is not configured yet")


_LOOPBACK_HOSTS = ("localhost", "127.0.0.1", "::1")
_LDAP_CONNECT_TIMEOUT_S = 10


class LdapAuthProvider:
    """Corporate directory passwords (LDAP / Active Directory).

    Config (config/app.json -> auth.ldap): host (required), port (636 with
    use_ssl, else 389), use_ssl (default true), user_dn_template
    (e.g. "uid={username},ou=people,dc=example,dc=com") or service-account
    search (bind_dn + bind_password + search_base + search_filter with
    {username}). Plaintext binds are refused unless the host is loopback --
    credentials must never cross the LAN in the clear.

    A successful bind maps to a local users row (auth_provider='ldap',
    external_id=DN, password_hash NULL, default 'user' role): RBAC, sessions,
    audit, MFA and tokens all work unchanged downstream. Never grants admin;
    local break-glass accounts keep password login (see verify_credentials).
    """

    def __init__(self, cfg: Optional[dict] = None, _server=None):
        self.cfg = dict(cfg or {})
        self._server_override = _server  # ldap3 MOCK_SYNC server for tests
        self.host = str(self.cfg.get("host") or "").strip()
        if not self.host:
            raise ValueError("auth.ldap.host is required")
        self.use_ssl = bool(self.cfg.get("use_ssl", True))
        default_port = 636 if self.use_ssl else 389
        try:
            self.port = int(self.cfg.get("port") or default_port)
        except (TypeError, ValueError):
            self.port = default_port
        if not self.use_ssl and self.host.lower() not in _LOOPBACK_HOSTS:
            raise ValueError("auth.ldap refuses plaintext binds past loopback "
                             "(set use_ssl: true or use LDAPS)")
        self.user_dn_template = str(self.cfg.get("user_dn_template") or "").strip()
        self.bind_dn = str(self.cfg.get("bind_dn") or "").strip()
        self.search_base = str(self.cfg.get("search_base") or "").strip()
        self.search_filter = str(self.cfg.get("search_filter") or "(uid={username})")
        if not self.user_dn_template and not (self.bind_dn and self.search_base):
            raise ValueError("auth.ldap needs user_dn_template or bind_dn + search_base")

    def supports_password_login(self) -> bool:
        return True

    def _server(self):
        if self._server_override is not None:
            return self._server_override
        import ldap3
        return ldap3.Server(self.host, port=self.port, use_ssl=self.use_ssl,
                            connect_timeout=_LDAP_CONNECT_TIMEOUT_S)

    def _bind(self, dn: str, password: str):
        import ldap3
        conn = ldap3.Connection(self._server(), user=dn, password=password,
                                auto_bind=False, receive_timeout=_LDAP_CONNECT_TIMEOUT_S,
                                raise_exceptions=False)
        try:
            ok = conn.bind()
        finally:
            try:
                conn.unbind()
            except Exception:
                pass
        return ok

    def _find_dn(self, username: str) -> Optional[str]:
        """Service-account search for the user's DN (None = no such user)."""
        import ldap3
        if "{username}" in self.user_dn_template or not self.bind_dn:
            return None
        conn = ldap3.Connection(self._server(), user=self.bind_dn,
                                password=self.cfg.get("bind_password") or "",
                                auto_bind=False, receive_timeout=_LDAP_CONNECT_TIMEOUT_S,
                                raise_exceptions=False)
        try:
            if not conn.bind():
                return None
            filtr = self.search_filter.replace("{username}", ldap3.utils.conv.escape_filter_chars(username))
            if not conn.search(self.search_base, filtr, attributes=["dn"]):
                return None
            entries = conn.entries
            if len(entries) != 1:
                return None
            return entries[0].entry_dn
        finally:
            try:
                conn.unbind()
            except Exception:
                pass

    def _user_dn(self, username: str) -> Optional[str]:
        if "{username}" in (self.user_dn_template or ""):
            return self.user_dn_template.replace("{username}", username)
        return self._find_dn(username)

    def verify_credentials(self, username: str, password: str) -> Optional[UserRecord]:
        # Local-first chain (zero route changes): a local-password row verifies
        # locally, so break-glass and pre-provisioned accounts keep working
        # under any provider. Every other username goes to the directory.
        # One generic failure either way -- never reveal which path was taken.
        row = auth_db.get_user_by_username((username or "").strip())
        if (row is not None and row["auth_provider"] == "local" and row["password_hash"]
                and row["is_active"] and not auth_db.is_locked(row)):
            return LocalAuthProvider().verify_credentials(username, password)
        username = (username or "").strip()
        if not username or not password:
            # empty passwords bind as "unauthenticated" on some servers and
            # SUCCEED -- reject before touching the directory
            return None
        if "/" in username or "\\" in username or "*" in username:
            return None
        dn = self._user_dn(username)
        if not dn:
            return None
        try:
            ok = self._bind(dn, password)
        except Exception:
            return None
        if not ok:
            return None
        return _provision_ldap_user(username, dn)

    def check_password(self, username: str, password: str) -> bool:
        """Bind-only re-verification (MFA enroll/disable for directory accounts):
        no provisioning, no row writes."""
        username = (username or "").strip()
        if not username or not password:
            return False
        dn = self._user_dn(username)
        if not dn:
            return False
        try:
            return bool(self._bind(dn, password))
        except Exception:
            return False


def _provision_ldap_user(username: str, dn: str) -> Optional[UserRecord]:
    """First successful bind creates the local row (default role only)."""
    row = auth_db.get_user_by_username(username)
    if row:
        if not row["is_active"]:
            return None
        try:
            ext = row["external_id"]
        except (IndexError, KeyError):
            ext = None
        if row["auth_provider"] not in ("ldap", "local") or (
                row["auth_provider"] == "ldap" and ext not in (None, dn)):
            return None
        auth_db.touch_login(row["id"])
    else:
        try:
            uid = auth_db.create_user(username=username, password_hash=None, auth_provider="ldap")
        except Exception:
            return None
        auth_db.db().execute("UPDATE users SET external_id = ? WHERE id = ?", (dn, uid))
        auth_db.db().commit()
        try:
            auth_db.assign_role(uid, "user")
        except Exception:
            pass
        auth_db.touch_login(uid)
        row = auth_db.get_user_by_id(uid)
        if not row:
            return None
    return UserRecord(
        id=row["id"], username=row["username"], display_name=row["display_name"] or row["username"],
        is_active=bool(row["is_active"]), is_super_admin=bool(row["is_super_admin"]),
        must_change_password=bool(row["must_change_password"]),
    )


def verify_account_password(user_id: int, password: str) -> bool:
    """Password re-check for MFA enroll/disable, whatever the account kind: the
    local hash, or a live directory bind for LDAP rows (checked, never stored).
    LDAP rows only verify while the ldap provider is configured -- a row left
    over from a decommissioned directory must not become passwordless."""
    row = auth_db.get_user_by_id(user_id)
    if not row or not password:
        return False
    if row["auth_provider"] == "local" and row["password_hash"]:
        return verify_password(password, row["password_hash"])
    if row["auth_provider"] == "ldap":
        try:
            from .small_model import APP_CONFIG
            cfg = (APP_CONFIG.get("auth") or {})
        except Exception:
            return False
        if (cfg.get("provider") or "local").strip().lower() != "ldap":
            return False
        try:
            return LdapAuthProvider(cfg.get("ldap") or {}).check_password(
                row["username"], password)
        except Exception:
            return False
    return False


def get_auth_provider() -> AuthProvider:
    # Single switch point for the directory cutover (config/app.json ->
    # {"auth": {"provider": "ldap", "ldap": {...}}}). Local password login is
    # always available for rows that have one (break-glass); the provider only
    # decides how *other* usernames verify (see LdapAuthProvider).
    try:
        from .small_model import APP_CONFIG
        cfg = (APP_CONFIG.get("auth") or {})
    except Exception:
        cfg = {}
    if (cfg.get("provider") or "local").strip().lower() == "ldap":
        return LdapAuthProvider((cfg.get("ldap") or {}))
    return LocalAuthProvider()

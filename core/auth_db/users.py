import json
import sqlite3
import sys
import time
from typing import Optional

from ..sqlite_util import transaction
from .common import LOCKOUT_S, MAX_FAILED_LOGINS, db

_USER_REFERRERS = (
    ("audit_log", "user_id"),
    ("role_permissions", "granted_by"),
    ("user_roles", "assigned_by"),
    ("knowledge_sources", "created_by"),
    ("knowledge_source_role_access", "granted_by"),
    ("api_tokens", "created_by"),
)
_USER_OWNED = (
    "user_roles",
    "auth_sessions",
    "user_allow_patterns",
    "user_mcp_servers",
    "api_tokens",
    "companion_devices",
    "agent_memory",
    "user_totp_backup",
)


def delete_user(user_id: int) -> None:
    with transaction(db()) as c:
        for table, col in _USER_REFERRERS:
            c.execute(f"UPDATE {table} SET {col} = NULL WHERE {col} = ?", (user_id,))
        for table in _USER_OWNED:
            c.execute(f"DELETE FROM {table} WHERE user_id = ?", (user_id,))
        c.execute("DELETE FROM users WHERE id = ?", (user_id,))


def get_user_by_username(username: str) -> Optional[sqlite3.Row]:
    return db().execute("SELECT * FROM users WHERE username = ?", (username,)).fetchone()


def get_user_by_id(user_id: int) -> Optional[sqlite3.Row]:
    return db().execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()


def count_users() -> int:
    return db().execute("SELECT COUNT(*) c FROM users").fetchone()["c"]


def create_user(username: str, password_hash: Optional[str], is_super_admin: bool = False,
                display_name: Optional[str] = None, email: Optional[str] = None,
                auth_provider: str = "local") -> int:
    now = time.time()
    cur = db().execute(
        "INSERT INTO users (username, email, password_hash, auth_provider, display_name, "
        "is_active, is_super_admin, created_at, updated_at) VALUES (?, ?, ?, ?, ?, 1, ?, ?, ?)",
        (username, email, password_hash, auth_provider, display_name or username,
         1 if is_super_admin else 0, now, now),
    )
    db().commit()
    return cur.lastrowid


def touch_login(user_id: int) -> None:
    db().execute("UPDATE users SET last_login_at = ?, failed_login_count = 0, locked_until = NULL "
                 "WHERE id = ?", (time.time(), user_id))
    db().commit()


def record_failed_login(user_id: int) -> bool:
    """Count a bad password; returns True when this attempt locked the account."""
    db().execute("UPDATE users SET failed_login_count = failed_login_count + 1 WHERE id = ?", (user_id,))
    row = db().execute("SELECT failed_login_count FROM users WHERE id = ?", (user_id,)).fetchone()
    locked = bool(row and row["failed_login_count"] >= MAX_FAILED_LOGINS)
    if locked:
        db().execute("UPDATE users SET locked_until = ?, failed_login_count = 0 WHERE id = ?",
                     (time.time() + LOCKOUT_S, user_id))
    db().commit()
    return locked


def unlock_user(user_id: int) -> None:
    db().execute("UPDATE users SET locked_until = NULL, failed_login_count = 0 WHERE id = ?", (user_id,))
    db().commit()


def is_locked(row) -> bool:
    try:
        until = row["locked_until"]
    except (IndexError, KeyError):
        return False
    return bool(until) and float(until) > time.time()


def get_user_role_names(user_id: int) -> list:
    rows = db().execute(
        "SELECT r.name FROM user_roles ur JOIN roles r ON r.id = ur.role_id WHERE ur.user_id = ?",
        (user_id,),
    ).fetchall()
    return [r["name"] for r in rows]


def get_user_permission_keys(user_id: int) -> set:
    rows = db().execute(
        "SELECT DISTINCT p.key FROM user_roles ur "
        "JOIN role_permissions rp ON rp.role_id = ur.role_id "
        "JOIN permissions p ON p.id = rp.permission_id "
        "WHERE ur.user_id = ?",
        (user_id,),
    ).fetchall()
    return {r["key"] for r in rows}


def assign_role(user_id: int, role_name: str, assigned_by: Optional[int] = None) -> None:
    role = db().execute("SELECT id FROM roles WHERE name = ?", (role_name,)).fetchone()
    if not role:
        raise ValueError(f"unknown role '{role_name}'")
    db().execute(
        "INSERT OR IGNORE INTO user_roles (user_id, role_id, assigned_at, assigned_by) VALUES (?, ?, ?, ?)",
        (user_id, role["id"], time.time(), assigned_by),
    )
    db().commit()


def get_user_allow_patterns(user_id: int) -> list:
    rows = db().execute(
        "SELECT pattern FROM user_allow_patterns WHERE user_id = ? ORDER BY id", (user_id,)
    ).fetchall()
    return [r["pattern"] for r in rows]


def add_user_allow_pattern(user_id: int, pattern: str) -> None:
    pattern = pattern.strip()
    if not pattern:
        return
    db().execute(
        "INSERT OR IGNORE INTO user_allow_patterns (user_id, pattern, created_at) VALUES (?, ?, ?)",
        (user_id, pattern, time.time()),
    )
    db().commit()


def remove_user_allow_pattern(user_id: int, pattern: str) -> None:
    db().execute(
        "DELETE FROM user_allow_patterns WHERE user_id = ? AND pattern = ?", (user_id, pattern.strip())
    )
    db().commit()


def list_user_mcp_servers(user_id: Optional[int] = None) -> list:
    """[(user_id, name, config_dict)] for one user, or every user when user_id is None."""
    q, args = "SELECT user_id, name, config FROM user_mcp_servers", []
    if user_id is not None:
        q += " WHERE user_id = ?"
        args.append(user_id)
    out = []
    for r in db().execute(q + " ORDER BY user_id, name", args):
        try:
            out.append((r["user_id"], r["name"], json.loads(r["config"])))
        except ValueError:
            print(f"[auth_db] bad user_mcp_servers config for {r['user_id']}/{r['name']}", file=sys.stderr)
    return out


def upsert_user_mcp_server(user_id: int, name: str, config: dict) -> None:
    now = time.time()
    db().execute(
        "INSERT INTO user_mcp_servers (user_id, name, config, created_at, updated_at) VALUES (?, ?, ?, ?, ?) "
        "ON CONFLICT(user_id, name) DO UPDATE SET config = excluded.config, updated_at = excluded.updated_at",
        (user_id, name, json.dumps(config), now, now),
    )
    db().commit()


def delete_user_mcp_server(user_id: int, name: str) -> None:
    db().execute("DELETE FROM user_mcp_servers WHERE user_id = ? AND name = ?", (user_id, name))
    db().commit()

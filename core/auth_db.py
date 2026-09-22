"""
core/auth_db.py - Users, roles, permissions, sessions, and audit log (sqlite).

Kept in its own auth.db file/connection, separate from projects.db, so
account/permission data has its own backup/lock domain and is never touched
by projects.db's cascade-delete logic. Follows the same idiom as core/db.py:
plain sqlite3, no ORM, CREATE TABLE IF NOT EXISTS + ALTER TABLE migrations
guarded by PRAGMA table_info.
"""

import sqlite3
import sys
import time
from typing import Optional

from .config import AUTH_DB_FILE

_BUILTIN_ROLES = ("admin", "user")

# Permission catalogue. Knowledge-source *visibility* is data-level
# (knowledge_source_role_access), not a permission key here.
PERMISSIONS = {
    "model.local.load": "Load/unload a local GGUF model and control server lifecycle",
    "model.local.configure": "Edit local model launch parameters",
    "settings.orchestration.configure": "Change the multi-agent orchestration engine mode",
    "settings.runtime.view": "View/edit sampling defaults, runtime keepalive, and GPU status in Settings",
    "usage.report.view": "View token usage / cost reports",
    "monitor.view": "View the real-time request monitor panel",
    "knowledge.manage": "Create/delete/edit organizational knowledge sources and their role access",
    "users.manage": "Create/deactivate users and assign roles",
    "roles.manage": "Create roles and edit role permission grants",
    "audit.view": "Read the audit log",
    "chat.use": "Use chat / agent features",
    "settings.input_guard": "Configure the input sanitizer (cloud-block patterns, prohibited prompt types, role restrictions)",
    "settings.shell.configure": "Edit the global shell command allowlist (capabilities.shell)",
}

# Permissions granted to the default 'user' role so regular accounts
# aren't locked out of the app itself.
_DEFAULT_USER_PERMISSIONS = ("chat.use",)


def _init_auth_db() -> sqlite3.Connection:
    conn = sqlite3.connect(str(AUTH_DB_FILE), check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.executescript("""
    CREATE TABLE IF NOT EXISTS users (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        username TEXT UNIQUE NOT NULL,
        email TEXT UNIQUE,
        password_hash TEXT,
        auth_provider TEXT NOT NULL DEFAULT 'local',
        external_id TEXT,
        display_name TEXT,
        is_active INTEGER NOT NULL DEFAULT 1,
        is_super_admin INTEGER NOT NULL DEFAULT 0,
        must_change_password INTEGER NOT NULL DEFAULT 0,
        failed_login_count INTEGER NOT NULL DEFAULT 0,
        locked_until REAL,
        created_at REAL NOT NULL,
        updated_at REAL NOT NULL,
        last_login_at REAL
    );
    CREATE UNIQUE INDEX IF NOT EXISTS idx_users_external ON users(auth_provider, external_id);

    CREATE TABLE IF NOT EXISTS roles (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT UNIQUE NOT NULL,
        description TEXT,
        is_builtin INTEGER NOT NULL DEFAULT 0,
        created_at REAL NOT NULL
    );

    CREATE TABLE IF NOT EXISTS permissions (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        key TEXT UNIQUE NOT NULL,
        description TEXT NOT NULL
    );

    CREATE TABLE IF NOT EXISTS role_permissions (
        role_id INTEGER NOT NULL REFERENCES roles(id) ON DELETE CASCADE,
        permission_id INTEGER NOT NULL REFERENCES permissions(id) ON DELETE CASCADE,
        granted_at REAL NOT NULL,
        granted_by INTEGER REFERENCES users(id),
        PRIMARY KEY (role_id, permission_id)
    );

    CREATE TABLE IF NOT EXISTS user_roles (
        user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
        role_id INTEGER NOT NULL REFERENCES roles(id) ON DELETE CASCADE,
        assigned_at REAL NOT NULL,
        assigned_by INTEGER REFERENCES users(id),
        PRIMARY KEY (user_id, role_id)
    );

    CREATE TABLE IF NOT EXISTS auth_sessions (
        id TEXT PRIMARY KEY,
        user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
        created_at REAL NOT NULL,
        last_seen_at REAL NOT NULL,
        expires_at REAL NOT NULL,
        ip TEXT,
        user_agent TEXT,
        revoked_at REAL
    );
    CREATE INDEX IF NOT EXISTS idx_auth_sessions_user ON auth_sessions(user_id);

    CREATE TABLE IF NOT EXISTS audit_log (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        ts REAL NOT NULL,
        user_id INTEGER REFERENCES users(id),
        username TEXT,
        action TEXT NOT NULL,
        resource TEXT,
        permission_key TEXT,
        result TEXT NOT NULL,
        detail TEXT,
        ip TEXT
    );
    CREATE INDEX IF NOT EXISTS idx_audit_ts ON audit_log(ts);
    CREATE INDEX IF NOT EXISTS idx_audit_user ON audit_log(user_id);

    CREATE TABLE IF NOT EXISTS knowledge_sources (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        title TEXT NOT NULL,
        kind TEXT NOT NULL,
        origin TEXT,
        stored_path TEXT,
        content_hash TEXT,
        status TEXT NOT NULL DEFAULT 'pending',
        error TEXT,
        created_by INTEGER REFERENCES users(id),
        created_at REAL NOT NULL,
        updated_at REAL NOT NULL
    );

    CREATE TABLE IF NOT EXISTS knowledge_source_role_access (
        source_id INTEGER NOT NULL REFERENCES knowledge_sources(id) ON DELETE CASCADE,
        role_id INTEGER NOT NULL REFERENCES roles(id) ON DELETE CASCADE,
        granted_at REAL NOT NULL,
        granted_by INTEGER REFERENCES users(id),
        PRIMARY KEY (source_id, role_id)
    );

    CREATE TABLE IF NOT EXISTS user_allow_patterns (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
        pattern TEXT NOT NULL,
        created_at REAL NOT NULL,
        UNIQUE(user_id, pattern)
    );
    """)
    conn.commit()
    _seed_defaults(conn)
    return conn


def _seed_defaults(conn: sqlite3.Connection) -> None:
    now = time.time()
    for key, desc in PERMISSIONS.items():
        conn.execute(
            "INSERT INTO permissions (key, description) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET description = excluded.description",
            (key, desc),
        )
    for name in _BUILTIN_ROLES:
        conn.execute(
            "INSERT OR IGNORE INTO roles (name, description, is_builtin, created_at) VALUES (?, ?, 1, ?)",
            (name, f"Built-in '{name}' role", now),
        )
    conn.commit()

    admin_role = conn.execute("SELECT id FROM roles WHERE name = 'admin'").fetchone()
    if admin_role:
        all_perm_ids = [r["id"] for r in conn.execute("SELECT id FROM permissions")]
        for pid in all_perm_ids:
            conn.execute(
                "INSERT OR IGNORE INTO role_permissions (role_id, permission_id, granted_at) VALUES (?, ?, ?)",
                (admin_role["id"], pid, now),
            )

    user_role = conn.execute("SELECT id FROM roles WHERE name = 'user'").fetchone()
    if user_role:
        for key in _DEFAULT_USER_PERMISSIONS:
            perm = conn.execute("SELECT id FROM permissions WHERE key = ?", (key,)).fetchone()
            if perm:
                conn.execute(
                    "INSERT OR IGNORE INTO role_permissions (role_id, permission_id, granted_at) VALUES (?, ?, ?)",
                    (user_role["id"], perm["id"], now),
                )
    conn.commit()


_auth_db: Optional[sqlite3.Connection] = None


def db() -> sqlite3.Connection:
    global _auth_db
    if _auth_db is None:
        _auth_db = _init_auth_db()
    return _auth_db


# ---------------- users ----------------
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
    db().execute("UPDATE users SET last_login_at = ?, failed_login_count = 0 WHERE id = ?",
                 (time.time(), user_id))
    db().commit()


def record_failed_login(user_id: int) -> None:
    db().execute("UPDATE users SET failed_login_count = failed_login_count + 1 WHERE id = ?", (user_id,))
    db().commit()


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


# ---------------- per-user shell allow patterns ----------------
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


# ---------------- knowledge sources ----------------
def create_knowledge_source(title: str, kind: str, origin: Optional[str] = None,
                             stored_path: Optional[str] = None, content_hash: Optional[str] = None,
                             status: str = "pending", created_by: Optional[int] = None) -> int:
    now = time.time()
    cur = db().execute(
        "INSERT INTO knowledge_sources (title, kind, origin, stored_path, content_hash, status, "
        "created_by, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (title, kind, origin, stored_path, content_hash, status, created_by, now, now),
    )
    db().commit()
    return cur.lastrowid


def update_knowledge_source_status(source_id: int, status: str, error: Optional[str] = None) -> None:
    db().execute("UPDATE knowledge_sources SET status = ?, error = ?, updated_at = ? WHERE id = ?",
                 (status, error, time.time(), source_id))
    db().commit()


def list_knowledge_sources() -> list:
    return db().execute("SELECT * FROM knowledge_sources ORDER BY created_at DESC").fetchall()


def get_knowledge_source(source_id: int) -> Optional[sqlite3.Row]:
    return db().execute("SELECT * FROM knowledge_sources WHERE id = ?", (source_id,)).fetchone()


def delete_knowledge_source(source_id: int) -> None:
    db().execute("DELETE FROM knowledge_sources WHERE id = ?", (source_id,))
    db().commit()


def get_source_role_names(source_id: int) -> list:
    rows = db().execute(
        "SELECT r.name FROM knowledge_source_role_access ksra JOIN roles r ON r.id = ksra.role_id "
        "WHERE ksra.source_id = ?", (source_id,),
    ).fetchall()
    return [r["name"] for r in rows]


def set_source_role_access(source_id: int, role_names: list, granted_by: Optional[int] = None) -> None:
    now = time.time()
    db().execute("DELETE FROM knowledge_source_role_access WHERE source_id = ?", (source_id,))
    for name in role_names:
        role = db().execute("SELECT id FROM roles WHERE name = ?", (name,)).fetchone()
        if role:
            db().execute(
                "INSERT OR IGNORE INTO knowledge_source_role_access "
                "(source_id, role_id, granted_at, granted_by) VALUES (?, ?, ?, ?)",
                (source_id, role["id"], now, granted_by),
            )
    db().commit()


def allowed_knowledge_source_ids_for_roles(role_names: list) -> set:
    # A source with zero rows in knowledge_source_role_access has no roles
    # assigned - the admin UI treats leaving the role picker empty as "allow
    # all users", so such sources are public rather than accessible to no one.
    public_rows = db().execute(
        "SELECT s.id FROM knowledge_sources s WHERE NOT EXISTS "
        "(SELECT 1 FROM knowledge_source_role_access ksra WHERE ksra.source_id = s.id)"
    ).fetchall()
    ids = {r["id"] for r in public_rows}
    if role_names:
        placeholders = ",".join("?" * len(role_names))
        rows = db().execute(
            f"SELECT DISTINCT ksra.source_id FROM knowledge_source_role_access ksra "
            f"JOIN roles r ON r.id = ksra.role_id WHERE r.name IN ({placeholders})",
            role_names,
        ).fetchall()
        ids |= {r["source_id"] for r in rows}
    return ids


def assign_role(user_id: int, role_name: str, assigned_by: Optional[int] = None) -> None:
    role = db().execute("SELECT id FROM roles WHERE name = ?", (role_name,)).fetchone()
    if not role:
        raise ValueError(f"unknown role '{role_name}'")
    db().execute(
        "INSERT OR IGNORE INTO user_roles (user_id, role_id, assigned_at, assigned_by) VALUES (?, ?, ?, ?)",
        (user_id, role["id"], time.time(), assigned_by),
    )
    db().commit()


# ---------------- sessions ----------------
def create_session_row(session_id_hash: str, user_id: int, expires_at: float,
                        ip: Optional[str], user_agent: Optional[str]) -> None:
    now = time.time()
    db().execute(
        "INSERT INTO auth_sessions (id, user_id, created_at, last_seen_at, expires_at, ip, user_agent) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (session_id_hash, user_id, now, now, expires_at, ip, user_agent),
    )
    db().commit()


def get_session_row(session_id_hash: str) -> Optional[sqlite3.Row]:
    return db().execute("SELECT * FROM auth_sessions WHERE id = ?", (session_id_hash,)).fetchone()


def touch_session(session_id_hash: str, expires_at: float) -> None:
    db().execute(
        "UPDATE auth_sessions SET last_seen_at = ?, expires_at = ? WHERE id = ?",
        (time.time(), expires_at, session_id_hash),
    )
    db().commit()


def revoke_session_row(session_id_hash: str) -> None:
    db().execute("UPDATE auth_sessions SET revoked_at = ? WHERE id = ?", (time.time(), session_id_hash))
    db().commit()


def revoke_all_sessions_for_user(user_id: int) -> None:
    db().execute("UPDATE auth_sessions SET revoked_at = ? WHERE user_id = ? AND revoked_at IS NULL",
                 (time.time(), user_id))
    db().commit()


# ---------------- audit log ----------------
def insert_audit(user_id: Optional[int], username: Optional[str], action: str,
                  resource: Optional[str], permission_key: Optional[str],
                  result: str, detail: Optional[str], ip: Optional[str]) -> None:
    try:
        db().execute(
            "INSERT INTO audit_log (ts, user_id, username, action, resource, permission_key, result, detail, ip) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (time.time(), user_id, username, action, resource, permission_key, result, detail, ip),
        )
        db().commit()
    except Exception as e:
        print(f"[auth_db] audit insert failed: {e}", file=sys.stderr)


def list_audit(since: float = 0.0, user_id: Optional[int] = None, limit: int = 500) -> list:
    q = "SELECT * FROM audit_log WHERE ts >= ?"
    args: list = [since]
    if user_id is not None:
        q += " AND user_id = ?"
        args.append(user_id)
    q += " ORDER BY ts DESC LIMIT ?"
    args.append(limit)
    return [dict(r) for r in db().execute(q, args)]

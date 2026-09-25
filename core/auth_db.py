"""
core/auth_db.py - Users, roles, permissions, sessions, and audit log (sqlite).

Kept in its own auth.db file/connection, separate from projects.db, so
account/permission data has its own backup/lock domain and is never touched
by projects.db's cascade-delete logic. Follows the same idiom as core/db.py:
plain sqlite3, no ORM, CREATE TABLE IF NOT EXISTS + ALTER TABLE migrations
guarded by PRAGMA table_info.
"""

import json
import sqlite3
import sys
import time
from typing import Optional

from .config import AUTH_DB_FILE
from .sqlite_util import ThreadLocalDB, transaction

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
    "database.manage": "Inspect system & workspace SQLite databases and execute SQL queries",
    "settings.shell.configure": "Edit the global shell command allowlist (capabilities.shell)",
    "settings.agents.configure": "Allow/deny Agent Library profiles and prompt commands (agent_library)",
    "settings.router.configure": "Edit agent routing rules and apply/dismiss usage-based router suggestions (router)",
    "git.push": "Push to git remotes / open pull requests (uses the server's git & GitHub credentials)",
    "capabilities.install": "Install/remove skills, plugins and connectors from the Customize catalog (shared by every user)",
}

# Keys that used to be in PERMISSIONS; removed from existing databases on start.
_RETIRED_PERMISSIONS = ("settings.integrations.configure",)

# Permissions granted to the default 'user' role so regular accounts
# aren't locked out of the app itself.
_DEFAULT_USER_PERMISSIONS = ("chat.use",)


def _init_auth_db() -> ThreadLocalDB:
    conn = ThreadLocalDB(AUTH_DB_FILE, row_factory=sqlite3.Row)
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

    -- personal MCP servers (Settings -> Capabilities -> MCP, "just for me"); config is
    -- the same secret-free JSON as capabilities.mcp_servers, secrets in the OS keychain
    CREATE TABLE IF NOT EXISTS user_mcp_servers (
        user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
        name TEXT NOT NULL,
        config TEXT NOT NULL,
        created_at REAL NOT NULL,
        updated_at REAL NOT NULL,
        PRIMARY KEY (user_id, name)
    );

    -- admin-issued API tokens (Bearer a770_pat_...); only the sha256 is stored
    CREATE TABLE IF NOT EXISTS api_tokens (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        token_hash TEXT UNIQUE NOT NULL,
        prefix TEXT NOT NULL,
        name TEXT NOT NULL,
        user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
        created_by INTEGER REFERENCES users(id),
        permissions TEXT NOT NULL,
        created_at REAL NOT NULL,
        expires_at REAL NOT NULL,
        last_used_at REAL,
        last_used_ip TEXT,
        revoked_at REAL
    );
    CREATE INDEX IF NOT EXISTS idx_api_tokens_user ON api_tokens(user_id);

    -- paired companion devices: device_hash is an HMAC of the OS machine id (the raw
    -- id is never stored), key_hash the sha256 of the device's pairing key
    CREATE TABLE IF NOT EXISTS companion_devices (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
        name TEXT NOT NULL,
        device_hash TEXT NOT NULL,
        key_hash TEXT UNIQUE NOT NULL,
        created_at REAL NOT NULL,
        last_seen_at REAL,
        revoked_at REAL
    );
    CREATE INDEX IF NOT EXISTS idx_companion_devices_user ON companion_devices(user_id);
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
    for key in _RETIRED_PERMISSIONS:
        conn.execute("DELETE FROM role_permissions WHERE permission_id IN "
                     "(SELECT id FROM permissions WHERE key = ?)", (key,))
        conn.execute("DELETE FROM permissions WHERE key = ?", (key,))
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


_auth_db: Optional[ThreadLocalDB] = None


def db() -> ThreadLocalDB:
    global _auth_db
    if _auth_db is None:
        _auth_db = _init_auth_db()
    return _auth_db


# ---------------- users ----------------
# Columns that point at users(id) with no ON DELETE action. They are cleared
# rather than cascaded: audit rows keep their username (PCI DSS 10 retention),
# and grants stay in place without the grantor link.
_USER_REFERRERS = (("audit_log", "user_id"), ("role_permissions", "granted_by"),
                   ("user_roles", "assigned_by"), ("knowledge_sources", "created_by"),
                   ("knowledge_source_role_access", "granted_by"), ("api_tokens", "created_by"))
# Rows owned by the user. ON DELETE CASCADE covers these when foreign keys are
# on; deleting them here as well keeps the result the same when they aren't.
_USER_OWNED = ("user_roles", "auth_sessions", "user_allow_patterns", "user_mcp_servers",
               "api_tokens", "companion_devices")


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


# PCI DSS 8.3.4: lock the account after at most 10 invalid attempts, for at
# least 30 minutes (or until an administrator unlocks it).
MAX_FAILED_LOGINS = 10
LOCKOUT_S = 30 * 60


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


# ---------------- per-user MCP servers ----------------
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


# ---------------- API tokens ----------------
_TOKEN_COLS = ("t.id, t.prefix, t.name, t.user_id, u.username, t.created_by, t.permissions, "
               "t.created_at, t.expires_at, t.last_used_at, t.last_used_ip, t.revoked_at")


def create_api_token_row(token_hash: str, prefix: str, name: str, user_id: int,
                         created_by: Optional[int], permissions: list, expires_at: float) -> int:
    cur = db().execute(
        "INSERT INTO api_tokens (token_hash, prefix, name, user_id, created_by, permissions, "
        "created_at, expires_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (token_hash, prefix, name, user_id, created_by, json.dumps(sorted(permissions)),
         time.time(), expires_at),
    )
    db().commit()
    return cur.lastrowid


def get_api_token_by_hash(token_hash: str) -> Optional[sqlite3.Row]:
    return db().execute("SELECT * FROM api_tokens WHERE token_hash = ?", (token_hash,)).fetchone()


def get_api_token(token_id: int) -> Optional[sqlite3.Row]:
    return db().execute("SELECT * FROM api_tokens WHERE id = ?", (token_id,)).fetchone()


def list_api_tokens(user_id: Optional[int] = None) -> list:
    sql = f"SELECT {_TOKEN_COLS} FROM api_tokens t LEFT JOIN users u ON u.id = t.user_id"
    args: tuple = ()
    if user_id is not None:
        sql += " WHERE t.user_id = ?"
        args = (user_id,)
    rows = db().execute(sql + " ORDER BY t.created_at DESC", args).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        d["permissions"] = json.loads(d["permissions"] or "[]")
        out.append(d)
    return out


def touch_api_token(token_id: int, ip: Optional[str]) -> None:
    db().execute("UPDATE api_tokens SET last_used_at = ?, last_used_ip = ? WHERE id = ?",
                 (time.time(), ip, token_id))
    db().commit()


def revoke_api_token(token_id: int) -> None:
    db().execute("UPDATE api_tokens SET revoked_at = ? WHERE id = ? AND revoked_at IS NULL",
                 (time.time(), token_id))
    db().commit()


# ---------------- companion devices ----------------
def create_companion_device(user_id: int, name: str, device_hash: str, key_hash: str) -> int:
    cur = db().execute(
        "INSERT INTO companion_devices (user_id, name, device_hash, key_hash, created_at) "
        "VALUES (?, ?, ?, ?, ?)", (user_id, name, device_hash, key_hash, time.time()))
    db().commit()
    return cur.lastrowid


def get_companion_device_by_key(key_hash: str) -> Optional[sqlite3.Row]:
    return db().execute("SELECT * FROM companion_devices WHERE key_hash = ?", (key_hash,)).fetchone()


def get_companion_device(device_row_id: int) -> Optional[sqlite3.Row]:
    return db().execute("SELECT * FROM companion_devices WHERE id = ?", (device_row_id,)).fetchone()


def list_companion_devices(user_id: Optional[int] = None) -> list:
    sql = ("SELECT d.id, d.user_id, u.username, d.name, d.created_at, d.last_seen_at, d.revoked_at "
           "FROM companion_devices d LEFT JOIN users u ON u.id = d.user_id")
    args: tuple = ()
    if user_id is not None:
        sql += " WHERE d.user_id = ?"
        args = (user_id,)
    return [dict(r) for r in db().execute(sql + " ORDER BY d.created_at DESC", args).fetchall()]


def revoke_companion_devices(user_id: int, device_hash: str) -> None:
    """Re-pairing a machine retires its earlier keys."""
    db().execute("UPDATE companion_devices SET revoked_at = ? WHERE user_id = ? AND device_hash = ? "
                 "AND revoked_at IS NULL", (time.time(), user_id, device_hash))
    db().commit()


def revoke_companion_device(device_row_id: int) -> None:
    db().execute("UPDATE companion_devices SET revoked_at = ? WHERE id = ? AND revoked_at IS NULL",
                 (time.time(), device_row_id))
    db().commit()


def touch_companion_device(device_row_id: int) -> None:
    db().execute("UPDATE companion_devices SET last_seen_at = ? WHERE id = ?", (time.time(), device_row_id))
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


def _audit_where(since: float = 0.0, until: Optional[float] = None, user_id: Optional[int] = None,
                 action: Optional[str] = None, result: Optional[str] = None,
                 ip: Optional[str] = None, text: Optional[str] = None) -> tuple[str, list]:
    """WHERE clause for audit filters; every value is bound, never interpolated."""
    clauses, args = ["ts >= ?"], [since or 0.0]
    if until:
        clauses.append("ts < ?")
        args.append(until)
    if user_id is not None:
        # 0 = system/anonymous entries (no user attached)
        if user_id == 0:
            clauses.append("user_id IS NULL")
        else:
            clauses.append("user_id = ?")
            args.append(user_id)
    if action:
        # "users.*" matches every action under that prefix
        if action.endswith(".*"):
            clauses.append("action LIKE ? ESCAPE '\\'")
            args.append(_like_escape(action[:-1]) + "%")
        else:
            clauses.append("action = ?")
            args.append(action)
    if result:
        clauses.append("result = ?")
        args.append(result)
    if ip:
        clauses.append("ip LIKE ? ESCAPE '\\'")
        args.append(_like_escape(ip) + "%")
    if text:
        pat = "%" + _like_escape(text) + "%"
        clauses.append("(resource LIKE ? ESCAPE '\\' OR detail LIKE ? ESCAPE '\\' OR action LIKE ? ESCAPE '\\' "
                       "OR permission_key LIKE ? ESCAPE '\\' OR username LIKE ? ESCAPE '\\')")
        args += [pat] * 5
    return " WHERE " + " AND ".join(clauses), args


def _like_escape(s: str) -> str:
    return s.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def query_audit(page: int = 1, page_size: int = 50, **filters) -> dict:
    """One page of audit entries (newest first) plus the total matching count."""
    where, args = _audit_where(**filters)
    total = db().execute("SELECT COUNT(*) FROM audit_log" + where, args).fetchone()[0]
    rows = db().execute(
        "SELECT * FROM audit_log" + where + " ORDER BY ts DESC, id DESC LIMIT ? OFFSET ?",
        args + [page_size, (page - 1) * page_size],
    )
    return {"entries": [dict(r) for r in rows], "total": total}


def iter_audit(max_rows: int = 50000, **filters) -> list:
    """All entries matching the filters (newest first), capped — for CSV export."""
    where, args = _audit_where(**filters)
    q = "SELECT * FROM audit_log" + where + " ORDER BY ts DESC, id DESC LIMIT ?"
    return [dict(r) for r in db().execute(q, args + [max_rows])]


def audit_facets() -> dict:
    """Distinct users / actions / results present in the log, for filter dropdowns."""
    users = [dict(r) for r in db().execute(
        "SELECT user_id, MAX(username) AS username, COUNT(*) AS n FROM audit_log "
        "GROUP BY user_id ORDER BY username COLLATE NOCASE")]
    actions = [dict(r) for r in db().execute(
        "SELECT action, COUNT(*) AS n FROM audit_log GROUP BY action ORDER BY action")]
    results = [r[0] for r in db().execute(
        "SELECT DISTINCT result FROM audit_log ORDER BY result")]
    return {"users": users, "actions": actions, "results": results}

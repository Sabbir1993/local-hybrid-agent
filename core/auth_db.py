"""
core/auth_db.py - Users, roles, permissions, sessions, and audit log (sqlite).

Kept in its own auth.db file/connection, separate from projects.db, so
account/permission data has its own backup/lock domain and is never touched
by projects.db's cascade-delete logic. Follows the same idiom as core/db.py:
plain sqlite3, no ORM, CREATE TABLE IF NOT EXISTS + ALTER TABLE migrations
guarded by PRAGMA table_info.
"""

import json
import re
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
    "custom_agents.publish": "Share custom agents with every user (public agents)",
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

    -- user-wise custom agents
    CREATE TABLE IF NOT EXISTS user_custom_agents (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER REFERENCES users(id) ON DELETE CASCADE,
        name TEXT NOT NULL,
        slug TEXT NOT NULL,
        description TEXT NOT NULL,
        icon TEXT DEFAULT '🤖',
        system_prompt TEXT NOT NULL,
        tool_allowlist TEXT DEFAULT '[]',
        input_template TEXT DEFAULT '',
        preferred_lane TEXT DEFAULT 'auto',
        reasoning_effort TEXT DEFAULT 'medium',
        temperature REAL DEFAULT 0.4,
        is_public INTEGER DEFAULT 0,
        created_at REAL NOT NULL,
        updated_at REAL NOT NULL
    );
    CREATE UNIQUE INDEX IF NOT EXISTS idx_user_custom_agents_slug_user ON user_custom_agents(COALESCE(user_id, 0), slug);
    CREATE INDEX IF NOT EXISTS idx_user_custom_agents_user ON user_custom_agents(user_id);
    CREATE INDEX IF NOT EXISTS idx_user_custom_agents_public ON user_custom_agents(is_public);
    """)
    conn.commit()
    _seed_defaults(conn)
    try:
        _seed_starter_custom_agents(conn)
    except Exception as _e:
        print(f"[auth_db] seed_starter_custom_agents failed: {_e}", file=sys.stderr)
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


# ---------------- user-wise custom agents ----------------

STARTER_CUSTOM_AGENTS = [
    {
        "name": "Email Analyzer & Drafter",
        "slug": "email-analyzer",
        "icon": "📧",
        "description": "Analyzes email threads, assesses urgency/sentiment, extracts action items, and drafts responses.",
        "system_prompt": (
            "You are an expert Email Analyzer and Executive Assistant Agent.\n\n"
            "Your workflow:\n"
            "1. Read and analyze the input email message or thread:\n"
            "   - Core sender, recipient(s), context, and subject.\n"
            "   - Sentiment & tone (e.g. appreciative, frustrated, formal, inquiring).\n"
            "   - Urgency rating: Low, Medium, High, or Urgent/Time-Sensitive.\n"
            "2. Extract explicit deliverables, deadlines, questions asked, and action items for each participant.\n"
            "3. Draft tailored, professional reply options (e.g., Option A: Formal & Detailed, Option B: Concise Acknowledgement).\n"
            "4. Format the final output with clean Markdown headings, bulleted lists, and clear next steps."
        ),
        "tool_allowlist": ["read_file", "doc_inspect", "read_file_chunk", "web_search", "web_fetch"],
        "input_template": "Please analyze this email message or thread:\n\n{input}",
        "preferred_lane": "auto",
        "reasoning_effort": "medium",
        "temperature": 0.3,
        "is_public": 1,
    },
    {
        "name": "System & Metric Reporter",
        "slug": "system-reporter",
        "icon": "📊",
        "description": "Inspects system health, server logs, hardware telemetry, and formats structured status summaries.",
        "system_prompt": (
            "You are an autonomous System Telemetry and Technical Reporting Agent.\n\n"
            "Your workflow:\n"
            "1. Inspect relevant log files, system metrics, hardware status, or service health indicators.\n"
            "2. Execute Python scripts using 'run_python' if data parsing, statistical calculation, or regex parsing is required.\n"
            "3. Synthesize findings into an executive-ready System Report covering:\n"
            "   - Executive Status (Operational / Degraded / Incident)\n"
            "   - Core Metrics (Throughput, error rates, resource utilization)\n"
            "   - Anomalies or Root Causes discovered\n"
            "   - Actionable Mitigation or Next Steps."
        ),
        "tool_allowlist": ["read_file", "list_files", "grep", "run_python", "write_file"],
        "input_template": "Generate a system report for:\n{input}",
        "preferred_lane": "auto",
        "reasoning_effort": "medium",
        "temperature": 0.2,
        "is_public": 1,
    },
    {
        "name": "Code QA & Test Automator",
        "slug": "test-qa-automator",
        "icon": "🧪",
        "description": "Inspects code, writes thorough unit and regression tests, executes test suites, and verifies fixes.",
        "system_prompt": (
            "You are an autonomous Code Quality & Test Engineering Agent.\n\n"
            "Your workflow:\n"
            "1. Inspect the target source code, boundary conditions, edge cases, and potential failure modes.\n"
            "2. Write comprehensive unit or integration tests matching the project's testing conventions.\n"
            "3. Run the tests using 'run_python' or 'run_shell' to ensure they pass and accurately catch edge cases.\n"
            "4. If bugs are found, diagnose the root cause and provide targeted fixes using 'edit_file'."
        ),
        "tool_allowlist": ["read_file", "list_files", "grep", "write_file", "edit_file", "run_python", "run_shell"],
        "input_template": "Inspect and create/run automated tests for:\n{input}",
        "preferred_lane": "auto",
        "reasoning_effort": "high",
        "temperature": 0.2,
        "is_public": 1,
    },
    {
        "name": "Excel & Data Transformer",
        "slug": "data-transformer",
        "icon": "📈",
        "description": "Ingests messy spreadsheets, CSVs or JSON files, cleans data, performs aggregations, and generates clean tables.",
        "system_prompt": (
            "You are an expert Data Wrangling and Spreadsheet Automation Agent.\n\n"
            "Your workflow:\n"
            "1. Inspect incoming data files (CSV, Excel, JSON, XML) using 'doc_inspect' or 'read_file'.\n"
            "2. Write Python scripts with 'run_python' to clean, transform, deduplicate, and aggregate the records.\n"
            "3. Generate cleaned output files (CSV or Excel) and present an executive data summary of findings."
        ),
        "tool_allowlist": ["read_file", "read_file_chunk", "doc_inspect", "doc_edit", "run_python", "write_file"],
        "input_template": "Clean, transform, and analyze the following data:\n{input}",
        "preferred_lane": "auto",
        "reasoning_effort": "medium",
        "temperature": 0.2,
        "is_public": 1,
    },
    {
        "name": "Release Notes & Changelog Synthesizer",
        "slug": "release-notes-writer",
        "icon": "📝",
        "description": "Reviews git commits, file diffs, and feature updates to write categorized changelogs and release notes.",
        "system_prompt": (
            "You are a Technical Writer and Software Release Management Agent.\n\n"
            "Your workflow:\n"
            "1. Review recent project changes via 'list_diff', git logs, or user summaries.\n"
            "2. Group changes into clear categories: Features, Bug Fixes, Performance Improvements, Breaking Changes, and Internal Tooling.\n"
            "3. Write human-friendly, concise release notes highlighting impact for end users and developers."
        ),
        "tool_allowlist": ["list_diff", "read_file", "list_files", "grep", "write_file"],
        "input_template": "Generate release notes for the following changes:\n{input}",
        "preferred_lane": "auto",
        "reasoning_effort": "medium",
        "temperature": 0.4,
        "is_public": 1,
    }
]


def _custom_agent_row(r) -> dict:
    if not r:
        return {}
    d = dict(r)
    try:
        if d.get("tool_allowlist"):
            d["tool_allowlist"] = json.loads(d["tool_allowlist"])
        else:
            d["tool_allowlist"] = []
    except Exception:
        d["tool_allowlist"] = []
    d["is_public"] = bool(d.get("is_public", 0))
    return d


def db_list_custom_agents(user_id: Optional[int] = None, include_public: bool = True) -> list[dict]:
    """List custom agents accessible to user: user's own agents + system templates / public agents."""
    conn = db()
    if user_id is not None:
        if include_public:
            rows = conn.execute(
                """SELECT * FROM user_custom_agents
                   WHERE user_id = ? OR user_id IS NULL OR is_public = 1
                   ORDER BY (CASE WHEN user_id = ? THEN 0 WHEN user_id IS NULL THEN 1 ELSE 2 END), name COLLATE NOCASE""",
                (user_id, user_id)
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM user_custom_agents WHERE user_id = ? ORDER BY name COLLATE NOCASE",
                (user_id,)
            ).fetchall()
    else:
        rows = conn.execute(
            "SELECT * FROM user_custom_agents WHERE user_id IS NULL OR is_public = 1 ORDER BY name COLLATE NOCASE"
        ).fetchall()
    return [_custom_agent_row(r) for r in rows]


def db_get_custom_agent(agent_id: int, user_id: Optional[int] = None) -> Optional[dict]:
    """Get agent by id, checking access (owner, public, or system template)."""
    conn = db()
    row = conn.execute("SELECT * FROM user_custom_agents WHERE id = ?", (agent_id,)).fetchone()
    if not row:
        return None
    agent = _custom_agent_row(row)
    if user_id is not None:
        # Accessible if owned by user, or public, or system template (user_id is None)
        if agent.get("user_id") not in (user_id, None) and not agent.get("is_public"):
            return None
    return agent


def db_get_custom_agent_by_slug(slug: str, user_id: Optional[int] = None) -> Optional[dict]:
    """Find a custom agent by slug: the user's own first, then system templates.
    Other users' public agents are never resolved by slug -- spawn_agent must not
    run a prompt someone else published (they are usable only when picked in the UI)."""
    conn = db()
    s = (slug or "").strip().lower()
    if user_id is not None:
        row = conn.execute(
            "SELECT * FROM user_custom_agents WHERE user_id = ? AND LOWER(slug) = ?",
            (user_id, s)
        ).fetchone()
        if row:
            return _custom_agent_row(row)
    row = conn.execute(
        "SELECT * FROM user_custom_agents WHERE user_id IS NULL AND LOWER(slug) = ?", (s,)
    ).fetchone()
    return _custom_agent_row(row) if row else None


class CustomAgentSlugTaken(ValueError):
    pass


def slugify_custom_agent(text) -> str:
    s = re.sub(r"[^a-z0-9_-]", "-", str(text or "").strip().lower())
    return re.sub(r"-{2,}", "-", s).strip("-")


def _slug_taken(conn, user_id: Optional[int], slug: str, exclude_id: Optional[int] = None) -> bool:
    row = conn.execute(
        "SELECT id FROM user_custom_agents WHERE COALESCE(user_id, 0) = ? AND LOWER(slug) = ? AND id != ?",
        (user_id or 0, slug.lower(), exclude_id or 0)).fetchone()
    return row is not None


def _tools_json(tools) -> str:
    return json.dumps([str(t) for t in tools]) if isinstance(tools, list) else "[]"


def db_create_custom_agent(user_id: Optional[int], data: dict) -> dict:
    """Create a new custom agent for a user."""
    conn = db()
    now = time.time()
    name = (data.get("name") or "Custom Agent").strip()
    slug = slugify_custom_agent(data.get("slug") or name) or "agent"
    description = (data.get("description") or "").strip()
    icon = (data.get("icon") or "🤖").strip()
    system_prompt = (data.get("system_prompt") or "").strip()
    tool_json = _tools_json(data.get("tool_allowlist") or [])
    input_template = (data.get("input_template") or "").strip()
    preferred_lane = (data.get("preferred_lane") or "auto").strip()
    reasoning_effort = (data.get("reasoning_effort") or "medium").strip()
    temp = data.get("temperature")
    temperature = float(temp) if temp is not None else 0.4
    is_public = 1 if data.get("is_public") else 0

    base_slug = slug
    counter = 1
    while _slug_taken(conn, user_id, slug):
        counter += 1
        slug = f"{base_slug}-{counter}"

    with transaction(conn) as c:
        cursor = c.execute(
            """INSERT INTO user_custom_agents (
                user_id, name, slug, description, icon, system_prompt, tool_allowlist,
                input_template, preferred_lane, reasoning_effort, temperature, is_public,
                created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (user_id, name, slug, description, icon, system_prompt, tool_json,
             input_template, preferred_lane, reasoning_effort, temperature, is_public,
             now, now)
        )
        new_id = cursor.lastrowid
    return db_get_custom_agent(new_id)


def db_update_custom_agent(agent_id: int, user_id: int, data: dict) -> Optional[dict]:
    """Update an agent owned by user (or admin updating any agent).
    Raises CustomAgentSlugTaken if the new slug collides with another agent."""
    conn = db()
    row = conn.execute("SELECT * FROM user_custom_agents WHERE id = ?", (agent_id,)).fetchone()
    if not row:
        return None
    # Only owner or super admin can edit
    if row["user_id"] != user_id:
        user_row = conn.execute("SELECT is_super_admin FROM users WHERE id = ?", (user_id,)).fetchone()
        if not (user_row and user_row["is_super_admin"]):
            return None

    # an explicit null means "leave unchanged", never the string "None"
    data = {k: v for k, v in data.items() if v is not None}
    now = time.time()
    fields = []
    args = []

    for k in ("name", "description", "icon", "system_prompt", "input_template", "preferred_lane", "reasoning_effort"):
        if k in data:
            fields.append(f"{k} = ?")
            args.append(str(data[k]).strip())
    if "slug" in data:
        slug = slugify_custom_agent(data["slug"])
        if slug and slug != row["slug"]:
            if _slug_taken(conn, row["user_id"], slug, exclude_id=agent_id):
                raise CustomAgentSlugTaken(slug)
            fields.append("slug = ?")
            args.append(slug)
    if "tool_allowlist" in data:
        fields.append("tool_allowlist = ?")
        args.append(_tools_json(data["tool_allowlist"]))
    if "temperature" in data:
        fields.append("temperature = ?")
        args.append(float(data["temperature"]))
    if "is_public" in data:
        fields.append("is_public = ?")
        args.append(1 if data["is_public"] else 0)

    if not fields:
        return _custom_agent_row(row)

    fields.append("updated_at = ?")
    args.append(now)
    args.append(agent_id)

    with transaction(conn) as c:
        c.execute(f"UPDATE user_custom_agents SET {', '.join(fields)} WHERE id = ?", args)

    # access was checked above; re-fetch unfiltered so a super admin editing
    # someone's private agent gets the row back instead of a spurious 403
    return db_get_custom_agent(agent_id)


def db_delete_custom_agent(agent_id: int, user_id: int) -> bool:
    """Delete an agent owned by user (or by super-admin)."""
    conn = db()
    row = conn.execute("SELECT * FROM user_custom_agents WHERE id = ?", (agent_id,)).fetchone()
    if not row:
        return False
    if row["user_id"] != user_id:
        user_row = conn.execute("SELECT is_super_admin FROM users WHERE id = ?", (user_id,)).fetchone()
        if not (user_row and user_row["is_super_admin"]):
            return False

    with transaction(conn) as c:
        c.execute("DELETE FROM user_custom_agents WHERE id = ?", (agent_id,))
    return True


def db_fork_custom_agent(agent_id: int, user_id: int, new_name: Optional[str] = None) -> Optional[dict]:
    """Clone an existing agent / template into user's own collection."""
    source = db_get_custom_agent(agent_id, user_id)
    if not source:
        return None
    now = time.time()
    base_name = new_name or f"{source['name']} (Fork)"
    base_slug = f"{source['slug']}-copy"
    # Ensure unique slug for user
    slug = base_slug
    counter = 1
    conn = db()
    while _slug_taken(conn, user_id, slug):
        counter += 1
        slug = f"{base_slug}-{counter}"

    data = {
        "name": base_name,
        "slug": slug,
        "description": source["description"],
        "icon": source["icon"],
        "system_prompt": source["system_prompt"],
        "tool_allowlist": source["tool_allowlist"],
        "input_template": source["input_template"],
        "preferred_lane": source["preferred_lane"],
        "reasoning_effort": source["reasoning_effort"],
        "temperature": source["temperature"],
        "is_public": 0,
    }
    return db_create_custom_agent(user_id, data)


_STARTER_FIELDS = ("name", "description", "icon", "system_prompt", "tool_allowlist", "input_template",
                   "preferred_lane", "reasoning_effort", "temperature", "is_public")


def _seed_starter_custom_agents(conn) -> None:
    """Insert missing starter templates, and refresh ones nobody has edited
    (updated_at == created_at) so template improvements ship with the code.
    Runs from _init_auth_db, i.e. against whichever AUTH_DB_FILE is configured."""
    now = time.time()
    for ag in STARTER_CUSTOM_AGENTS:
        vals = [_tools_json(ag[k]) if k == "tool_allowlist" else ag[k] for k in _STARTER_FIELDS]
        existing = conn.execute(
            "SELECT * FROM user_custom_agents WHERE user_id IS NULL AND slug = ?", (ag["slug"],)
        ).fetchone()
        if not existing:
            conn.execute(
                f"INSERT INTO user_custom_agents (user_id, slug, {', '.join(_STARTER_FIELDS)}, created_at, updated_at) "
                f"VALUES (NULL, ?, {', '.join('?' * len(_STARTER_FIELDS))}, ?, ?)",
                (ag["slug"], *vals, now, now))
        elif existing["updated_at"] == existing["created_at"] and \
                [existing[k] for k in _STARTER_FIELDS] != vals:
            conn.execute(
                f"UPDATE user_custom_agents SET {', '.join(k + ' = ?' for k in _STARTER_FIELDS)} WHERE id = ?",
                (*vals, existing["id"]))
    conn.commit()


def seed_starter_custom_agents() -> None:
    _seed_starter_custom_agents(db())

import sqlite3
import sys
import time

from .common import (
    _BUILTIN_ROLES,
    _DEFAULT_USER_PERMISSIONS,
    _RETIRED_PERMISSIONS,
    PERMISSIONS,
)

SCHEMA_SCRIPT = """
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
    totp_secret TEXT,
    totp_enabled INTEGER NOT NULL DEFAULT 0,
    totp_last_counter INTEGER NOT NULL DEFAULT 0,
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
    mfa_verified INTEGER NOT NULL DEFAULT 0,
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

CREATE TABLE IF NOT EXISTS knowledge_categories (
    name TEXT PRIMARY KEY,
    cloud_ok INTEGER NOT NULL DEFAULT 0,
    created_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS knowledge_rules (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL UNIQUE,
    kind TEXT NOT NULL,
    pattern TEXT NOT NULL,
    enabled INTEGER NOT NULL DEFAULT 1,
    builtin INTEGER NOT NULL DEFAULT 0,
    created_at REAL NOT NULL
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

CREATE TABLE IF NOT EXISTS user_mcp_servers (
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    name TEXT NOT NULL,
    config TEXT NOT NULL,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL,
    PRIMARY KEY (user_id, name)
);

CREATE TABLE IF NOT EXISTS agent_memory (
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    path TEXT NOT NULL,
    description TEXT NOT NULL DEFAULT '',
    content TEXT NOT NULL DEFAULT '',
    version TEXT NOT NULL,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL,
    PRIMARY KEY (user_id, path)
);

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

CREATE TABLE IF NOT EXISTS user_totp_backup (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    code_hash TEXT NOT NULL,
    created_at REAL NOT NULL,
    used_at REAL,
    UNIQUE(user_id, code_hash)
);
CREATE INDEX IF NOT EXISTS idx_user_totp_backup_user ON user_totp_backup(user_id);

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
    work_dir TEXT DEFAULT '',
    share_status TEXT DEFAULT '',
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_user_custom_agents_slug_user ON user_custom_agents(COALESCE(user_id, 0), slug);
CREATE INDEX IF NOT EXISTS idx_user_custom_agents_user ON user_custom_agents(user_id);
CREATE INDEX IF NOT EXISTS idx_user_custom_agents_public ON user_custom_agents(is_public);
"""


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


def _seed_knowledge_classification(conn) -> None:
    """Default categories and sensitive-content rules, only when the tables are empty so an admin's
    edits (including deleting a default) are never undone."""
    from .knowledge_defaults import DEFAULT_CATEGORIES, DEFAULT_RULES
    now = time.time()
    if not conn.execute("SELECT 1 FROM knowledge_categories LIMIT 1").fetchone():
        conn.executemany("INSERT INTO knowledge_categories (name, cloud_ok, created_at) VALUES (?, ?, ?)",
                         [(n, ok, now) for n, ok in DEFAULT_CATEGORIES])
    if not conn.execute("SELECT 1 FROM knowledge_rules LIMIT 1").fetchone():
        conn.executemany("INSERT INTO knowledge_rules (name, kind, pattern, enabled, builtin, created_at) "
                         "VALUES (?, ?, ?, 1, 1, ?)", [(n, k, p, now) for n, k, p in DEFAULT_RULES])
    conn.commit()


def init_tables(conn) -> None:
    conn.executescript(SCHEMA_SCRIPT)
    conn.commit()
    _cols = {r[1] for r in conn.execute("PRAGMA table_info(users)")}
    for col, ddl in (("totp_secret", "TEXT"),
                     ("totp_enabled", "INTEGER NOT NULL DEFAULT 0"),
                     ("totp_last_counter", "INTEGER NOT NULL DEFAULT 0")):
        if col not in _cols:
            conn.execute(f"ALTER TABLE users ADD COLUMN {col} {ddl}")
            conn.commit()
    _sess_cols = {r[1] for r in conn.execute("PRAGMA table_info(auth_sessions)")}
    if "mfa_verified" not in _sess_cols:
        conn.execute("ALTER TABLE auth_sessions ADD COLUMN mfa_verified INTEGER NOT NULL DEFAULT 0")
        conn.commit()
    if "work_dir" not in {r[1] for r in conn.execute("PRAGMA table_info(user_custom_agents)")}:
        conn.execute("ALTER TABLE user_custom_agents ADD COLUMN work_dir TEXT DEFAULT ''")
        conn.commit()
    if "share_status" not in {r[1] for r in conn.execute("PRAGMA table_info(user_custom_agents)")}:
        # sharing now needs approval; agents that were already public keep their approved standing
        conn.execute("ALTER TABLE user_custom_agents ADD COLUMN share_status TEXT DEFAULT ''")
        conn.execute("UPDATE user_custom_agents SET share_status = 'approved' WHERE is_public = 1 AND user_id IS NOT NULL")
        conn.commit()
    if "cloud_ok" not in {r[1] for r in conn.execute("PRAGMA table_info(knowledge_sources)")}:
        # admin decides per source whether cloud models may read it; existing sources stay local-only
        conn.execute("ALTER TABLE knowledge_sources ADD COLUMN cloud_ok INTEGER NOT NULL DEFAULT 0")
        conn.commit()
    if "category" not in {r[1] for r in conn.execute("PRAGMA table_info(knowledge_sources)")}:
        conn.execute("ALTER TABLE knowledge_sources ADD COLUMN category TEXT")
        conn.commit()
    _seed_knowledge_classification(conn)
    _seed_defaults(conn)
    try:
        from .custom_agents_seed import _seed_starter_custom_agents
        _seed_starter_custom_agents(conn)
    except Exception as _e:
        print(f"[auth_db] seed_starter_custom_agents failed: {_e}", file=sys.stderr)

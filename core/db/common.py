import sqlite3
import sys

from ..config import PROJECTS_DB_FILE
from ..sqlite_util import ThreadLocalDB, transaction


def _init_projects_db() -> ThreadLocalDB:
    db_file = getattr(sys.modules.get("core.db"), "PROJECTS_DB_FILE", PROJECTS_DB_FILE)
    conn = ThreadLocalDB(db_file, row_factory=sqlite3.Row)
    conn.executescript("""
    CREATE TABLE IF NOT EXISTS projects (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT NOT NULL,
        created_at REAL NOT NULL,
        workspace_dir TEXT,
        allow_patterns TEXT DEFAULT '[]',
        user_id INTEGER,
        device_id TEXT DEFAULT 'default',
        device_name TEXT DEFAULT 'Default Device',
        UNIQUE(name, user_id, device_id)
    );
    CREATE TABLE IF NOT EXISTS sessions (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        project_id INTEGER REFERENCES projects(id) ON DELETE CASCADE,
        title TEXT NOT NULL,
        created_at REAL NOT NULL,
        user_id INTEGER,
        agent_id INTEGER
    );
    CREATE TABLE IF NOT EXISTS messages (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        session_id INTEGER NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
        role TEXT NOT NULL,
        content TEXT NOT NULL,
        meta TEXT,
        created_at REAL NOT NULL
    );
    CREATE INDEX IF NOT EXISTS idx_messages_session ON messages(session_id);
    CREATE TABLE IF NOT EXISTS plan_items (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        session_id INTEGER NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
        ord INTEGER NOT NULL,
        text TEXT NOT NULL,
        status TEXT NOT NULL DEFAULT 'pending',
        note TEXT,
        created_at REAL NOT NULL,
        updated_at REAL NOT NULL
    );
    CREATE INDEX IF NOT EXISTS idx_plan_items_session ON plan_items(session_id);
    CREATE TABLE IF NOT EXISTS session_working_memory (
        session_id INTEGER PRIMARY KEY REFERENCES sessions(id) ON DELETE CASCADE,
        user_id INTEGER,
        content TEXT NOT NULL,
        updated_at REAL NOT NULL
    );
    CREATE TABLE IF NOT EXISTS doc_files (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER NOT NULL,
        session_id INTEGER,
        name TEXT NOT NULL,
        kind TEXT NOT NULL,
        location TEXT NOT NULL DEFAULT 'common',
        parent_id INTEGER REFERENCES doc_files(id),
        version INTEGER NOT NULL DEFAULT 1,
        sha256 TEXT,
        source_spec TEXT,
        created_at REAL NOT NULL
    );
    CREATE INDEX IF NOT EXISTS idx_doc_files_user_name ON doc_files(user_id, name);
    """)
    # migration: older DBs lack the workspace_dir column or allow_patterns
    cols = [r[1] for r in conn.execute("PRAGMA table_info(projects)")]
    if "workspace_dir" not in cols:
        conn.execute("ALTER TABLE projects ADD COLUMN workspace_dir TEXT")
    if "allow_patterns" not in cols:
        conn.execute("ALTER TABLE projects ADD COLUMN allow_patterns TEXT DEFAULT '[]'")
    if "user_id" not in cols:
        conn.execute("ALTER TABLE projects ADD COLUMN user_id INTEGER")
    if "device_id" not in cols:
        conn.execute("ALTER TABLE projects ADD COLUMN device_id TEXT DEFAULT 'default'")
    if "device_name" not in cols:
        conn.execute("ALTER TABLE projects ADD COLUMN device_name TEXT DEFAULT 'Default Device'")
    session_cols = [r[1] for r in conn.execute("PRAGMA table_info(sessions)")]
    if "user_id" not in session_cols:
        conn.execute("ALTER TABLE sessions ADD COLUMN user_id INTEGER")
    if "agent_id" not in session_cols:
        conn.execute("ALTER TABLE sessions ADD COLUMN agent_id INTEGER")
    try:
        conn.execute("""
            UPDATE sessions SET agent_id = 1
            WHERE agent_id IS NULL AND (
                title LIKE '%System & Metric Report%'
                OR title LIKE '%Email Analyzer%'
                OR title LIKE '%Code QA & Test%'
                OR title LIKE '%Architecture & System%'
                OR title LIKE '%Security & Vulnerability%'
                OR title LIKE '%Doc & Knowledge%'
                OR title LIKE '📊%'
                OR title LIKE '📧%'
                OR title LIKE '🧪%'
                OR title LIKE '🏗️%'
                OR title LIKE '🛡️%'
                OR title LIKE '📚%'
                OR title LIKE '% Agent'
                OR title LIKE '% Reporter'
            )
        """)
    except Exception:
        pass

    # migration: check if UNIQUE(name, user_id, device_id) is present.
    has_user_dev_unique = False
    for idx in conn.execute("PRAGMA index_list('projects')"):
        if idx["unique"]:
            idx_cols = sorted([r["name"] for r in conn.execute(f"PRAGMA index_info('{idx['name']}')")])
            if idx_cols == ["device_id", "name", "user_id"]:
                has_user_dev_unique = True
                break

    if not has_user_dev_unique:
        old_cols = [r[1] for r in conn.execute("PRAGMA table_info(projects)")]
        has_dev_id = "device_id" in old_cols
        has_dev_name = "device_name" in old_cols
        has_allow_pats = "allow_patterns" in old_cols
        has_uid = "user_id" in old_cols
        conn.commit()
        conn.execute("PRAGMA foreign_keys = OFF")
        conn.executescript(f"""
            DROP TABLE IF EXISTS projects_new;
            CREATE TABLE projects_new (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                created_at REAL NOT NULL,
                workspace_dir TEXT,
                allow_patterns TEXT DEFAULT '[]',
                user_id INTEGER,
                device_id TEXT DEFAULT 'default',
                device_name TEXT DEFAULT 'Default Device',
                UNIQUE(name, user_id, device_id)
            );
            INSERT INTO projects_new (id, name, created_at, workspace_dir, allow_patterns, user_id, device_id, device_name)
                SELECT id, name, created_at, workspace_dir,
                       {'allow_patterns' if has_allow_pats else "'[]'"},
                       {'user_id' if has_uid else 'NULL'},
                       {'COALESCE(device_id, "default")' if has_dev_id else "'default'"},
                       {'COALESCE(device_name, "Default Device")' if has_dev_name else "'Default Device'"}
                FROM projects;
            PRAGMA legacy_alter_table = ON;
            DROP TABLE projects;
            ALTER TABLE projects_new RENAME TO projects;
            PRAGMA legacy_alter_table = OFF;
        """)
        conn.execute(f"PRAGMA foreign_keys = {'ON' if conn.foreign_keys else 'OFF'}")

    from ..request_context import normalize_device_id
    for (old,) in conn.execute("SELECT DISTINCT device_id FROM projects WHERE device_id LIKE 'dev_win_%' "
                               "OR device_id LIKE 'dev_lnx_%' OR device_id LIKE 'dev_mac_%'").fetchall():
        new = normalize_device_id(old)
        if new == old:
            continue
        try:
            conn.execute("UPDATE projects SET device_id = ? WHERE device_id = ?", (new, old))
        except sqlite3.IntegrityError:
            conn.execute("UPDATE OR IGNORE projects SET device_id = ? WHERE device_id = ?", (new, old))

    conn.commit()
    return conn


_projects_db = _init_projects_db()


def _get_projects_db() -> ThreadLocalDB:
    pkg = sys.modules.get("core.db")
    return getattr(pkg, "_projects_db", _projects_db) if pkg else _projects_db


def db_clear_all_projects_data() -> dict:
    """Danger-zone: wipe every project/session/message/plan-item for every user."""
    counts = {}
    for table in ("session_working_memory", "plan_items", "messages", "sessions", "projects"):
        counts[table] = _projects_db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
    with transaction(_projects_db) as c:
        for table in ("session_working_memory", "plan_items", "messages", "sessions", "projects"):
            c.execute(f"DELETE FROM {table}")
    return counts


def backfill_legacy_owner(user_id: int) -> None:
    """One-time migration hook: attribute pre-auth rows to the bootstrap super-admin."""
    _projects_db.execute("UPDATE projects SET user_id = ? WHERE user_id IS NULL", (user_id,))
    _projects_db.execute("UPDATE sessions SET user_id = ? WHERE user_id IS NULL", (user_id,))
    _projects_db.commit()


def _forget_session_memory(sids: list) -> None:
    """Drop the memory-search chunks of deleted sessions."""
    if not sids:
        return
    try:
        from ..memory import delete_session_chunks
        delete_session_chunks(sids)
    except Exception as e:
        print(f"[db] memory cleanup for deleted sessions failed: {e}", file=sys.stderr)

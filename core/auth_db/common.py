import sqlite3
import sys
from typing import Optional

from ..config import AUTH_DB_FILE
from ..sqlite_util import ThreadLocalDB

_BUILTIN_ROLES = ("admin", "user")

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

_RETIRED_PERMISSIONS = ("settings.integrations.configure",)
_DEFAULT_USER_PERMISSIONS = ("chat.use",)

MAX_FAILED_LOGINS = 10
LOCKOUT_S = 30 * 60

_auth_db: Optional[ThreadLocalDB] = None


def get_auth_db_file():
    mod = sys.modules.get("core.auth_db")
    return getattr(mod, "AUTH_DB_FILE", AUTH_DB_FILE) if mod else AUTH_DB_FILE


def _init_auth_db() -> ThreadLocalDB:
    from .schema import init_tables
    db_file = get_auth_db_file()
    conn = ThreadLocalDB(db_file, row_factory=sqlite3.Row)
    init_tables(conn)
    return conn


def db() -> ThreadLocalDB:
    mod = sys.modules.get("core.auth_db")
    if mod and hasattr(mod, "_auth_db") and mod._auth_db is not None:
        return mod._auth_db
    global _auth_db
    if _auth_db is None:
        _auth_db = _init_auth_db()
        if mod:
            mod._auth_db = _auth_db
    return _auth_db

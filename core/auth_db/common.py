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
    "custom_agents.publish": "Approve custom agents that users want to share with everyone",
}

# How the Users & Roles screen presents each permission: a module (the part of the product it
# belongs to), a kind (read = look only, write = change settings or data, action = do something
# with effects) and plain-language wording for people who are not engineers. Keys and enforcement
# are unchanged; anything not listed falls under "Other".
PERMISSION_MODULES = (
    ("chat", "Chat & Agent"),
    ("models", "Models"),
    ("behaviour", "Agent behaviour"),
    ("knowledge", "Knowledge & safety"),
    ("insight", "Monitoring & audit"),
    ("access", "People & access"),
    ("sharing", "Sharing & code"),
)
PERMISSION_META = {
    "chat.use": ("chat", "action", "Use chat and the agent",
                 "Lets the person talk to the assistant and run agent tasks. Without this they can sign in but cannot use it."),
    "model.local.load": ("models", "action", "Start and stop the local model",
                         "Loads or unloads the AI model that runs on this computer. Takes GPU memory while loaded."),
    "model.local.configure": ("models", "write", "Change local model settings",
                              "Edits how the local model starts (memory use, context size, GPU split). A wrong value can make it fail to start."),
    "settings.orchestration.configure": ("models", "write", "Choose which model does which job",
                                         "Sets the main model, helper model and cloud providers. Affects cost and where data is sent."),
    "settings.runtime.view": ("models", "read", "See hardware and runtime settings",
                              "Shows GPU status, speed defaults and keep-alive timing. View only."),
    "settings.router.configure": ("behaviour", "write", "Change routing rules",
                                  "Edits how requests are sent to the fast or the strong model and applies suggested improvements."),
    "settings.agents.configure": ("behaviour", "write", "Allow or block agent profiles",
                                  "Decides which built-in agent profiles and shortcut commands people can use."),
    "settings.shell.configure": ("behaviour", "write", "Set which commands the agent may run",
                                 "Edits the list of computer commands the agent can run without asking. Powerful - grant with care."),
    "capabilities.install": ("behaviour", "action", "Install skills, plugins and connectors",
                             "Adds or removes extras from the catalog for everyone who uses this system."),
    "knowledge.manage": ("knowledge", "write", "Manage the company knowledge base",
                         "Adds, edits and deletes company documents and decides which roles can read them."),
    "settings.input_guard": ("knowledge", "write", "Manage the safety filter",
                             "Sets what text is blocked or hidden before it goes to a cloud model (for example card numbers)."),
    "monitor.view": ("insight", "read", "Watch live activity",
                     "Shows requests as they happen with speed and size. View only."),
    "usage.report.view": ("insight", "read", "See usage and cost reports",
                          "Shows how much each model and day was used. View only."),
    "audit.view": ("insight", "read", "Read the audit log",
                   "Shows who did what and when, including denied actions. View only."),
    "database.manage": ("insight", "write", "Open the database and run queries",
                        "Lets the person read and change the system's stored data directly. Very powerful - admins only."),
    "users.manage": ("access", "write", "Create and manage users and tokens",
                     "Adds people, disables them, changes their role and issues API tokens."),
    "roles.manage": ("access", "write", "Edit roles and their permissions",
                     "Changes what each role is allowed to do. Whoever holds this can grant themselves more access."),
    "custom_agents.publish": ("sharing", "action", "Approve shared agents",
                              "Reviews agents that users ask to share, and makes approved ones available to everyone in Customize."),
    "git.push": ("sharing", "action", "Push code and open pull requests",
                 "Sends code to remote repositories using this system's saved git credentials."),
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

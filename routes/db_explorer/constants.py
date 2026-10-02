"""
routes/db_explorer/constants.py - Shared constants and models for the DB Explorer.
"""

import re

from pydantic import BaseModel, Field

from core.config import (
    AUTH_DB_FILE,
    MEMORY_DB_FILE,
    PROJECTS_DB_FILE,
    USAGE_DB_FILE,
)

# Pre-defined system databases
SYSTEM_DBS = {
    "auth": {
        "id": "auth",
        "name": "auth.db",
        "title": "Authentication & RBAC",
        "description": "Users, roles, permissions, sessions, audit log, and knowledge sources",
        "path": AUTH_DB_FILE,
        "is_system": True,
    },
    "projects": {
        "id": "projects",
        "name": "projects.db",
        "title": "Projects & Chat Sessions",
        "description": "Registered projects, chat sessions, message histories, and plan items",
        "path": PROJECTS_DB_FILE,
        "is_system": True,
    },
    "usage": {
        "id": "usage",
        "name": "usage.db",
        "title": "Usage & Performance Metrics",
        "description": "LLM request metrics, prompt/completion tokens, cache hit metrics, and t/s speed",
        "path": USAGE_DB_FILE,
        "is_system": True,
    },
    "memory": {
        "id": "memory",
        "name": "memory.db",
        "title": "Semantic Vector Memory",
        "description": "Text chunks, cosine embeddings, and past session recall index",
        "path": MEMORY_DB_FILE,
        "is_system": True,
    },
}


class QueryRequest(BaseModel):
    sql: str = Field(..., min_length=1, max_length=65536, description="SQL query to execute")
    max_rows: int = Field(500, ge=1, le=5000, description="Maximum number of rows to return")


# auth.db columns nobody should read through the console.
#   password_hash    - credential hash (the whole point of storing it hashed)
#   totp_secret      - the TOTP SEED. core/totp.py turns any seed into a valid 6-digit code for
#                      any user at any time, so exposing this is a complete MFA bypass: no second
#                      factor, no rate limit, and no audit row, because it is a SELECT and not a
#                      login (routes/auth.py's _mfa_throttled never fires).
#   auth_sessions.id - live session token; the sha256 is enough to hijack if the DB is copied.
# database.manage is granted to every admin (core/auth_db/schema.py seeds the admin role with all
# permissions), so "hidden here" is the only thing standing between an ordinary admin and every
# enrolled account's second factor.
_HIDDEN_COLUMNS = {
    ("users", "password_hash"),
    ("users", "totp_secret"),
    ("auth_sessions", "id"),
}
# Introspection pragmas take a table/index argument; settable ones must be bare (no "= v").
_INTROSPECT_PRAGMAS = {"table_info", "table_xinfo", "index_list", "index_info", "index_xinfo",
                       "foreign_key_list", "database_list", "compile_options", "page_count"}
_SETTABLE_PRAGMAS = {"page_size", "user_version", "schema_version", "journal_mode"}
_VACUUM_RE = re.compile(r"\bVACUUM\b", re.IGNORECASE)

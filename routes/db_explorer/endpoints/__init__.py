"""routes/db_explorer/endpoints.py - FastAPI route handlers for the DB Explorer.

Permission-restricted via 'database.manage'. Allows administrators to inspect
all system SQLite databases (auth.db, projects.db, usage.db, memory.db) and any
active project workspace databases, view schema and table definitions, and
execute SQL queries with detailed execution metrics and audit logging.
"""

from .base import (
    router,
)
from .browse import (
    list_databases,
    get_schema,
    get_table_data,
)
from .query import (
    execute_query,
)

__all__ = [
    "router",
    "list_databases",
    "get_schema",
    "get_table_data",
    "execute_query",
]

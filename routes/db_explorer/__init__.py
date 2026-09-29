"""
routes/db_explorer - Web Database Explorer & SQL Query Console package.

Permission-restricted via 'database.manage'. Allows administrators to inspect
all system SQLite databases (auth.db, projects.db, usage.db, memory.db) and any
active project workspace databases, view schema and table definitions, and
execute SQL queries with detailed execution metrics and audit logging.

Re-exports all public symbols so that existing imports remain valid:
    from routes.db_explorer import router
    from routes.db_explorer import SYSTEM_DBS, QueryRequest
    etc.
"""

from .constants import (
    SYSTEM_DBS,
    QueryRequest,
    _HIDDEN_COLUMNS,
    _INTROSPECT_PRAGMAS,
    _SETTABLE_PRAGMAS,
    _VACUUM_RE,
)
from .helpers import (
    _authorizer,
    _connect,
    _discover_workspace_dbs,
    _get_table_count,
    _resolve_db,
    _sanitize_cell_value,
)
from .endpoints import (
    router,
    list_databases,
    get_schema,
    execute_query,
    get_table_data,
)

__all__ = [
    # router
    "router",
    # constants
    "SYSTEM_DBS",
    "QueryRequest",
    "_HIDDEN_COLUMNS",
    "_INTROSPECT_PRAGMAS",
    "_SETTABLE_PRAGMAS",
    "_VACUUM_RE",
    # helpers
    "_sanitize_cell_value",
    "_discover_workspace_dbs",
    "_resolve_db",
    "_authorizer",
    "_connect",
    "_get_table_count",
    # endpoints
    "list_databases",
    "get_schema",
    "execute_query",
    "get_table_data",
]

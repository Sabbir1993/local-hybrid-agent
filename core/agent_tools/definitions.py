from .finish import tool_finish
from .common_writer import tool_write_file_common
from .diffs import tool_list_diff, tool_revert
from .file_ops import (
    tool_append_file,
    tool_edit_file,
    tool_grep,
    tool_insert_at_line,
    tool_list_files,
    tool_read_file,
    tool_run_python,
    tool_write_file,
)
from .memory_tools import MEMORY_IMPLS, MEMORY_SCHEMAS
from .plans import tool_create_plan, tool_get_plan, tool_update_plan_item
from .schemas import AGENT_CORE_TOOLS, AGENT_TOOLS, CHAT_WRITE_FILE_SCHEMA
from .search_tools import (
    tool_analyze_image,
    tool_search_knowledge_base,
    tool_search_memory,
)

TOOL_IMPLS = {
    "list_files": tool_list_files,
    "read_file": tool_read_file,
    "grep": tool_grep,
    "write_file": tool_write_file,
    "write_file_common": tool_write_file_common,
    "append_file": tool_append_file,
    "edit_file": tool_edit_file,
    "insert_at_line": tool_insert_at_line,
    "run_python": tool_run_python,
    "list_diff": tool_list_diff,
    "revert": tool_revert,
    "analyze_image": tool_analyze_image,
    "search_memory": tool_search_memory,
    "search_knowledge_base": tool_search_knowledge_base,
    "create_plan": tool_create_plan,
    "update_plan_item": tool_update_plan_item,
    "get_plan": tool_get_plan,
    "finish": tool_finish,
}

TOOL_IMPLS.update(MEMORY_IMPLS)
AGENT_TOOLS.extend(MEMORY_SCHEMAS)

__all__ = [
    "AGENT_TOOLS",
    "AGENT_CORE_TOOLS",
    "CHAT_WRITE_FILE_SCHEMA",
    "TOOL_IMPLS",
]

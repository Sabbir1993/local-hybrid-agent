from .. import companion_bridge
from ..db import _projects_db
from ..request_context import (
    get_current_device_id,
    get_current_user_id,
    set_current_user,
)
from ..small_model import COMMON_ROOT
from .common_writer import tool_write_file_common
from .definitions import (
    AGENT_CORE_TOOLS,
    AGENT_TOOLS,
    CHAT_WRITE_FILE_SCHEMA,
    TOOL_IMPLS,
)
from .diffs import (
    MAX_DIFF_LINES,
    _file_diffs,
    _record_diff,
    _remote_read_or_none,
    _snapshot_change,
    diff_summary,
    pop_file_diff,
    tool_list_diff,
    tool_revert,
)
from .dom_convert import _markdown_to_html_dom
from .dom_theme import DOC_CSS, SLIDES_CSS, render_doc_page, render_slides_page
from .file_ops import (
    tool_edit_file,
    tool_grep,
    tool_list_files,
    tool_read_file,
    tool_run_python,
    tool_write_file,
)
from .pdf_render import (
    _DOCTYPE_RX,
    _PDF_CSP,
    _find_chromium_binary,
    _lock_down_html,
    _pdf_looks_valid,
    _render_html_to_pdf,
    _save_text_or_markdown_as_pdf,
)
from .plans import (
    _PLAN_STATUS_MARKS,
    _format_plan,
    _plan_session_var,
    get_plan_context,
    set_plan_context,
    tool_create_plan,
    tool_get_plan,
    tool_update_plan_item,
)
from .search_tools import (
    MAX_IMAGE_BYTES,
    tool_analyze_image,
    tool_search_knowledge_base,
    tool_search_memory,
)
from .tabular import (
    _CODE_FENCE_LANGS,
    _CODE_LINE_RE,
    _MD_SEP_RE,
    _fenced_blocks,
    _looks_like_code,
    _markdown_table_rows,
    _modal_column_rows,
    _parse_tabular_text,
    _sniff_delimited,
    _split_delimited,
)
from .workspace import (
    DOCUMENT_EXTS,
    MAX_EDIT_BYTES,
    MAX_TOOL_OUTPUT,
    WorkspaceAccessDenied,
    _active_project,
    _common_resolve,
    _remote_uid,
    _user_device_key,
    _within,
    _ws_changes,
    _ws_resolve,
    active_workspace,
    common_workspace,
    get_active_project,
    require_device_workspace,
    set_active_project,
    user_common_root,
    workspace_label,
)

# Registered last, after AGENT_TOOLS/TOOL_IMPLS/active_workspace exist: core.subagent
# imports back from this module and from core.agent_loop, so wiring it in here (rather
# than at the top of the file) avoids a circular import during startup.
from ..subagent import SPAWN_AGENT_SCHEMA, tool_spawn_agent  # noqa: E402
AGENT_TOOLS.append(SPAWN_AGENT_SCHEMA)
TOOL_IMPLS["spawn_agent"] = tool_spawn_agent

# Document tools (doc_inspect / doc_edit / doc_create): same late wiring, since
# core.doc_tools imports the workspace helpers from this module.
from ..doc_tools import AGENT_IMPLS as _DOC_IMPLS, DOC_SCHEMAS as _DOC_SCHEMAS  # noqa: E402
AGENT_TOOLS.extend(_DOC_SCHEMAS)
AGENT_CORE_TOOLS.extend(s for s in _DOC_SCHEMAS if s["function"]["name"] in ("doc_inspect", "doc_edit"))
TOOL_IMPLS.update(_DOC_IMPLS)

__all__ = [
    "MAX_TOOL_OUTPUT",
    "MAX_EDIT_BYTES",
    "DOCUMENT_EXTS",
    "_active_project",
    "_ws_changes",
    "_user_device_key",
    "get_active_project",
    "set_active_project",
    "WorkspaceAccessDenied",
    "require_device_workspace",
    "active_workspace",
    "workspace_label",
    "_remote_uid",
    "user_common_root",
    "common_workspace",
    "_within",
    "_common_resolve",
    "_ws_resolve",
    "_CODE_FENCE_LANGS",
    "_CODE_LINE_RE",
    "_MD_SEP_RE",
    "_fenced_blocks",
    "_looks_like_code",
    "_markdown_table_rows",
    "_split_delimited",
    "_modal_column_rows",
    "_sniff_delimited",
    "_parse_tabular_text",
    "SLIDES_CSS",
    "DOC_CSS",
    "render_slides_page",
    "render_doc_page",
    "_markdown_to_html_dom",
    "_PDF_CSP",
    "_DOCTYPE_RX",
    "_find_chromium_binary",
    "_lock_down_html",
    "_render_html_to_pdf",
    "_pdf_looks_valid",
    "_save_text_or_markdown_as_pdf",
    "tool_list_files",
    "tool_read_file",
    "tool_grep",
    "tool_write_file",
    "tool_write_file_common",
    "tool_edit_file",
    "tool_run_python",
    "_snapshot_change",
    "_file_diffs",
    "MAX_DIFF_LINES",
    "_remote_read_or_none",
    "diff_summary",
    "_record_diff",
    "pop_file_diff",
    "tool_list_diff",
    "tool_revert",
    "MAX_IMAGE_BYTES",
    "tool_analyze_image",
    "tool_search_memory",
    "tool_search_knowledge_base",
    "_plan_session_var",
    "_PLAN_STATUS_MARKS",
    "set_plan_context",
    "get_plan_context",
    "_format_plan",
    "tool_create_plan",
    "tool_update_plan_item",
    "tool_get_plan",
    "AGENT_TOOLS",
    "AGENT_CORE_TOOLS",
    "CHAT_WRITE_FILE_SCHEMA",
    "TOOL_IMPLS",
]

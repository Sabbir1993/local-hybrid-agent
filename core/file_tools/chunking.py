from typing import Optional
from .. import companion_bridge
from ..agent_tools import (
    MAX_TOOL_OUTPUT,
    WorkspaceAccessDenied,
    _common_resolve,
    _ws_resolve,
    require_device_workspace,
)
from .constants import CHUNK_SIZE, MAX_CHUNK_CHARS, _BINARY_DOCS
from .extractors import extract_file_content


async def _read_project_text(path_arg: str) -> tuple[str, Optional[str]]:
    """("ok", text) for a project text file read via the companion, otherwise
    ("missing" | "binary" | "error: ...", None). No project on this device
    counts as missing."""
    try:
        uid, _ws = require_device_workspace()
        p = _ws_resolve(path_arg)
    except WorkspaceAccessDenied:
        return "missing", None
    except PermissionError as e:
        return f"error: {e}", None
    if p.suffix.lower() in _BINARY_DOCS:
        return "binary", None
    import sys
    cb = sys.modules.get("core.companion_bridge") or companion_bridge
    cb_call = getattr(cb, "call", companion_bridge.call)
    data = await cb_call(uid, "fs.read", {"path": str(p)})
    content = data.get("content")
    return ("ok", content) if content is not None else ("missing", None)


async def tool_read_file_chunk(args: dict) -> str:
    """Agent tool: page through large file content in chunks.

    Project files live on the user's machine and are read through the companion
    -- never from the server's disk at the same path. Anything not in the
    project falls back to uploaded attachments in the server's common space.
    """
    path_arg = args.get("path") or args.get("file")
    if not path_arg:
        raise ValueError("path required")
    offset = int(args.get("offset_chars", 0))
    # model-chosen: capped so one call can't pull a whole document into context
    chunk = max(1, min(int(args.get("max_chars", CHUNK_SIZE)), MAX_CHUNK_CHARS))

    status, full_text = await _read_project_text(path_arg)
    if full_text is None:
        try:
            up = _common_resolve(path_arg)
        except PermissionError:
            up = None
        if up is None or not up.is_file():
            if status == "binary":
                return (f"error: {path_arg} is a binary document on your machine; read it with "
                        "run_python (e.g. openpyxl / pdfplumber / python-docx) instead")
            if status.startswith("error:"):
                return status
            return f"error: File not found: '{path_arg}'"
        if up.suffix.lower() in _BINARY_DOCS or up.suffix.lower() == ".csv":
            full_text, _ = extract_file_content(up, max_chars=999_999_999)  # extract all
        else:
            full_text = up.read_text(encoding="utf-8", errors="replace")

    total = len(full_text)
    if offset >= total:
        return f"(end of file - {total} total chars)"

    slice_text = full_text[offset:offset + chunk]
    next_offset = offset + len(slice_text)
    remaining = total - next_offset

    result = slice_text
    if remaining > 0:
        result += f"\n\n[chunk: chars {offset}-{next_offset - 1} of {total} total. Call read_file_chunk('{path_arg}', offset_chars={next_offset}) to read next chunk. {remaining} chars remaining.]"
    else:
        result += f"\n\n[end of file - read {next_offset} of {total} chars]"
    return result


def register_file_tools() -> None:
    """Register file intelligence tools into the global tool registry."""
    from ..registry import registry

    registry.register(
        "read_file_chunk",
        tool_read_file_chunk,
        {
            "type": "function",
            "function": {
                "name": "read_file_chunk",
                "description": (
                    "Read a chunk of a file starting at a character offset. "
                    "Works with ALL file types: text files, Excel (.xlsx/.xls), CSV, PDF, "
                    "PowerPoint (.pptx), Word (.docx). Use this to page through large files "
                    "that were truncated in the initial context injection. "
                    "Check if the previous chunk said '[chunk: ...]' - if so, call this tool "
                    "with the indicated next offset_chars to read more."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "path": {
                            "type": "string",
                            "description": "Workspace-relative path to the file",
                        },
                        "offset_chars": {
                            "type": "integer",
                            "description": "Character offset to start reading from (default 0)",
                        },
                        "max_chars": {
                            "type": "integer",
                            "description": "Max characters to return (default 10000)",
                        },
                    },
                    "required": ["path"],
                },
            },
        },
        source="builtin",
        meta={"label": "Read file chunk (pagination for large files)"},
        replace=True,
    )

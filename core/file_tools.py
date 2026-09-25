"""core/file_tools.py — Document-file extraction engine and agent tools.

Supports: .xlsx/.xls (Excel), .csv, .pdf, .pptx/.ppt (PowerPoint),
          .docx/.doc (Word), .json, .xml (pass-through text).

Public surface:
  extract_file_content(path, max_chars=12000) -> str
      Server-side: called at upload time to produce the injected text block.
  tool_read_file_chunk(args) -> str
      Agent tool: lets the LLM page through large files on demand.
  register_file_tools() -> None
      Called once at startup to register file tools into the global registry.
"""

import csv
import io
import json
import os
from pathlib import Path
from typing import Optional, Union

from . import companion_bridge
from .agent_tools import (MAX_TOOL_OUTPUT, WorkspaceAccessDenied, _common_resolve, _ws_resolve,
                          require_device_workspace)

# ------------------------------------------------------------------
# Extension -> MIME type map (for /agent/download responses)
# ------------------------------------------------------------------
MIME_MAP = {
    ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    ".xls":  "application/vnd.ms-excel",
    ".csv":  "text/csv",
    ".pdf":  "application/pdf",
    ".pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
    ".ppt":  "application/vnd.ms-powerpoint",
    ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    ".doc":  "application/msword",
    ".json": "application/json",
    ".xml":  "application/xml",
    ".png":  "image/png",
    ".jpg":  "image/jpeg",
    ".jpeg": "image/jpeg",
    ".webp": "image/webp",
}

DOCUMENT_EXTENSIONS = set(MIME_MAP.keys()) | {".txt", ".md"}

# Default chunking limits
DEFAULT_MAX_CHARS = 12_000    # ~3000 tokens - safe for 4096-token context windows
CHUNK_SIZE = 10_000           # size for read_file_chunk pages
MAX_CHUNK_CHARS = 40_000      # largest page a caller may ask for


# ------------------------------------------------------------------
# Internal per-format extractors
# ------------------------------------------------------------------

def _extract_excel(path: Path) -> str:
    """Extract cell data from .xlsx / .xls workbooks via openpyxl or xlrd."""
    suffix = path.suffix.lower()
    parts = []
    if suffix == ".xls":
        try:
            import xlrd
            wb = xlrd.open_workbook(str(path))
            for sheet in wb.sheets():
                parts.append(f"## Sheet: {sheet.name}")
                for row_idx in range(sheet.nrows):
                    row = [str(sheet.cell_value(row_idx, c)) for c in range(sheet.ncols)]
                    parts.append("\t".join(row))
        except ImportError:
            parts.append("(xlrd not installed - install with: pip install xlrd)")
        except Exception as e:
            parts.append(f"(error reading .xls: {e})")
    else:
        try:
            import openpyxl
            wb = openpyxl.load_workbook(str(path), read_only=True, data_only=True)
            for sheet_name in wb.sheetnames:
                ws = wb[sheet_name]
                parts.append(f"## Sheet: {sheet_name}")
                row_count = 0
                for row in ws.iter_rows(values_only=True):
                    row_strs = [str(c) if c is not None else "" for c in row]
                    if any(v.strip() for v in row_strs):   # skip blank rows
                        parts.append("\t".join(row_strs))
                        row_count += 1
                        if row_count >= 2000:     # cap at 2000 rows per sheet
                            parts.append(f"... (truncated at 2000 rows)")
                            break
            wb.close()
        except ImportError:
            parts.append("(openpyxl not installed - install with: pip install openpyxl)")
        except Exception as e:
            parts.append(f"(error reading Excel: {e})")
    return "\n".join(parts)


def _extract_csv(path: Path) -> str:
    """Extract all rows from a CSV file."""
    parts = []
    try:
        with path.open(encoding="utf-8", errors="replace", newline="") as f:
            reader = csv.reader(f)
            row_count = 0
            for row in reader:
                parts.append("\t".join(row))
                row_count += 1
                if row_count >= 3000:
                    parts.append("... (truncated at 3000 rows)")
                    break
    except Exception as e:
        parts.append(f"(error reading CSV: {e})")
    return "\n".join(parts)


def _extract_pdf(path: Path) -> str:
    """Extract text and tables from a PDF via pdfplumber."""
    parts = []
    try:
        import pdfplumber
        with pdfplumber.open(str(path)) as pdf:
            page_count = len(pdf.pages)
            parts.append(f"(PDF - {page_count} page{'s' if page_count != 1 else ''})")
            for i, page in enumerate(pdf.pages[:100]):   # cap at 100 pages
                page_num = i + 1
                text = (page.extract_text() or "").strip()
                tables = page.extract_tables() or []
                if text or tables:
                    parts.append(f"\n--- Page {page_num} ---")
                if text:
                    parts.append(text)
                for tbl in tables:
                    parts.append("")
                    for row in tbl:
                        row_strs = [str(c) if c is not None else "" for c in row]
                        parts.append("\t".join(row_strs))
            if page_count > 100:
                parts.append(f"\n... ({page_count - 100} more pages - use read_file_chunk to access them)")
    except ImportError:
        parts.append("(pdfplumber not installed - install with: pip install pdfplumber)")
    except Exception as e:
        parts.append(f"(error reading PDF: {e})")
    return "\n".join(parts)


def _extract_office(path: Path) -> str:
    """PowerPoint / Word via core.doc_ops: the same addressed outline doc_edit
    targets (slides, shapes, notes, tables; paragraphs, tables, headers)."""
    from . import doc_ops
    if path.suffix.lower() in (".ppt", ".doc"):
        return (f"(legacy {path.suffix} format - text cannot be read; open it in Office and save as "
                f"{path.suffix}x, then attach it again)")
    try:
        return doc_ops.inspect(path.read_bytes(), path.name, max_chars=10 ** 9)
    except Exception as e:
        return f"(error reading {path.suffix} document: {e})"


_extract_pptx = _extract_docx = _extract_office


def _extract_text(path: Path) -> str:
    """Read plain text files (json, xml, txt, md, etc.)."""
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except Exception as e:
        return f"(error reading file: {e})"


# ------------------------------------------------------------------
# Public extraction entry point
# ------------------------------------------------------------------

def extract_file_content(path, max_chars: int = DEFAULT_MAX_CHARS):
    """Extract text content from a document file.

    Returns:
        (content: str, truncated: bool)
        content is the extracted text, truncated if it exceeds max_chars.
    """
    p = Path(path)
    if not p.is_file():
        return f"(file not found: {path})", False

    suffix = p.suffix.lower()
    if suffix in (".xlsx", ".xls"):
        text = _extract_excel(p)
    elif suffix == ".csv":
        text = _extract_csv(p)
    elif suffix == ".pdf":
        text = _extract_pdf(p)
    elif suffix in (".pptx", ".ppt"):
        text = _extract_pptx(p)
    elif suffix in (".docx", ".doc"):
        text = _extract_docx(p)
    elif suffix in (".png", ".jpg", ".jpeg", ".webp"):
        text = f"[IMAGE ATTACHMENT: {p.name}]\nWorkspace path: {p.name}\n(Perceptual pre-processing ready: Call analyze_image for grounded OCR coordinates or scene summary.)"
    else:
        # json, xml, txt, md, etc. - raw text
        text = _extract_text(p)

    if len(text) > max_chars:
        return text[:max_chars], True
    return text, False


# ------------------------------------------------------------------
# Agent tool: read_file_chunk
# ------------------------------------------------------------------

_BINARY_DOCS = (".xlsx", ".xls", ".pdf", ".pptx", ".ppt", ".docx", ".doc")


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
    data = await companion_bridge.call(uid, "fs.read", {"path": str(p)})
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


# ------------------------------------------------------------------
# Registry bootstrap
# ------------------------------------------------------------------

def register_file_tools() -> None:
    """Register file intelligence tools into the global tool registry."""
    from .registry import registry

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

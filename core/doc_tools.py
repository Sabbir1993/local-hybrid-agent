"""core/doc_tools.py - doc_inspect / doc_edit / doc_create tools.

Chat mode   : files live in the caller's own common space (COMMON_ROOT/user_<id>).
Agent mode  : files live in the project folder on the user's device; bytes go
              through the companion (fs.read_b64 / fs.write_b64) and are edited
              in memory here -- never written to the server's disk. Attachments
              uploaded in agent mode sit in the common space and are found there.

Every edit writes a new version and records it in doc_store, so the previous
file is always still there.
"""

import base64
from pathlib import Path
from typing import Optional

from . import companion_bridge, doc_ops, doc_store
from .agent_tools import (_common_resolve, _ws_resolve, common_workspace,
                          get_current_user_id, require_device_workspace)
from .doc_ops.base import DocOpError, sha256

DEVICE_MAX_BYTES = 10 * 1024 * 1024   # companion frames (uvicorn ws_max_size 16 MB, base64)


def _uid() -> int:
    uid = get_current_user_id()
    if uid is None:
        raise PermissionError("not signed in")
    return uid


def _file_arg(args: dict) -> str:
    f = args.get("file") or args.get("path") or args.get("filename") or args.get("name")
    if not f:
        raise ValueError("file required")
    return str(f).strip()


# ------------------------------------------------------------------
# Locating files
# ------------------------------------------------------------------

def resolve_common(name: str) -> Optional[Path]:
    """The caller's common-space file: exact name, else the newest version of it."""
    try:
        p = _common_resolve(name)
    except PermissionError:
        return None
    if p.is_file():
        return p
    folder = common_workspace()
    stem, suf = doc_store.base_stem(Path(name).name), Path(name).suffix.lower()
    cands = [f for f in folder.glob(f"{stem}*{suf}") if f.is_file() and doc_store.base_stem(f.name) == stem]
    return max(cands, key=lambda f: f.stat().st_mtime) if cands else None


class _Loc:
    """Where a document lives and how to write its next version."""

    def __init__(self, location: str, path: Path, uid: int):
        self.location, self.path, self.uid = location, path, uid

    @property
    def key(self) -> str:
        return self.path.name if self.location == "common" else str(self.path)

    def sibling(self, name: str) -> Path:
        if self.location == "common":
            return _common_resolve(name)
        return self.path.with_name(name)


async def _locate(name: str) -> tuple[_Loc, bytes]:
    uid = _uid()
    # 1. the project folder on the user's device (agent mode)
    try:
        dev_uid, _ws = require_device_workspace()
        p = _ws_resolve(name)
        data = await companion_bridge.call(dev_uid, "fs.read_b64", {"path": str(p)})
        if data.get("data") is not None:
            return _Loc("device", p, dev_uid), base64.b64decode(data["data"])
    except PermissionError:   # includes WorkspaceAccessDenied: no project on this device
        pass
    except RuntimeError as e:
        if "unknown op" in str(e):
            raise DocOpError("the A770 Companion app on your machine is too old for document edits - "
                             "update it and try again")
        raise DocOpError(f"could not read the file on your device: {e}")
    # 2. the user's own common space (chat files, uploaded attachments)
    p = resolve_common(name)
    if p is None:
        raise FileNotFoundError(f"document not found: {name}")
    return _Loc("common", p, uid), p.read_bytes()


def _locate_common(name: str) -> tuple[_Loc, bytes]:
    p = resolve_common(name)
    if p is None:
        raise FileNotFoundError(f"document not found: {name}")
    return _Loc("common", p, _uid()), p.read_bytes()


async def _write(loc: _Loc, target: Path, data: bytes) -> None:
    if loc.location == "common":
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
        return
    if len(data) > DEVICE_MAX_BYTES:
        raise DocOpError(f"document too large for the companion ({len(data)} bytes)")
    await companion_bridge.call(loc.uid, "fs.write_b64", {"path": str(target),
                                                          "data": base64.b64encode(data).decode("ascii")})


def _spec_for(loc: _Loc) -> tuple[Optional[dict], Optional[str]]:
    row = doc_store.find(loc.uid, loc.key, loc.location)
    return row, (row or {}).get("source_spec")


# ------------------------------------------------------------------
# Core operations (shared by chat and agent entry points)
# ------------------------------------------------------------------

def _inspect(loc: _Loc, data: bytes, args: dict) -> str:
    _row, spec = _spec_for(loc)
    text = doc_ops.inspect(data, loc.path.name, focus=args.get("focus"), source_spec=spec)
    where = "your device" if loc.location == "device" else "your files"
    return f"File: {loc.key} ({where})\n{text}"


async def _load_images(loc: _Loc, ops: list) -> None:
    """replace_image ops name an image file; load its bytes from the same place."""
    for op in ops:
        if str(op.get("op", "")).lower() == "replace_image" and op.get("image"):
            if loc.location == "device":
                d = await companion_bridge.call(loc.uid, "fs.read_b64", {"path": str(loc.sibling(Path(op["image"]).name))})
                op["_image_bytes"] = base64.b64decode(d["data"]) if d.get("data") else None
            else:
                p = resolve_common(op["image"])
                op["_image_bytes"] = p.read_bytes() if p else None


async def _edit(loc: _Loc, data: bytes, args: dict) -> str:
    ops = doc_ops.normalize_ops(args.get("ops"))
    await _load_images(loc, ops)
    row, spec = _spec_for(loc)
    out = doc_ops.edit(data, loc.path.name, ops, source_spec=spec)
    parent_id = row["id"] if row else doc_store.register(
        loc.uid, loc.key, loc.path.suffix.lower().lstrip("."), location=loc.location, sha256=sha256(data))
    version = (row["version"] if row else 1) + 1
    in_place = bool(args.get("in_place")) and loc.location == "device"
    target = loc.path if in_place else loc.sibling(doc_store.version_name(loc.path.name, version))
    await _write(loc, target, out.data)
    new_key = target.name if loc.location == "common" else str(target)
    doc_store.register(loc.uid, new_key, loc.path.suffix.lower().lstrip("."), location=loc.location,
                       parent_id=parent_id, source_spec=out.source_spec, sha256=sha256(out.data))
    doc_store.audit(loc.uid, "doc.edit", new_key, {"parent": loc.key, "ops": [o.get("op") for o in ops],
                                                   "sha256": sha256(out.data)})
    lines = [f"Edited {loc.path.name} -> saved as {target.name} (version {version}; the previous version is kept)."]
    lines += [f"- {c}" for c in out.changes]
    lines += [f"note: {n}" for n in out.notes]
    lines.append("Everything else in the document is unchanged (verified).")
    if loc.location == "common":
        lines.append(f"[DOWNLOAD: {target.name}]")
    return "\n".join(lines)


async def _create(args: dict, location: str) -> str:
    name = _file_arg(args)
    content = str(args.get("content") or args.get("spec") or "")
    template = None
    if args.get("template"):
        tloc, template = (await _locate(args["template"])) if location == "device" else _locate_common(args["template"])
    uid = _uid()
    data, spec = doc_ops.create(name, content, template=template)
    if location == "device":
        dev_uid, _ws = require_device_workspace()
        target = _ws_resolve(name)
        loc = _Loc("device", target, dev_uid)
        key = str(target)
    else:
        stem, suf = doc_store.base_stem(Path(name).name), Path(name).suffix.lower()
        target = _common_resolve(doc_store.version_name(f"{stem}{suf}", 1).replace("-v1-", "-"))
        loc = _Loc("common", target, uid)
        key = target.name
    await _write(loc, target, data)
    doc_store.register(uid, key, target.suffix.lower().lstrip("."), location=location, source_spec=spec,
                       sha256=sha256(data))
    msg = f"Created {target.name}. Later changes can be made with doc_edit without regenerating it."
    return msg + (f" [DOWNLOAD: {target.name}]" if location == "common" else f" (saved on your device: {target})")


def _err(e: Exception) -> str:
    if isinstance(e, (DocOpError, FileNotFoundError, PermissionError, ValueError)):
        return f"doc error: {e}"
    raise e


# ------------------------------------------------------------------
# Agent-mode tools (async; device first, then common space)
# ------------------------------------------------------------------

async def tool_doc_inspect(args: dict) -> str:
    try:
        loc, data = await _locate(_file_arg(args))
        return _inspect(loc, data, args)
    except Exception as e:
        return _err(e)


async def tool_doc_edit(args: dict) -> str:
    try:
        loc, data = await _locate(_file_arg(args))
        return await _edit(loc, data, args)
    except Exception as e:
        return _err(e)


async def tool_doc_create(args: dict) -> str:
    try:
        return await _create(args, "device")
    except Exception as e:
        return _err(e)


# ------------------------------------------------------------------
# Chat-mode tools (sync; the caller's common space only)
# ------------------------------------------------------------------

def _run(coro):
    import asyncio
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(coro)
    # called from inside the event loop's thread: common-space writes are plain file IO,
    # so drive the coroutine to completion synchronously
    try:
        coro.send(None)
    except StopIteration as stop:
        return stop.value
    raise RuntimeError("common-space doc tool unexpectedly awaited")


def tool_doc_inspect_common(args: dict) -> str:
    try:
        loc, data = _locate_common(_file_arg(args))
        return _inspect(loc, data, args)
    except Exception as e:
        return _err(e)


def tool_doc_edit_common(args: dict) -> str:
    try:
        loc, data = _locate_common(_file_arg(args))
        return _run(_edit(loc, data, args))
    except Exception as e:
        return _err(e)


def tool_doc_create_common(args: dict) -> str:
    try:
        return _run(_create(args, "common"))
    except Exception as e:
        return _err(e)


# ------------------------------------------------------------------
# Schemas
# ------------------------------------------------------------------

_OPS_HELP = (
    "List of edit ops applied in place; everything not targeted stays byte-identical. "
    "Addresses come from doc_inspect. "
    "PPTX: set_text{addr:'s2/sh3'|'s2/title'|'s2/sh3/p1'|'s2/sh5/tbl[1,2]',text}, replace_text{find,replace,scope?:'s2'}, "
    "add_slide{after:2,title,bullets:[..],layout?,like?:'s3'}, duplicate_slide{addr:'s2'}, delete_slide{addr:'s4'}, "
    "move_slide{addr:'s4',to:2}, set_notes{addr:'s2',text}, replace_image{addr:'s2/sh4',image:'logo.png'}. "
    "XLSX: set_cell{addr:'Sheet1!B7',value}, set_formula{addr,formula:'=SUM(B2:B6)'}, "
    "set_range{addr:'Sheet1!A2',values:[[..],[..]],formulas?:true}, clear{addr:'Sheet1!B2:C4'}, "
    "append_rows{sheet,rows:[[..]]}, insert_rows{sheet,at,count,values?}, delete_rows{sheet,at,count}, "
    "add_sheet{name,rows?}, rename_sheet{sheet,to}. "
    "CSV: set_cell{addr:'r3cAmount'|'r3c2'|'B3',value}, set_row{row,values}, append_rows{rows}, "
    "insert_rows{at,rows}, delete_rows{at,count}, add_column{name,values}, replace_text{find,replace}. "
    "DOCX: set_text{addr:'p4'|'t1[2,3]'|'hdr1/p1',text}, insert_paragraph_after{after:'p4',text|texts,style?}, "
    "delete_paragraph{addr}, add_table_row{addr:'t1',values,after?}, replace_text{find,replace}. "
    "PDF (generated here) / MD: replace_section{addr:'sec2',text}, set_heading{addr,text}, "
    "insert_section_after{after:'sec2',text}, delete_section{addr}, replace_text{find,replace}."
)

DOC_INSPECT_SCHEMA = {"type": "function", "function": {
    "name": "doc_inspect",
    "description": "Read a PowerPoint/Excel/Word/CSV/PDF document as a structured outline with stable "
                   "addresses (slide shapes, cells, paragraphs, sections). Call before doc_edit.",
    "parameters": {"type": "object", "properties": {
        "file": {"type": "string", "description": "file name, e.g. deck.pptx"},
        "focus": {"type": "string", "description": "optional address to zoom into, e.g. s4 or Sheet1!row20"},
    }, "required": ["file"]}}}

DOC_EDIT_SCHEMA = {"type": "function", "function": {
    "name": "doc_edit",
    "description": "Change only the requested parts of an existing PowerPoint/Excel/Word/CSV/PDF document, "
                   "keeping all other content, layout, styles and formatting exactly as they are. Use this "
                   "instead of regenerating a document. Saves a new version; the old one is kept.",
    "parameters": {"type": "object", "properties": {
        "file": {"type": "string"},
        "ops": {"type": "array", "items": {"type": "object"}, "description": _OPS_HELP},
        "in_place": {"type": "boolean", "description": "agent mode only: overwrite the file instead of saving -vN"},
    }, "required": ["file", "ops"]}}}

DOC_CREATE_SCHEMA = {"type": "function", "function": {
    "name": "doc_create",
    "description": "Create a new .pptx/.docx/.xlsx/.csv/.pdf from markdown (slides split by '---' or '# '; "
                   "tables as markdown tables or CSV). Built so later doc_edit calls can change parts of it.",
    "parameters": {"type": "object", "properties": {
        "file": {"type": "string", "description": "file name with extension"},
        "content": {"type": "string", "description": "markdown / table text"},
        "template": {"type": "string", "description": "optional .pptx whose theme and layouts to use"},
    }, "required": ["file", "content"]}}}

DOC_SCHEMAS = [DOC_INSPECT_SCHEMA, DOC_EDIT_SCHEMA, DOC_CREATE_SCHEMA]
AGENT_IMPLS = {"doc_inspect": tool_doc_inspect, "doc_edit": tool_doc_edit, "doc_create": tool_doc_create}
CHAT_IMPLS = {"doc_inspect": tool_doc_inspect_common, "doc_edit": tool_doc_edit_common,
              "doc_create": tool_doc_create_common}

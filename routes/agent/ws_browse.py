from pathlib import Path
from typing import Optional
from fastapi import Depends
from fastapi.responses import JSONResponse, Response
from core.auth import Principal
from core.deps import get_current_user
from core.file_tools import VIDEO_MIME
from core.agent_tools import (
    active_workspace,
    get_active_project,
    require_device_workspace,
    WorkspaceAccessDenied,
    _ws_resolve,
    _ws_changes,
    _remote_uid,
)
from core import companion_bridge

from .base import router


def _personal_folder(agent: Optional[int], user: Principal):
    """Preview of a Personal Agent's file: read the agent's own work folder, not the selected project.
    Only the agent's owner, only a folder set on that agent. Returns (folder or None, error response or None)."""
    if not agent:
        return None, None
    from core.auth_db import db_get_custom_agent
    from core.request_context import set_personal_workspace
    a = db_get_custom_agent(agent, user.id)
    folder = (a or {}).get("work_dir") if (a or {}).get("user_id") == user.id else ""
    if not folder:
        return None, JSONResponse({"error": "This agent has no work folder"}, status_code=400)
    set_personal_workspace(folder)
    return folder, None


async def _ws_tree_scan(rel_dir: str) -> list:
    """One level of the workspace tree from the agent tools module."""
    ignored = {".git", "__pycache__", "node_modules", ".venv", "venv", "_agent_run.py"}
    # the workspace lives on the user's own machine -- always ask the companion
    uid, ws = require_device_workspace()
    data = await companion_bridge.call(uid, "fs.tree", {"root": str(ws), "rel": rel_dir or ""})
    changed_keys = [k.replace("\\", "/") for k in (_ws_changes.get(uid) or {})]
    out = []
    for n in (data.get("nodes") or []):
        if n.get("name") in ignored or str(n.get("name") or "").startswith("_agent_run_"):
            continue
        rel = n.get("path", "")
        if n.get("dir"):
            out.append({"name": n["name"], "path": rel, "dir": True, "children": None})
        else:
            changed = any(k.endswith("/" + rel) or k == rel for k in changed_keys)
            out.append({"name": n["name"], "path": rel, "dir": False,
                        "size": n.get("size", 0), "changed": changed})
    return out


def _ws_diff_lines(before: Optional[str], after: str) -> list:
    """Minimal line diff (unified-like) using difflib; returns per-line dicts."""
    import difflib
    b = (before or "").splitlines()
    a = (after or "").splitlines()
    ops = []
    for tag, i1, i2, j1, j2 in difflib.SequenceMatcher(None, b, a).get_opcodes():
        if tag == "equal":
            for ln in b[i1:i2]:
                ops.append({"t": " ", "s": ln})
        elif tag == "delete":
            for ln in b[i1:i2]:
                ops.append({"t": "-", "s": ln})
        elif tag == "insert":
            for ln in a[j1:j2]:
                ops.append({"t": "+", "s": ln})
        elif tag == "replace":
            for ln in b[i1:i2]:
                ops.append({"t": "-", "s": ln})
            for ln in a[j1:j2]:
                ops.append({"t": "+", "s": ln})
    return ops


@router.get("/agent/ws/tree")
async def agent_ws_tree(path: str = "", user: Principal = Depends(get_current_user), agent: Optional[int] = None):
    """One level of the files the Code tab shows. With `agent` it is THAT agent's own work folder; without it the active
    project. One never stands in for the other: an agent with no folder is an error, not the project's files."""
    folder, bad = _personal_folder(agent, user)
    if bad:
        return bad
    curr_proj = get_active_project(user.id)
    if not folder and (not curr_proj or curr_proj in ("scratch", "default")):
        return JSONResponse({"error": "No project selected", "root": "", "project": None, "nodes": [], "changes": []})

    try:
        ws = active_workspace()
    except WorkspaceAccessDenied as e:
        return JSONResponse({"error": str(e), "root": "", "nodes": [], "changes": []}, status_code=409)
    changes = []
    for k, rec in (_ws_changes.get(user.id) or {}).items():
        try:
            rel = str(Path(k).relative_to(ws)).replace("\\", "/")
        except ValueError:
            continue          # a file of another workspace is not a change of this one
        changes.append({
            "path": rel,
            "status": "created" if rec.get("before") is None else "modified",
        })
    changes.sort(key=lambda x: x["path"])
    try:
        nodes = await _ws_tree_scan(path)
    except (ConnectionError, TimeoutError, RuntimeError) as e:
        return JSONResponse({"error": f"SSL Local Agent: {e}"}, status_code=502)
    return {"root": ws.name, "project": None if folder else curr_proj, "agent": agent if folder else None,
            "nodes": nodes, "changes": changes}


@router.get("/agent/ws/file")
async def agent_ws_file(path: str, user: Principal = Depends(get_current_user), agent: Optional[int] = None):
    folder, bad = _personal_folder(agent, user)
    if bad:
        return bad
    curr_proj = get_active_project(user.id)
    if not folder and (not curr_proj or curr_proj in ("scratch", "default")):
        return JSONResponse({"error": "No project selected"}, status_code=400)

    # read from the user's machine via the companion (never the server's disk)
    uid = _remote_uid()
    try:
        p = _ws_resolve(path)
    except WorkspaceAccessDenied:
        raise
    except PermissionError as e:
        return JSONResponse({"error": str(e)}, status_code=403)
    try:
        data = await companion_bridge.call(uid, "fs.read", {"path": str(p)})
    except (ConnectionError, TimeoutError, RuntimeError) as e:
        return JSONResponse({"error": f"SSL Local Agent: {e}"}, status_code=502)
    content = data.get("content")
    if content is None:
        return JSONResponse({"error": f"file not found: {path}"}, status_code=404)
    rec = (_ws_changes.get(uid) or {}).get(str(p), {})
    before = rec.get("before")
    changed = bool(rec) and rec.get("after") is not None
    resp = {
        "path": path, "size": len(content.encode("utf-8", errors="replace")),
        "content": content, "changed": changed,
        "status": ("created" if before is None else "modified") if changed else "unchanged",
    }
    if changed and before is not None:
        resp["diff"] = _ws_diff_lines(before, content)
    elif changed and before is None:
        resp["diff"] = [{"t": "+", "s": ln} for ln in content.splitlines()]
    return resp


# Text-ish extensions the preview can render directly; anything else goes to the
# client as bytes so the modal can build an ArrayBuffer/object URL for it.
_WS_RAW_TEXT_EXT = {
    ".txt", ".md", ".markdown", ".py", ".js", ".mjs", ".cjs", ".ts", ".tsx", ".jsx", ".json",
    ".yaml", ".yml", ".toml", ".ini", ".cfg", ".conf", ".env", ".html", ".htm", ".css", ".scss",
    ".less", ".sql", ".sh", ".bash", ".ps1", ".bat", ".xml", ".csv", ".tsv", ".log", ".lua",
    ".rb", ".go", ".rs", ".java", ".kt", ".swift", ".c", ".h", ".cpp", ".hpp", ".cs", ".php",
    ".pl", ".r", ".dart", ".vue", ".svelte", ".gradle", ".dockerfile", ".gitignore", ".lock",
    ".diff", ".patch", ".svg",
}


_WS_RAW_MAX = 12 * 1024 * 1024


async def _ws_load(path: str, agent: Optional[int], user: Principal):
    """One workspace file, read through the companion with the workspace guards: (media type, bytes, truncated) or an error
    response. The path can never escape the active workspace and the bytes never come from this server's disk. Shared by
    /agent/ws/raw and the slide / page-picture endpoints below."""
    folder, bad = _personal_folder(agent, user)
    if bad:
        return bad
    curr_proj = get_active_project(user.id)
    if not folder and (not curr_proj or curr_proj in ("scratch", "default")):
        return JSONResponse({"error": "No project selected"}, status_code=400)
    uid = _remote_uid()
    try:
        p = _ws_resolve(path)
    except WorkspaceAccessDenied:
        raise
    except PermissionError as e:
        return JSONResponse({"error": str(e)}, status_code=403)

    from core.file_tools import MIME_MAP as _MM
    ext = p.suffix.lower()
    if ext in _MM:
        media = _MM[ext]
    elif ext in {".html", ".htm"}:
        media = "text/html; charset=utf-8"
    elif ext in _WS_RAW_TEXT_EXT:
        media = "text/plain; charset=utf-8"
    elif ext == ".svg":
        media = "image/svg+xml"
    elif ext in {".png", ".jpg", ".jpeg", ".webp", ".gif", ".ico"}:
        media = f"image/{ext.lstrip('.')}"
    elif ext in VIDEO_MIME:
        media = VIDEO_MIME[ext]
    else:
        media = "application/octet-stream"
    is_text = ("." + ext.lstrip(".")) in _WS_RAW_TEXT_EXT
    try:
        if is_text:
            data = await companion_bridge.call(uid, "fs.read", {"path": str(p)})
            content = data.get("content")
            if content is None:
                return JSONResponse({"error": f"file not found: {path}"}, status_code=404)
            body = content.encode("utf-8", errors="replace")
        else:
            data = await companion_bridge.call(uid, "fs.read_b64", {"path": str(p)})
            b64 = data.get("data")
            if not b64:
                return JSONResponse({"error": f"file not found: {path}"}, status_code=404)
            import base64 as _b64
            body = _b64.b64decode(b64)
    except (ConnectionError, TimeoutError, RuntimeError) as e:
        return JSONResponse({"error": f"SSL Local Agent: {e}"}, status_code=502)
    except ValueError as e:
        return JSONResponse({"error": f"could not decode {path}: {e}"}, status_code=422)
    truncated = len(body) > _WS_RAW_MAX
    if truncated:
        body = body[:_WS_RAW_MAX]
    return media, body, truncated


@router.get("/agent/ws/raw")
async def agent_ws_raw(path: str, user: Principal = Depends(get_current_user), agent: Optional[int] = None):
    """Stream one workspace file verbatim, for the preview modal.

    /agent/raw only serves the server-side common space, so a project file
    (src/shop.js and friends) previewed from a tool card 404'd. This reads the
    user's own machine through the companion, with the same workspace guards
    agent_ws_file uses: the path can never escape the active workspace, and the
    bytes never come from this server's disk.
    """
    loaded = await _ws_load(path, agent, user)
    if isinstance(loaded, Response):
        return loaded
    media, body, _truncated = loaded
    # Never let the browser sniff or execute a workspace file in this origin.
    headers = {
        "Content-Disposition": "inline",
        "X-Content-Type-Options": "nosniff",
        "Cache-Control": "no-store",
    }
    # A sandboxed document cannot open the browser's PDF viewer (the preview showed a blank frame), and a PDF does not run
    # script in this origin: /agent/raw serves the server's own PDFs the same way. Every other type stays sandboxed.
    if media.split(";")[0].strip().lower() != "application/pdf":
        headers["Content-Security-Policy"] = "sandbox"
    return Response(content=body, media_type=media, headers=headers)


# ---- slide and page pictures of a deck / workbook / document that lives on the user's own machine ------------------------
# /agent/slides and /agent/doc-page read the server's own common space only; a file the agent wrote into its work folder is
# on the user's machine, so these twins read the bytes through the companion and use the same renderers.
def _too_big(truncated: bool):
    return JSONResponse({"error": "file too large to preview here - open it on your computer"}, status_code=413) if truncated else None


@router.get("/agent/ws/slides")
async def agent_ws_slides(path: str, user: Principal = Depends(get_current_user), agent: Optional[int] = None):
    """Render model of a .pptx in the workspace (positions, text, images) for the preview modal."""
    if not path.lower().endswith(".pptx"):
        return JSONResponse({"error": "only .pptx files can be previewed as slides"}, status_code=400)
    loaded = await _ws_load(path, agent, user)
    if isinstance(loaded, Response):
        return loaded
    _media, body, truncated = loaded
    big = _too_big(truncated)
    if big:
        return big
    import asyncio
    from core.doc_ops import pptx_ops
    from core.doc_ops.base import DocOpError
    try:
        model = await asyncio.get_running_loop().run_in_executor(None, pptx_ops.preview, body)
    except DocOpError as e:
        return JSONResponse({"error": str(e)}, status_code=422)
    except Exception as e:
        return JSONResponse({"error": f"could not read presentation: {type(e).__name__}"}, status_code=422)
    return JSONResponse(model, headers={"Cache-Control": "no-cache"})


async def _ws_pages(path: str, agent: Optional[int], user: Principal):
    """(pdf bytes, page count) of a workspace .pptx / .xlsx / .docx drawn by LibreOffice, or (None, 0)."""
    from core.doc_tools import render
    from .preview import _pages_of_bytes
    ext = "." + path.rsplit(".", 1)[-1].lower() if "." in path else ""
    if ext not in render.RENDERABLE:
        return None, 0
    loaded = await _ws_load(path, agent, user)
    if isinstance(loaded, Response):
        return None, 0
    _media, body, truncated = loaded
    if truncated:
        return None, 0
    return await _pages_of_bytes(body, ext)


@router.get("/agent/ws/doc-pages")
async def agent_ws_doc_pages(path: str, user: Principal = Depends(get_current_user), agent: Optional[int] = None):
    """{"pages": N}: how many page pictures /agent/ws/doc-page can serve (0 when the server has no LibreOffice or the file is
    not on this user's machine, so the client keeps its own preview)."""
    from .preview import _MAX_PAGES_SHOWN
    try:
        _pdf, count = await _ws_pages(path, agent, user)
    except WorkspaceAccessDenied:
        raise
    except Exception:
        count = 0
    return JSONResponse({"pages": min(int(count or 0), _MAX_PAGES_SHOWN)}, headers={"Cache-Control": "no-store"})


@router.get("/agent/ws/doc-page")
async def agent_ws_doc_page(path: str, n: int = 1, user: Principal = Depends(get_current_user), agent: Optional[int] = None):
    """One page of the rendered workspace file as a JPEG (n is 1-based)."""
    from .preview import page_jpeg
    try:
        pdf, count = await _ws_pages(path, agent, user)
    except WorkspaceAccessDenied:
        raise
    except Exception:
        pdf, count = None, 0
    return await page_jpeg(pdf, count, n)

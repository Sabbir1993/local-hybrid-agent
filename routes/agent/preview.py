import asyncio
import time
from typing import Optional
from fastapi import Depends
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
from core.auth import Principal
from core.deps import get_current_user

from .base import router
from .download import _resolve_requested_file


# ---- HTML preview of generated markup (code blocks, unsaved files) ----
# The app's CSP forbids inline script, and an iframe srcdoc inherits it, so previews
# are served from here instead with the same sandbox CSP as /agent/raw: scripts run,
# but in an opaque origin without this app's cookies or API.
_PREVIEW_TTL_S = 15 * 60


_PREVIEW_MAX_BYTES = 2 * 1024 * 1024


_PREVIEW_MAX_PER_USER = 20


_previews: dict = {}          # id -> (user_id, created, html)


class PreviewHtmlReq(BaseModel):
    html: str = Field(max_length=_PREVIEW_MAX_BYTES)


def _prune_previews(now: float) -> None:
    """Drop expired previews. Called from both the write and the read path: cleanup used to
    run only on POST, so a user's HTML sat in a module global until that same user previewed
    something else. Capped per user, but never collected for anyone who stops previewing."""
    for k in [k for k, (_uid, ts, _h) in _previews.items() if now - ts > _PREVIEW_TTL_S]:
        _previews.pop(k, None)


@router.post("/agent/preview-html")
async def put_preview_html(req: PreviewHtmlReq, user: Principal = Depends(get_current_user)):
    import secrets
    now = time.time()
    _prune_previews(now)
    mine = sorted((ts, k) for k, (uid, ts, _h) in _previews.items() if uid == user.id)
    for _ts, k in mine[:max(0, len(mine) - _PREVIEW_MAX_PER_USER + 1)]:
        _previews.pop(k, None)
    pid = secrets.token_urlsafe(18)
    _previews[pid] = (user.id, now, req.html)
    return {"url": f"/agent/preview-html/{pid}"}


@router.get("/agent/preview-html/{pid}")
async def get_preview_html(pid: str, user: Principal = Depends(get_current_user)):
    from fastapi.responses import HTMLResponse
    _prune_previews(time.time())
    hit = _previews.get(pid)
    if not hit or hit[0] != user.id or time.time() - hit[1] > _PREVIEW_TTL_S:
        return JSONResponse({"error": "preview expired - reopen it"}, status_code=404)
    return HTMLResponse(hit[2], headers={
        "Content-Security-Policy": "sandbox allow-scripts allow-forms allow-popups allow-modals",
        "Cache-Control": "no-store", "X-Content-Type-Options": "nosniff"})


@router.get("/agent/slides")
async def agent_slides(path: str, space: Optional[str] = None):
    """Render model of a .pptx for the preview modal (positions, text, images)."""
    p = _resolve_requested_file(path, space)
    if p is None or not p.is_file():
        return JSONResponse({"error": f"file not found: {path}"}, status_code=404)
    if p.suffix.lower() != ".pptx":
        return JSONResponse({"error": "only .pptx files can be previewed as slides"}, status_code=400)
    from core.doc_ops import pptx_ops
    from core.doc_ops.base import DocOpError
    try:
        model = await asyncio.get_running_loop().run_in_executor(None, pptx_ops.preview, p.read_bytes())
    except DocOpError as e:
        return JSONResponse({"error": str(e)}, status_code=422)
    except Exception as e:
        return JSONResponse({"error": f"could not read presentation: {type(e).__name__}"}, status_code=422)
    return JSONResponse(model, headers={"Cache-Control": "no-cache"})


# ---- page pictures of a deck / workbook / document, as LibreOffice draws it (core/doc_tools/render.py) ----
# The real file, not an approximation: what the user will see in PowerPoint. Rendered once per file version and kept
# for a short while; the picture is served only to a signed-in user who can already open the file.
_PAGES_TTL_S = 10 * 60
_PAGES_MAX = 6
_MAX_PAGES_SHOWN = 60
_page_pdfs: dict = {}         # sha256 of the file -> (created, pdf bytes, page count)


def _rendered_pdf(data: bytes):
    import hashlib
    key = hashlib.sha256(data).hexdigest()
    now = time.time()
    for k in [k for k, v in _page_pdfs.items() if now - v[0] > _PAGES_TTL_S]:
        _page_pdfs.pop(k, None)
    hit = _page_pdfs.get(key)
    if hit:
        return hit[1], hit[2]
    return None, key


def _render_now(data: bytes, ext: str, key: str):
    from core.doc_tools import render
    from core.pdf_tools.common import open_reader
    pdf = render.to_pdf(data, ext)
    if not pdf:
        return None, 0
    count = len(open_reader(pdf, None).pages)
    while len(_page_pdfs) >= _PAGES_MAX:
        _page_pdfs.pop(min(_page_pdfs, key=lambda k: _page_pdfs[k][0]), None)
    _page_pdfs[key] = (time.time(), pdf, count)
    return pdf, count


async def _pages_of(path: str, space: Optional[str]):
    from core.doc_tools import render
    p = _resolve_requested_file(path, space)
    if p is None or not p.is_file() or p.suffix.lower() not in render.RENDERABLE:
        return None, 0
    data = p.read_bytes()
    pdf, key_or_count = _rendered_pdf(data)
    if pdf is not None:
        return pdf, key_or_count
    return await asyncio.get_running_loop().run_in_executor(None, _render_now, data, p.suffix.lower(), key_or_count)


@router.get("/agent/doc-pages")
async def doc_pages(path: str, space: Optional[str] = None, user: Principal = Depends(get_current_user)):
    """{"pages": N}: how many page pictures /agent/doc-page can serve for this .pptx / .xlsx / .docx (0 when the server
    has no LibreOffice, so the client keeps its own preview)."""
    try:
        _pdf, count = await _pages_of(path, space)
    except Exception:
        count = 0
    return JSONResponse({"pages": min(int(count or 0), _MAX_PAGES_SHOWN)}, headers={"Cache-Control": "no-store"})


@router.get("/agent/doc-page")
async def doc_page(path: str, n: int = 1, space: Optional[str] = None, user: Principal = Depends(get_current_user)):
    """One page of the rendered file as a JPEG (n is 1-based)."""
    from fastapi.responses import Response
    try:
        pdf, count = await _pages_of(path, space)
        if not pdf or not (1 <= int(n) <= min(count, _MAX_PAGES_SHOWN)):
            return JSONResponse({"error": "page not available"}, status_code=404)

        def draw():
            from core.pdf_tools.common import render_pages
            from core.pdf_tools.see import _jpeg
            return _jpeg(render_pages(pdf, [int(n)], 1.5)[0][1])
        img = await asyncio.get_running_loop().run_in_executor(None, draw)
    except Exception as e:
        return JSONResponse({"error": f"could not draw the page ({type(e).__name__})"}, status_code=422)
    return Response(img, media_type="image/jpeg", headers={"Cache-Control": "private, max-age=300",
                                                           "X-Content-Type-Options": "nosniff"})


@router.get("/agent/doc-draft")
async def doc_draft(path: str, user: Principal = Depends(get_current_user)):
    """HTML draft of a PDF that is still being built in pieces (doc_create + doc_append). The real PDF is printed once, at
    the end; until then the right-hand pane shows this page, rendered from the stored markdown with the PDF's own styles."""
    from fastapi.responses import HTMLResponse
    from core import doc_store
    from core.agent_tools.dom_convert import _markdown_to_html_dom
    p = _resolve_requested_file(path, "common")
    row = doc_store.find(user.id, p.name, "common") if p is not None and p.is_file() else None
    if not row or not row.get("source_spec"):
        return JSONResponse({"error": "no draft for this file"}, status_code=404)
    title = p.stem.replace("_", " ").replace("-", " ").title()
    return HTMLResponse(_markdown_to_html_dom(row["source_spec"], title), headers={
        "Content-Security-Policy": "sandbox", "Cache-Control": "no-store", "X-Content-Type-Options": "nosniff"})

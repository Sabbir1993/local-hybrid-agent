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

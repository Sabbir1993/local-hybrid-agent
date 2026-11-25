"""
routes/pages.py - The React app (served at / and its client-side paths), its build assets, static files, and the settings
page.

The UI is the React app in frontend/ (built to frontend/dist). It shows its own login screen when there is no session, and
every data endpoint checks authentication itself. Its settings modal shows settings.html in an iframe
(frontend/src/components/modals/SettingsModal.tsx), so that page is still served at /settings.
"""

from fastapi import APIRouter, Request
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse
from core.config import BASE_DIR, STATIC_DIR
from core.deps import get_current_user

router = APIRouter()

SETTINGS_FILE = BASE_DIR / "settings.html"
DIST_DIR = BASE_DIR / "frontend" / "dist"
DIST_INDEX = DIST_DIR / "index.html"
DIST_ASSETS_DIR = DIST_DIR / "assets"
REACT_MISSING = {"error": "This port serves the API only. The web UI runs as a separate frontend app; open that app's URL instead."}


# --- React build assets ---
@router.get("/assets/{file_path:path}")
async def serve_react_assets(file_path: str):
    p = (DIST_ASSETS_DIR / file_path).resolve()
    if p.exists() and p.is_file() and p.is_relative_to(DIST_ASSETS_DIR.resolve()):
        media_types = {
            ".css": "text/css",
            ".js": "application/javascript",
            ".map": "application/json",
            ".svg": "image/svg+xml",
            ".png": "image/png",
            ".jpg": "image/jpeg",
        }
        media = media_types.get(p.suffix.lower(), "application/octet-stream")
        return FileResponse(p, media_type=media, headers={"Cache-Control": "public, max-age=31536000"})
    return JSONResponse({"error": "asset not found"}, status_code=404)


def _react_index():
    if DIST_INDEX.exists():
        return FileResponse(DIST_INDEX, media_type="text/html", headers={"Cache-Control": "no-store, max-age=0"})
    return JSONResponse(REACT_MISSING, status_code=404)


# The React app handles these paths itself: login, report, audit and the database explorer are screens inside it.
@router.get("/")
@router.get("/react")
@router.get("/login")
@router.get("/report")
@router.get("/audit")
@router.get("/db")
@router.get("/share")
@router.get("/share/{key}")
async def react_app(key: str = ""):
    return _react_index()


@router.get("/media/shared-session/{key}")
@router.get("/public/share/{key}")
@router.get("/api/share/{key}")
async def get_public_share(key: str):
    """Public read-only session data by secure share_key. Unauthenticated by design."""
    from core.db import db_get_shared_session
    data = db_get_shared_session(key)
    if not data:
        return JSONResponse({"error": "Shared session not found or link has expired"}, status_code=404)
    return {
        "ok": True,
        "title": data["title"],
        "created_at": data["created_at"],
        "shared_at": data["shared_at"],
        "messages": data["messages"],
    }


@router.get("/media/shared-session/{key}/raw")
async def shared_session_raw(key: str, path: str):
    """Serve a file inline for previews in shared sessions (unauthenticated, secured by share_key)."""
    from core.db import db_get_shared_session
    from routes.agent.download import _resolve_requested_file, MIME_MAP, VIDEO_MIME
    data = db_get_shared_session(key)
    if not data:
        return JSONResponse({"error": "Shared session not found or link has expired"}, status_code=404)

    p = _resolve_requested_file(path, recover=False, user_id=data.get("user_id"))
    if p is None or not p.is_file():
        return JSONResponse({"error": f"file not found: {path}"}, status_code=404)

    suffix = p.suffix.lower()
    mime = MIME_MAP.get(suffix)
    if not mime:
        if suffix in {".html", ".htm"}:
            mime = "text/html; charset=utf-8"
        elif suffix in {".py", ".js", ".ts", ".jsx", ".tsx", ".css", ".md", ".txt", ".json", ".log", ".yaml", ".yml", ".toml", ".ini", ".sh", ".bat", ".ps1", ".sql", ".csv"}:
            mime = "text/plain; charset=utf-8"
        elif suffix == ".svg":
            mime = "image/svg+xml"
        elif suffix in {".png", ".jpg", ".jpeg", ".webp", ".gif"}:
            mime = f"image/{suffix.lstrip('.')}"
        elif suffix in VIDEO_MIME:
            mime = VIDEO_MIME[suffix]
        else:
            mime = "application/octet-stream"

    headers = {
        "Content-Disposition": f'inline; filename="{p.name}"',
        "Cache-Control": "no-cache, must-revalidate",
        "X-Content-Type-Options": "nosniff",
    }
    return FileResponse(str(p), media_type=mime, headers=headers)


@router.get("/media/shared-session/{key}/download")
async def shared_session_download(key: str, path: str):
    """Serve a file as a download attachment in shared sessions (unauthenticated, secured by share_key)."""
    from core.db import db_get_shared_session
    from routes.agent.download import _resolve_requested_file, MIME_MAP, VIDEO_MIME
    data = db_get_shared_session(key)
    if not data:
        return JSONResponse({"error": "Shared session not found or link has expired"}, status_code=404)

    p = _resolve_requested_file(path, recover=False, user_id=data.get("user_id"))
    if p is None or not p.is_file():
        return JSONResponse({"error": f"file not found: {path}"}, status_code=404)

    suffix = p.suffix.lower()
    mime = MIME_MAP.get(suffix) or VIDEO_MIME.get(suffix) or "application/octet-stream"
    headers = {
        "Content-Disposition": f'attachment; filename="{p.name}"',
        "Cache-Control": "no-cache, must-revalidate",
    }
    return FileResponse(str(p), media_type=mime, headers=headers)


# --- Static assets (unauthenticated: CSS/JS/images carry no sensitive data) ---
@router.get("/static/{file_path:path}")
async def serve_static(file_path: str):
    p = (STATIC_DIR / file_path).resolve()
    if p.exists() and p.is_file() and p.is_relative_to(STATIC_DIR.resolve()):
        media_types = {
            ".css": "text/css",
            ".js": "application/javascript",
            ".jpg": "image/jpeg",
            ".jpeg": "image/jpeg",
            ".png": "image/png",
            ".svg": "image/svg+xml",
            ".ico": "image/x-icon",
            ".webp": "image/webp",
            ".webmanifest": "application/manifest+json",
        }
        media = media_types.get(p.suffix.lower())
        return FileResponse(p, media_type=media,
                            headers={"Cache-Control": "no-cache, must-revalidate"})
    return JSONResponse({"error": "file not found"}, status_code=404)


@router.get("/favicon.ico")
async def favicon():
    p = (STATIC_DIR / "logo" / "logo.jpg").resolve()
    if p.exists() and p.is_file():
        return FileResponse(p, media_type="image/jpeg",
                            headers={"Cache-Control": "public, max-age=86400"})
    return JSONResponse({"error": "favicon not found"}, status_code=404)


@router.get("/settings")
async def settings_page(request: Request):
    """The settings page the React settings modal embeds. Signed out: to /, where the React app shows its login."""
    try:
        await get_current_user(request)
    except Exception:
        return RedirectResponse(url="/")
    if SETTINGS_FILE.exists():
        return FileResponse(SETTINGS_FILE, media_type="text/html", headers={"Cache-Control": "no-store, max-age=0"})
    return JSONResponse({"error": f"settings.html not found at {SETTINGS_FILE}"}, status_code=404)

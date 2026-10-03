"""
routes/pages.py - The HTML shell pages (/, /login, /settings) and static assets.
"""

from fastapi import APIRouter, Request
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse
from core.config import BASE_DIR, STATIC_DIR, UI_FILE
from core.deps import get_current_user

router = APIRouter()

LOGIN_FILE = BASE_DIR / "login.html"
SETTINGS_FILE = BASE_DIR / "settings.html"


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


@router.get("/login")
async def login_page():
    if LOGIN_FILE.exists():
        return FileResponse(LOGIN_FILE, media_type="text/html",
                            headers={"Cache-Control": "no-store, max-age=0"})
    return JSONResponse({"error": f"login.html not found at {LOGIN_FILE}"}, status_code=404)


@router.get("/settings")
async def settings_page(request: Request):
    try:
        await get_current_user(request)
    except Exception:
        return RedirectResponse(url="/login?next=/settings")
    if SETTINGS_FILE.exists():
        return FileResponse(SETTINGS_FILE, media_type="text/html",
                            headers={"Cache-Control": "no-store, max-age=0"})
    return JSONResponse({"error": f"settings.html not found at {SETTINGS_FILE}"}, status_code=404)


@router.get("/")
async def ui_root(request: Request):
    try:
        await get_current_user(request)
    except Exception:
        return RedirectResponse(url="/login?next=/")
    if UI_FILE.exists():
        return FileResponse(UI_FILE, media_type="text/html",
                            headers={"Cache-Control": "no-store, max-age=0"})
    return JSONResponse({"error": f"ui.html not found at {UI_FILE}"}, status_code=404)

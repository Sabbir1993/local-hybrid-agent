"""
routes/pages.py - The HTML shell pages (/, /login, /settings), React SPA, and static assets.
"""

from fastapi import APIRouter, Request
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse
from core.config import BASE_DIR, STATIC_DIR, UI_FILE
from core.deps import get_current_user

router = APIRouter()

LOGIN_FILE = BASE_DIR / "login.html"
SETTINGS_FILE = BASE_DIR / "settings.html"
DIST_DIR = BASE_DIR / "frontend" / "dist"
DIST_INDEX = DIST_DIR / "index.html"
DIST_ASSETS_DIR = DIST_DIR / "assets"


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


@router.get("/react")
async def react_ui(request: Request):
    if DIST_INDEX.exists():
        return FileResponse(DIST_INDEX, media_type="text/html", headers={"Cache-Control": "no-store, max-age=0"})
    return JSONResponse({"error": "React build not found. Run npm run build in frontend/"}, status_code=404)


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


@router.get("/report")
async def report_page(request: Request):
    try:
        await get_current_user(request)
    except Exception:
        return RedirectResponse(url="/login?next=/report")
    if DIST_INDEX.exists():
        return FileResponse(DIST_INDEX, media_type="text/html",
                            headers={"Cache-Control": "no-store, max-age=0"})
    return RedirectResponse(url="/")


@router.get("/audit")
async def audit_page(request: Request):
    try:
        await get_current_user(request)
    except Exception:
        return RedirectResponse(url="/login?next=/audit")
    if DIST_INDEX.exists():
        return FileResponse(DIST_INDEX, media_type="text/html",
                            headers={"Cache-Control": "no-store, max-age=0"})
    return RedirectResponse(url="/")


@router.get("/db")
async def db_page(request: Request):
    try:
        await get_current_user(request)
    except Exception:
        return RedirectResponse(url="/login?next=/db")
    if DIST_INDEX.exists():
        return FileResponse(DIST_INDEX, media_type="text/html",
                            headers={"Cache-Control": "no-store, max-age=0"})
    return RedirectResponse(url="/")


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

"""
routes/pages.py - The React app (served at / and its client-side paths), its build assets, and static files.

The UI is the React app in frontend/ (built to frontend/dist). It shows its own login screen when there is no session, and
every data endpoint checks authentication itself. Its settings modal embeds settings.html (served at /settings).
"""

from fastapi import APIRouter, Request
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse
from core.config import BASE_DIR, STATIC_DIR
from core.deps import get_current_user

router = APIRouter()

DIST_DIR = BASE_DIR / "frontend" / "dist"
DIST_INDEX = DIST_DIR / "index.html"
DIST_ASSETS_DIR = DIST_DIR / "assets"
# The React settings modal shows this page in an iframe (frontend/src/components/modals/SettingsModal.tsx).
SETTINGS_FILE = BASE_DIR / "settings.html"
REACT_MISSING = {"error": "React build not found. Run `npm run build:prod` (or `npm run build`) in frontend/."}


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


# The React app handles these paths itself (login, report, audit, database and settings are screens inside it).
@router.get("/")
async def react_root():
    return _react_index()


@router.get("/react")
async def react_alias():
    return _react_index()


@router.get("/login")
@router.get("/report")
@router.get("/audit")
@router.get("/db")
async def react_screens():
    return _react_index()


@router.get("/settings")
async def settings_page(request: Request):
    """The settings page the React app embeds. Signed out: back to / where the React app shows its login."""
    try:
        await get_current_user(request)
    except Exception:
        return RedirectResponse(url="/")
    if SETTINGS_FILE.exists():
        return FileResponse(SETTINGS_FILE, media_type="text/html", headers={"Cache-Control": "no-store, max-age=0"})
    return JSONResponse({"error": f"settings.html not found at {SETTINGS_FILE}"}, status_code=404)


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

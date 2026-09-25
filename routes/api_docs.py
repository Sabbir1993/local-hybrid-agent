"""routes/api_docs.py - OpenAPI schema + Swagger UI behind authentication.

FastAPI's built-in /docs, /redoc and /openapi.json are disabled (server_manager.py);
these replacements require a browser session or an API token. Turn them off with
config/app.json security.api_docs_enabled = false.
"""

from fastapi import APIRouter, HTTPException, Request
from fastapi.openapi.docs import get_swagger_ui_html
from fastapi.responses import JSONResponse, RedirectResponse

from core.deps import get_current_user
from core.small_model import APP_CONFIG

router = APIRouter(include_in_schema=False)

# Swagger UI loads its bundle from jsDelivr and boots with an inline script, so this one
# page carries its own CSP (the global middleware never overrides a handler's header).
_DOCS_CSP = ("default-src 'self'; script-src 'self' 'unsafe-inline' https://cdn.jsdelivr.net; "
             "style-src 'self' 'unsafe-inline' https://cdn.jsdelivr.net; img-src 'self' data: https:; "
             "connect-src 'self'; object-src 'none'; base-uri 'self'; frame-ancestors 'self'")


def _enabled() -> bool:
    return bool((APP_CONFIG.get("security") or {}).get("api_docs_enabled", True))


@router.get("/openapi.json")
async def openapi_schema(request: Request):
    if not _enabled():
        raise HTTPException(status_code=404)
    await get_current_user(request)
    schema = dict(request.app.openapi())
    comps = dict(schema.get("components") or {})
    comps["securitySchemes"] = {"bearerAuth": {"type": "http", "scheme": "bearer",
                                               "description": "API token (a770_pat_...) issued by an admin"}}
    schema["components"] = comps
    schema["security"] = [{"bearerAuth": []}]
    return JSONResponse(schema)


@router.get("/docs")
async def swagger_ui(request: Request):
    if not _enabled():
        raise HTTPException(status_code=404)
    try:
        await get_current_user(request)
    except HTTPException:
        return RedirectResponse(url="/login?next=/docs")
    resp = get_swagger_ui_html(openapi_url="/openapi.json", title="Local Agent API")
    resp.headers["Content-Security-Policy"] = _DOCS_CSP
    resp.headers["Cache-Control"] = "no-store"
    return resp

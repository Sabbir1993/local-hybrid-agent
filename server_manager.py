#!/usr/bin/env python3
"""
server_manager.py - Process manager + orchestrator for dual-A770 Intel Arc Vulkan runtime.

Thin bootstrap and lifespan manager. Route handlers are modularized into the `routes` package:
  - routes.control: Server lifecycle, profiles, GPU stats, config, and monitor.
  - routes.projects: Projects, sessions, messages, and filesystem browsing.
  - routes.capabilities: Skills, plugins, MCP, shell permissions, and model status.
  - routes.chat: Chat completions with real-time web search and tool execution.
  - routes.agent: Multi-turn autonomous coding loop, permissions, vision, and workspace tools.
  - routes.proxy: OpenAI-compatible reverse proxy forwarding to llama-server.
"""

import argparse
import os
import sys
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

import uvicorn
from fastapi import Depends, FastAPI, Request
from fastapi.responses import JSONResponse

from core.config import PROXY_HOST, PROXY_PORT
from core.csrf import CSRFMiddleware, SecurityHeadersMiddleware
from core.deps import get_current_user, require_permission
from core.limits import RequestLimitsMiddleware
from core.agent_tools import WorkspaceAccessDenied
from core.companion_bridge import router as companion_router
from routes.api_docs import router as api_docs_router
from routes.api_tokens import router as api_tokens_router, self_router as my_tokens_router

from routes import (
    admin_rbac_router,
    agent_router,
    auth_router,
    capabilities_router,
    chat_router,
    cloud_router,
    control_router,
    lanes_router,
    media_router,
    db_explorer_router,
    git_router,
    input_guard_router,
    knowledge_router,
    mcp_manager_router,
    plugins_router,
    customize_router,
    projects_router,
    proxy_router,
    custom_agents_router,
)
from routes import common
from routes.pages import router as pages_router
from core.startup import lifespan





# built-in docs are unauthenticated; routes/api_docs.py serves them behind login / API token
app = FastAPI(title="Local Agent", lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
# Innermost of the three: body size and request rate are cheap rejections, so they run before
# the CSRF check and the security headers rather than after
app.add_middleware(RequestLimitsMiddleware)
app.add_middleware(CSRFMiddleware)
app.add_middleware(SecurityHeadersMiddleware)


@app.exception_handler(WorkspaceAccessDenied)
async def _workspace_denied(request: Request, exc: WorkspaceAccessDenied):
    # agent workspaces exist only on users' machines (core/agent_tools.py
    # require_device_workspace); the server's disk is never served instead
    return JSONResponse({"error": str(exc)}, status_code=403)



app.include_router(pages_router)


# --- Register modular routers ---
# /auth is the only surface reachable without a session (login itself).
# Everything else requires an authenticated principal at minimum; specific
# routes layer finer-grained require_permission() checks on top (see each
# router's own dependencies=[...] for the privileged endpoints).
app.include_router(auth_router)
app.include_router(api_docs_router)        # checks auth itself (redirects the browser to /login)
_authed = [Depends(get_current_user)]
# chat.use is the default "user" permission; revoking it must actually cut chat/agent access
_chat_users = [Depends(require_permission("chat.use"))]
app.include_router(control_router, dependencies=_authed)
app.include_router(projects_router, dependencies=_authed)
app.include_router(capabilities_router, dependencies=_authed)
app.include_router(cloud_router, dependencies=_chat_users)
app.include_router(lanes_router, dependencies=_chat_users)
app.include_router(media_router, dependencies=_chat_users)
app.include_router(chat_router, dependencies=_chat_users)
app.include_router(agent_router, dependencies=_chat_users)
app.include_router(git_router, dependencies=_chat_users)
app.include_router(mcp_manager_router, dependencies=_authed)
app.include_router(plugins_router, dependencies=_authed)
app.include_router(customize_router, dependencies=_authed)
app.include_router(admin_rbac_router, dependencies=_authed)
app.include_router(api_tokens_router, dependencies=_authed)
app.include_router(my_tokens_router, dependencies=_authed)
app.include_router(knowledge_router, dependencies=_authed)
app.include_router(input_guard_router, dependencies=_authed)
app.include_router(db_explorer_router, dependencies=_authed)
app.include_router(custom_agents_router, dependencies=_authed)
# Own auth handling (session cookie checked inside the socket handler) since
# Depends() on a websocket route doesn't compose with the HTTP auth flow above.
# Must sit before the proxy catch-all, or /companion/pair and /companion/devices 404.
app.include_router(companion_router)
# Reverse proxy catch-all must be mounted last
app.include_router(proxy_router, dependencies=_authed)


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--profile", required=False, default=None, help="Path to initial profile JSON (optional)")
    ap.add_argument("--models-dir", required=False, default=None, help="Path to directory containing .gguf models")
    ap.add_argument("--port", type=int, default=PROXY_PORT, help="Port for this proxy (default 8000)")
    ap.add_argument("--host", default=os.environ.get("A770_HOST", PROXY_HOST),
                    help="Bind address (default %(default)s; env A770_HOST). Use 127.0.0.1 behind "
                         "a TLS reverse proxy / tunnel so sessions never cross the LAN in plaintext")
    args = ap.parse_args()

    if args.profile:
        common.initial_profile_path = Path(args.profile)
    if args.models_dir:
        common.models_dir = Path(args.models_dir)
        from core.profiles import register_models_root
        register_models_root(common.models_dir)

    if args.host not in ("127.0.0.1", "localhost", "::1"):
        # PCI DSS 4.2.1: session cookies over plain HTTP on the LAN are sniffable
        print(f"[server_manager] WARNING: listening on {args.host}:{args.port} over plain HTTP. "
              "Bind to 127.0.0.1 (--host) and put TLS in front unless this network is trusted.",
              file=sys.stderr)
    # proxy_headers: behind a local TLS proxy / cloudflared, X-Forwarded-Proto makes
    # request.url.scheme "https" so cookies get the Secure flag (FORWARDED_ALLOW_IPS env)
    uvicorn.run(app, host=args.host, port=args.port, proxy_headers=True)


if __name__ == "__main__":
    main()

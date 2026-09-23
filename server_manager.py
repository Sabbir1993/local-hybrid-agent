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
import asyncio
import getpass
import json
import secrets
import sys
from contextlib import asynccontextmanager
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

import uvicorn
from fastapi import Depends, FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse

from core.config import (
    BASE_DIR,
    PROVIDERS_DIR,
    PROVIDERS_FILE,
    PROXY_HOST,
    PROXY_PORT,
    STATIC_DIR,
    UI_FILE,
)
from core import auth_db
from core.auth_provider import hash_password
from core.csrf import CSRFMiddleware
from core.deps import get_current_user
from core.mcp import connect_all_mcp, stop_all_mcp
from core.plugins import load_plugins
from core.process import kill_orphan_llama_servers
from core.registry import bootstrap_builtin_tools
from core.shell_tools import register_shell_tools
from core.companion_bridge import router as companion_router
from core.skills import register_skill_tools
from core.small_model import small_models
from core.memory import memory_background_task
from core.state import keepalive_loop, state
from core.web_tools import register_web_tools
from core.file_tools import register_file_tools

from routes import (
    admin_rbac_router,
    agent_router,
    auth_router,
    capabilities_router,
    chat_router,
    cloud_router,
    control_router,
    db_explorer_router,
    git_router,
    input_guard_router,
    knowledge_router,
    mcp_manager_router,
    projects_router,
    proxy_router,
)
from routes import common

LOGIN_FILE = BASE_DIR / "login.html"
SETTINGS_FILE = BASE_DIR / "settings.html"


def _bootstrap_super_admin() -> None:
    """First-boot only: create the one super-admin account. Never a hardcoded
    default credential -- taken from env vars if present (for scripted/CI
    setup), otherwise prompted interactively on the console."""
    if auth_db.count_users() > 0:
        return
    username = None
    password = None
    import os
    username = os.environ.get("A770_BOOTSTRAP_USERNAME")
    password = os.environ.get("A770_BOOTSTRAP_PASSWORD")
    if not username or not password:
        print("\n[auth] No users exist yet -- create the initial super-admin account.")
        try:
            username = input("  Username: ").strip() or "admin"
            password = getpass.getpass("  Password (leave blank to auto-generate): ")
        except (EOFError, OSError):
            username, password = "admin", None
    if not password:
        password = secrets.token_urlsafe(16)
        print(f"[auth] Generated super-admin password (save this now): {password}")
    uid = auth_db.create_user(username=username, password_hash=hash_password(password), is_super_admin=True)
    auth_db.assign_role(uid, "admin")
    print(f"[auth] Super-admin account '{username}' created.\n")


def _backfill_legacy_data_ownership() -> None:
    """Attribute any pre-auth projects.db rows (from before this feature existed)
    to a super admin so nothing is left ownerless. Idempotent no-op once done."""
    from core.db import backfill_legacy_owner
    row = auth_db.db().execute(
        "SELECT id FROM users WHERE is_super_admin = 1 ORDER BY id LIMIT 1"
    ).fetchone()
    if row:
        backfill_legacy_owner(row["id"])


def _backfill_legacy_cloud_providers() -> None:
    """One-time migration: the old shared config/providers.json (pre-per-user
    cloud config) becomes the first super admin's own per-user file, since
    core.cloud no longer reads the legacy shared file at all. Idempotent --
    a no-op once the legacy file is gone or the target already exists."""
    if not PROVIDERS_FILE.exists():
        return
    row = auth_db.db().execute(
        "SELECT id FROM users WHERE is_super_admin = 1 ORDER BY id LIMIT 1"
    ).fetchone()
    if not row:
        return
    target = PROVIDERS_DIR / f"user_{row['id']}.json"
    if target.exists():
        return
    try:
        PROVIDERS_DIR.mkdir(parents=True, exist_ok=True)
        target.write_text(PROVIDERS_FILE.read_text(encoding="utf-8"), encoding="utf-8")
        PROVIDERS_FILE.rename(PROVIDERS_FILE.with_suffix(".json.migrated"))
        print(f"[server_manager] migrated legacy config/providers.json -> {target.name}")
    except Exception as e:
        print(f"[server_manager] legacy cloud provider migration failed: {e}", file=sys.stderr)


_shutdown_done = False


def _shutdown_cleanup() -> None:
    """Kill llama-server + small models + MCP servers. Blocking, idempotent."""
    global _shutdown_done
    if _shutdown_done:
        return
    _shutdown_done = True
    try:
        state._stop_process_locked()
    except Exception as e:
        print(f"[server_manager] main model cleanup error: {e}", file=sys.stderr)
    for role, inst in small_models.instances.items():
        try:
            inst._stop()
        except Exception as e:
            print(f"[server_manager] {role} cleanup error: {e}", file=sys.stderr)
    try:
        stop_all_mcp()
    except Exception as e:
        print(f"[server_manager] mcp cleanup error: {e}", file=sys.stderr)
    print("[server_manager] shutdown cleanup complete")


@asynccontextmanager
async def lifespan(app: FastAPI):
    _bootstrap_super_admin()
    _backfill_legacy_data_ownership()
    _backfill_legacy_cloud_providers()
    bootstrap_builtin_tools()
    register_web_tools()
    register_skill_tools()
    register_shell_tools()
    register_file_tools()
    load_plugins()
    await asyncio.get_event_loop().run_in_executor(None, kill_orphan_llama_servers)
    asyncio.create_task(connect_all_mcp())
    if common.initial_profile_path is not None and common.initial_profile_path.exists():
        try:
            state.profile_path = common.initial_profile_path
            state.profile = json.loads(common.initial_profile_path.read_text(encoding="utf-8"))
            print(f"[server_manager] Selected initial profile: {state.profile.get('name')}")
        except Exception as e:
            print(f"[server_manager] Warning loading initial profile JSON: {e}")

    state.watchdog_task = asyncio.create_task(state.watchdog())
    state.keepalive_task = asyncio.create_task(keepalive_loop())
    small_models.start_reaper()
    memory_task = asyncio.create_task(memory_background_task())
    yield
    # Fast, cancellable: stop background loops first
    for task in (state.watchdog_task, state.keepalive_task, small_models.reaper_task, memory_task):
        if task:
            task.cancel()
    # Blocking process teardown runs off the loop thread
    try:
        await asyncio.shield(asyncio.to_thread(_shutdown_cleanup))
    except (asyncio.CancelledError, KeyboardInterrupt):
        _shutdown_cleanup()


app = FastAPI(title="Local Agent", lifespan=lifespan)
app.add_middleware(CSRFMiddleware)

# --- Static assets (unauthenticated: CSS/JS carry no sensitive data) ---
@app.get("/static/{file_path:path}")
async def serve_static(file_path: str):
    p = (STATIC_DIR / file_path).resolve()
    if p.exists() and p.is_file() and str(p).startswith(str(STATIC_DIR.resolve())):
        media = "text/css" if p.suffix == ".css" else ("application/javascript" if p.suffix == ".js" else None)
        return FileResponse(p, media_type=media,
                            headers={"Cache-Control": "no-cache, must-revalidate"})
    return JSONResponse({"error": "file not found"}, status_code=404)


@app.get("/login")
async def login_page():
    if LOGIN_FILE.exists():
        return FileResponse(LOGIN_FILE, media_type="text/html",
                            headers={"Cache-Control": "no-store, max-age=0"})
    return JSONResponse({"error": f"login.html not found at {LOGIN_FILE}"}, status_code=404)


@app.get("/settings")
async def settings_page(request: Request):
    try:
        await get_current_user(request)
    except Exception:
        return RedirectResponse(url="/login?next=/settings")
    if SETTINGS_FILE.exists():
        return FileResponse(SETTINGS_FILE, media_type="text/html",
                            headers={"Cache-Control": "no-store, max-age=0"})
    return JSONResponse({"error": f"settings.html not found at {SETTINGS_FILE}"}, status_code=404)


@app.get("/")
async def ui_root(request: Request):
    try:
        await get_current_user(request)
    except Exception:
        return RedirectResponse(url="/login?next=/")
    if UI_FILE.exists():
        return FileResponse(UI_FILE, media_type="text/html",
                            headers={"Cache-Control": "no-store, max-age=0"})
    return JSONResponse({"error": f"ui.html not found at {UI_FILE}"}, status_code=404)


# --- Register modular routers ---
# /auth is the only surface reachable without a session (login itself).
# Everything else requires an authenticated principal at minimum; specific
# routes layer finer-grained require_permission() checks on top (see each
# router's own dependencies=[...] for the privileged endpoints).
app.include_router(auth_router)
_authed = [Depends(get_current_user)]
app.include_router(control_router, dependencies=_authed)
app.include_router(projects_router, dependencies=_authed)
app.include_router(capabilities_router, dependencies=_authed)
app.include_router(cloud_router, dependencies=_authed)
app.include_router(chat_router, dependencies=_authed)
app.include_router(agent_router, dependencies=_authed)
app.include_router(git_router, dependencies=_authed)
app.include_router(mcp_manager_router, dependencies=_authed)
app.include_router(admin_rbac_router, dependencies=_authed)
app.include_router(knowledge_router, dependencies=_authed)
app.include_router(input_guard_router, dependencies=_authed)
app.include_router(db_explorer_router, dependencies=_authed)
# Reverse proxy catch-all must be mounted last
app.include_router(proxy_router, dependencies=_authed)
# Own auth handling (session cookie checked inside the socket handler) since
# Depends() on a websocket route doesn't compose with the HTTP auth flow above.
app.include_router(companion_router)


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--profile", required=False, default=None, help="Path to initial profile JSON (optional)")
    ap.add_argument("--models-dir", required=False, default=None, help="Path to directory containing .gguf models")
    ap.add_argument("--port", type=int, default=PROXY_PORT, help="Port for this proxy (default 8000)")
    args = ap.parse_args()

    if args.profile:
        common.initial_profile_path = Path(args.profile)
    if args.models_dir:
        common.models_dir = Path(args.models_dir)
        from core.profiles import register_models_root
        register_models_root(common.models_dir)

    uvicorn.run(app, host=PROXY_HOST, port=args.port)


if __name__ == "__main__":
    main()

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
import json
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
from fastapi import FastAPI
from fastapi.responses import FileResponse, JSONResponse

from core.config import (
    PROXY_HOST,
    PROXY_PORT,
    STATIC_DIR,
    UI_FILE,
)
from core.mcp import connect_all_mcp, stop_all_mcp
from core.plugins import load_plugins
from core.process import kill_orphan_llama_servers
from core.registry import bootstrap_builtin_tools
from core.shell_tools import register_shell_tools
from core.skills import register_skill_tools
from core.small_model import small_models
from core.memory import memory_background_task
from core.state import keepalive_loop, state
from core.web_tools import register_web_tools
from core.file_tools import register_file_tools

from routes import (
    agent_router,
    capabilities_router,
    chat_router,
    control_router,
    projects_router,
    proxy_router,
)
from routes import common

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


app = FastAPI(title="A770-Dual Runtime", lifespan=lifespan)

# --- Static assets & Root UI ---
@app.get("/static/{file_path:path}")
async def serve_static(file_path: str):
    p = (STATIC_DIR / file_path).resolve()
    if p.exists() and p.is_file() and str(p).startswith(str(STATIC_DIR.resolve())):
        media = "text/css" if p.suffix == ".css" else ("application/javascript" if p.suffix == ".js" else None)
        return FileResponse(p, media_type=media,
                            headers={"Cache-Control": "no-cache, must-revalidate"})
    return JSONResponse({"error": "file not found"}, status_code=404)


@app.get("/")
async def ui_root():
    if UI_FILE.exists():
        return FileResponse(UI_FILE, media_type="text/html",
                            headers={"Cache-Control": "no-store, max-age=0"})
    return JSONResponse({"error": f"ui.html not found at {UI_FILE}"}, status_code=404)


# --- Register modular routers ---
app.include_router(control_router)
app.include_router(projects_router)
app.include_router(capabilities_router)
app.include_router(chat_router)
app.include_router(agent_router)
# Reverse proxy catch-all must be mounted last
app.include_router(proxy_router)


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

    uvicorn.run(app, host=PROXY_HOST, port=args.port)


if __name__ == "__main__":
    main()

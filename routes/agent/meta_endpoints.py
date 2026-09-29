import re
import sys
from fastapi import Depends
from fastapi.responses import JSONResponse
from core.auth import Principal
from core.deps import get_current_user
from core.audit import audit_log
from core.project_context import load_project_instructions, INIT_PROMPT
from core.agent_tools import get_active_project, require_device_workspace
from core import companion_bridge
from core.agent_library import load_prompt_commands, expand_command
from core import route_log

from .base import router
from .models import CommandExpandReq, RunFeedbackReq


@router.get("/agent/project_instructions")
async def agent_project_instructions(user: Principal = Depends(get_current_user)):
    """Whether the active project has an AGENTS.md (drives the /init hint)."""
    proj = get_active_project()
    if not proj:
        return {"project": None, "exists": False, "filename": None, "size": 0, "init_prompt": INIT_PROMPT}
    pi = None
    try:
        pi = await load_project_instructions()
    except Exception as e:
        print(f"[agent] project instructions status failed: {e}", file=sys.stderr)
    return {"project": proj, "exists": bool(pi), "filename": pi[0] if pi else None,
            "size": len(pi[1]) if pi else 0, "init_prompt": INIT_PROMPT}


@router.post("/agent/feedback")
async def agent_feedback(req: RunFeedbackReq, user: Principal = Depends(get_current_user)):
    """Thumbs on an agent answer -- feeds the router tuner (core/router_tuner.py)."""
    if req.rating not in (-1, 0, 1) or not re.fullmatch(r"[0-9a-f]{32}", req.run_id or ""):
        return JSONResponse({"error": "invalid feedback"}, status_code=400)
    if not route_log.rate(req.run_id, user.id, req.rating or None):
        return JSONResponse({"error": "run not found"}, status_code=404)
    return {"ok": True}


@router.get("/agent/commands")
async def agent_commands(user: Principal = Depends(get_current_user)):
    """Admin-allowed prompt commands (.agents/commands) for the slash menu."""
    return {"items": [{"name": c["name"], "description": c["description"],
                       "argument_hint": c["argument_hint"]} for c in load_prompt_commands().values()]}


@router.post("/agent/command/expand")
async def agent_command_expand(req: CommandExpandReq, user: Principal = Depends(get_current_user)):
    """Prompt command -> agent prompt. The client runs it via /agent/run as a normal task."""
    prompt = expand_command(req.name, req.args)
    if prompt is None:
        audit_log(user, action="agent.command", resource=req.name, result="deny")
        return JSONResponse({"error": f"prompt command '{req.name}' is not available"}, status_code=404)
    audit_log(user, action="agent.command", resource=req.name)
    return {"name": req.name, "prompt": prompt}


@router.get("/agent/workspace")
async def agent_workspace():
    # flat file list for @-tagging, read from the user's machine via the companion
    uid, ws = require_device_workspace()
    data = await companion_bridge.call(uid, "fs.list", {"root": str(ws), "pattern": "**/*"})
    files = [{"path": f} for f in sorted(data.get("files") or []) if isinstance(f, str)]
    return {"root": str(ws), "project": get_active_project(), "files": files[:500]}

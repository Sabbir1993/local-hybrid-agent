from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse
from core.audit import audit_log
from core.auth import Principal
from core.auth_db import (
    CustomAgentSlugTaken,
    db_create_custom_agent,
    db_delete_custom_agent,
    db_fork_custom_agent,
    db_get_custom_agent,
    db_list_custom_agents,
    db_update_custom_agent,
)
from core.deps import get_current_user
from core.registry import registry
from .helpers import (
    _check_publish,
    _check_slug,
    _decorate,
)
from .models import (
    CustomAgentCreateReq,
    CustomAgentUpdateReq,
    ForkReq,
)

router = APIRouter(prefix="/custom-agents", tags=["custom_agents"])


@router.get("")
async def list_custom_agents(user: Principal = Depends(get_current_user)):
    """List custom agents accessible to current user (own agents + system templates/public)."""
    agents = db_list_custom_agents(user.id, include_public=True)
    return {"agents": [_decorate(a, user) for a in agents]}


@router.post("")
async def create_custom_agent(req: CustomAgentCreateReq, user: Principal = Depends(get_current_user)):
    """Create a new custom agent for the signed-in user."""
    data = req.model_dump()
    err = _check_publish(user, data.get("is_public")) or _check_slug(data.get("slug"), data.get("name"))
    if err:
        return err
    try:
        new_agent = db_create_custom_agent(user.id, data)
        audit_log(user, action="custom_agent.create", resource=new_agent.get("slug"))
        return _decorate(new_agent, user)
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=400)


@router.get("/tools/available")
async def available_tools(user: Principal = Depends(get_current_user)):
    """Available agent tools grouped by category for the agent builder."""
    from core.request_context import set_current_user
    set_current_user(user.id)

    tool_meta = {
        "read_file": {"label": "Read File", "cat": "Files & Code", "desc": "Safely read workspace files."},
        "write_file": {"label": "Write File", "cat": "Files & Code", "desc": "Create or overwrite files in the workspace."},
        "edit_file": {"label": "Edit File", "cat": "Files & Code", "desc": "Apply targeted line or string replacements."},
        "list_files": {"label": "List Files", "cat": "Files & Code", "desc": "Glob and list files in workspace."},
        "grep": {"label": "Grep Search", "cat": "Files & Code", "desc": "Fast regex search across workspace files."},
        "run_python": {"label": "Run Python", "cat": "Files & Code", "desc": "Execute Python scripts inside the workspace sandbox."},
        "run_shell": {"label": "Run Shell / Terminal", "cat": "Files & Code", "desc": "Execute terminal commands (gated by permission)."},
        "list_diff": {"label": "Session Diff", "cat": "Files & Code", "desc": "View modified or created files during the session."},
        "revert": {"label": "Revert File", "cat": "Files & Code", "desc": "Rollback file to pre-session snapshot."},
        "doc_inspect": {"label": "Inspect Documents", "cat": "Documents & Office", "desc": "Inspect slide, cell, and paragraph structures."},
        "doc_edit": {"label": "Edit Documents", "cat": "Documents & Office", "desc": "Modify Excel/PowerPoint/Word files with targeted ops."},
        "read_file_chunk": {"label": "Read File Chunk", "cat": "Documents & Office", "desc": "Page through large documents & datasets."},
        "web_search": {"label": "Web Search", "cat": "Web & Research", "desc": "Query search engines for fresh internet data."},
        "web_fetch": {"label": "Web Fetch", "cat": "Web & Research", "desc": "Download and extract content from public web URLs."},
        "search_knowledge_base": {"label": "Knowledge Base", "cat": "Web & Research", "desc": "Search internal company organizational documents."},
        "analyze_image": {"label": "Analyze Image", "cat": "Vision & Media", "desc": "Inspect workspace pictures using local vision model."},
        "spawn_agent": {"label": "Spawn Sub-Agent", "cat": "Multi-Agent", "desc": "Delegate sub-tasks to focused helper agents."},
    }
    from core.browser_tools import BROWSER_TOOLS
    from core.device_tools import DEVICE_TOOLS
    for n, _fn, schema in BROWSER_TOOLS + DEVICE_TOOLS:
        cat = "Browser Testing" if n.startswith("browser_") else "Mobile Testing"
        tool_meta.setdefault(n, {"label": n.split("_", 1)[1].replace("_", " ").title(), "cat": cat,
                                 "desc": schema["function"]["description"].split(". ")[0][:140]})

    discovered = []
    registered_tools = registry.list(include_disabled=True)
    registered_map = {t.name: t for t in registered_tools}
    all_names = set(registered_map.keys()) | set(tool_meta.keys())

    for name in sorted(all_names):
        if name in tool_meta:
            info = tool_meta[name]
        elif name.startswith("mcp__"):
            parts = name.split("__", 2)
            srv = parts[1] if len(parts) > 1 else "mcp"
            t_base = parts[2] if len(parts) > 2 else parts[-1]
            reg_t = registered_map.get(name) or registry.get(name)
            desc = ""
            if reg_t and reg_t.schema and isinstance(reg_t.schema, dict):
                fn = reg_t.schema.get("function", {})
                desc = fn.get("description", "")
            info = {
                "label": t_base.replace("_", " ").title(),
                "cat": f"MCP: {srv.title()}",
                "desc": desc or f"MCP tool from {srv}"
            }
        else:
            info = {
                "label": name.replace("_", " ").title(),
                "cat": "Other / Extensions",
                "desc": f"Tool: {name}"
            }
        discovered.append({
            "name": name,
            "label": info["label"],
            "category": info["cat"],
            "description": info["desc"]
        })

    return {"tools": discovered}


@router.get("/{agent_id}")
async def get_custom_agent(agent_id: int, user: Principal = Depends(get_current_user)):
    """Retrieve details for a single agent."""
    agent = db_get_custom_agent(agent_id, user.id)
    if not agent:
        return JSONResponse({"error": "Agent not found"}, status_code=404)
    return _decorate(agent, user)


@router.put("/{agent_id}")
async def update_custom_agent(agent_id: int, req: CustomAgentUpdateReq, user: Principal = Depends(get_current_user)):
    """Update a custom agent owned by user."""
    data = req.model_dump(exclude_unset=True)
    err = _check_publish(user, data.get("is_public")) or (_check_slug(data["slug"]) if data.get("slug") else None)
    if err:
        return err
    if not db_get_custom_agent(agent_id):
        return JSONResponse({"error": "Agent not found"}, status_code=404)
    try:
        updated = db_update_custom_agent(agent_id, user.id, data)
    except CustomAgentSlugTaken as e:
        return JSONResponse({"error": f"slug '{e}' is already used by another agent"}, status_code=409)
    if not updated:
        return JSONResponse({"error": "permission denied"}, status_code=403)
    audit_log(user, action="custom_agent.update", resource=updated.get("slug"))
    return _decorate(updated, user)


@router.delete("/{agent_id}")
async def delete_custom_agent(agent_id: int, user: Principal = Depends(get_current_user)):
    """Delete a custom agent owned by user."""
    ok = db_delete_custom_agent(agent_id, user.id)
    if not ok:
        return JSONResponse({"error": "Agent not found or permission denied"}, status_code=403)
    audit_log(user, action="custom_agent.delete", resource=str(agent_id))
    return {"ok": True}


@router.post("/{agent_id}/fork")
async def fork_custom_agent(agent_id: int, req: ForkReq, user: Principal = Depends(get_current_user)):
    """Clone an agent or starter template into user's own custom agents."""
    forked = db_fork_custom_agent(agent_id, user.id, req.name)
    if not forked:
        return JSONResponse({"error": "Source agent not found"}, status_code=404)
    audit_log(user, action="custom_agent.fork", resource=forked.get("slug"))
    return _decorate(forked, user)

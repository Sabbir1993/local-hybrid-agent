from fastapi import APIRouter, Depends, Request
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
    db_list_pending_agents,
    db_set_share_status,
    db_update_custom_agent,
)
from core.deps import get_current_user
from core.registry import registry
from .helpers import (
    can_approve,
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
async def create_custom_agent(req: CustomAgentCreateReq, request: Request, user: Principal = Depends(get_current_user)):
    """Create a new custom agent for the signed-in user."""
    data = req.model_dump()
    if not data.get("device_id"):
        data["device_id"] = request.headers.get("X-Device-Id", "")
    if not data.get("device_name"):
        data["device_name"] = request.headers.get("X-Device-Name", "")
    err = _check_slug(data.get("slug"), data.get("name"))
    if err:
        return err
    # sharing waits for approval unless the author can approve it themselves
    data["share_status"] = ("approved" if can_approve(user) else "pending") if data.pop("is_public", False) else ""
    try:
        new_agent = db_create_custom_agent(user.id, data)
        audit_log(user, action="custom_agent.create", resource=new_agent.get("slug"))
        return _decorate(new_agent, user)
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=400)


@router.get("/pending")
async def pending_custom_agents(user: Principal = Depends(get_current_user)):
    """Agents waiting to be shared with everyone (needs the publish permission)."""
    if not can_approve(user):
        return JSONResponse({"error": "missing permission: custom_agents.publish"}, status_code=403)
    out = []
    for a in db_list_pending_agents():
        _decorate(a, user)
        a["work_dir"] = ""
        out.append(a)
    return {"agents": out}


@router.post("/{agent_id}/approve")
async def approve_custom_agent(agent_id: int, user: Principal = Depends(get_current_user)):
    return _review(agent_id, user, True)


@router.post("/{agent_id}/reject")
async def reject_custom_agent(agent_id: int, user: Principal = Depends(get_current_user)):
    return _review(agent_id, user, False)


def _review(agent_id: int, user: Principal, approved: bool):
    if not can_approve(user):
        return JSONResponse({"error": "missing permission: custom_agents.publish"}, status_code=403)
    agent = db_set_share_status(agent_id, approved)
    if not agent:
        return JSONResponse({"error": "That agent is not waiting for approval"}, status_code=404)
    audit_log(user, action="custom_agent.approve" if approved else "custom_agent.reject", resource=agent.get("slug"))
    return _decorate(agent, user)


@router.get("/tools/available")
async def available_tools(user: Principal = Depends(get_current_user)):
    """Available agent tools grouped by category for the agent builder."""
    from core.request_context import set_current_user
    set_current_user(user.id)

    tool_meta = {
        "read_file": {"label": "Read File", "cat": "Files & Code", "desc": "Safely read workspace files."},
        "write_file": {"label": "Write File", "cat": "Files & Code", "desc": "Create new files in the workspace."},
        "append_file": {"label": "Append to File", "cat": "Files & Code", "desc": "Add sections to the end of a file (builds large files in pieces)."},
        "insert_at_line": {"label": "Insert at Line", "cat": "Files & Code", "desc": "Insert text at a line number."},
        "memory_list": {"label": "Memory: List", "cat": "Memory", "desc": "List the user's long-term memory files."},
        "memory_read": {"label": "Memory: Read", "cat": "Memory", "desc": "Read long-term memory files."},
        "memory_write": {"label": "Memory: Write", "cat": "Memory", "desc": "Rewrite a long-term memory file."},
        "memory_str_replace": {"label": "Memory: Edit Line", "cat": "Memory", "desc": "Change or remove one memory line."},
        "memory_append": {"label": "Memory: Add Fact", "cat": "Memory", "desc": "Save a fact the user stated."},
        "memory_delete": {"label": "Memory: Delete File", "cat": "Memory", "desc": "Delete a memory file when the user asks."},
        "edit_file": {"label": "Edit File", "cat": "Files & Code", "desc": "Apply targeted line or string replacements."},
        "list_files": {"label": "List Files", "cat": "Files & Code", "desc": "Glob and list files in workspace."},
        "project_overview": {"label": "Project Overview", "cat": "Files & Code", "desc": "Layout, languages and manifests of the project in one call."},
        "grep": {"label": "Grep Search", "cat": "Files & Code", "desc": "Fast regex search across workspace files."},
        "git_inspect": {"label": "Git: Inspect", "cat": "Files & Code", "desc": "Read-only git status, diff, log and branches."},
        "git_commit": {"label": "Git: Commit", "cat": "Files & Code", "desc": "Commit named files locally (asks first; never pushes)."},
        "git_branch": {"label": "Git: Branch", "cat": "Files & Code", "desc": "Create or switch branches (asks first; never deletes)."},
        "run_tests": {"label": "Run Tests", "cat": "Files & Code", "desc": "Run the project's tests (pytest, unittest, npm, go, cargo) and get a failure summary."},
        "find_symbol": {"label": "Find Symbol", "cat": "Files & Code", "desc": "Where a function, class or method is defined (Python/JS/TS)."},
        "find_references": {"label": "Find References", "cat": "Files & Code", "desc": "Every call site of a function or method."},
        "file_outline": {"label": "File Outline", "cat": "Files & Code", "desc": "Classes, functions and methods of one file, without bodies."},
        "run_python": {"label": "Run Python", "cat": "Files & Code", "desc": "Execute Python scripts inside the workspace sandbox."},
        "run_shell": {"label": "Run Shell / Terminal", "cat": "Files & Code", "desc": "Execute terminal commands (gated by permission)."},
        "list_diff": {"label": "Session Diff", "cat": "Files & Code", "desc": "View modified or created files during the session."},
        "revert": {"label": "Revert File", "cat": "Files & Code", "desc": "Undo the last edit(s) to a file."},
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
    err = _check_slug(data["slug"]) if data.get("slug") else None
    if err:
        return err
    if "is_public" in data:
        data["share"] = bool(data.pop("is_public"))
    data["can_approve"] = can_approve(user)
    if not db_get_custom_agent(agent_id):
        return JSONResponse({"error": "Agent not found"}, status_code=404)
    try:
        updated = db_update_custom_agent(agent_id, user.id, data)
    except CustomAgentSlugTaken as e:
        return JSONResponse({"error": f"slug '{e}' is already used by another agent"}, status_code=409)
    except ValueError as e:
        return JSONResponse({"error": str(e)}, status_code=400)
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
async def fork_custom_agent(agent_id: int, req: ForkReq, request: Request, user: Principal = Depends(get_current_user)):
    """Clone an agent or starter template into user's own custom agents."""
    device_id = req.device_id or request.headers.get("X-Device-Id", "")
    device_name = req.device_name or request.headers.get("X-Device-Name", "")
    try:
        forked = db_fork_custom_agent(agent_id, user.id, req.name, req.work_dir or "",
                                     device_id=device_id, device_name=device_name)
    except ValueError as e:
        return JSONResponse({"error": str(e)}, status_code=400)
    if not forked:
        return JSONResponse({"error": "Source agent not found"}, status_code=404)
    audit_log(user, action="custom_agent.fork", resource=forked.get("slug"))
    return _decorate(forked, user)

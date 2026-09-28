"""
routes/custom_agents.py - User-wise Custom Agents management endpoints.
Allows users to create, configure, list, update, and fork personalized agents.
"""

from typing import List, Optional
from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from core.auth import Principal, user_has_permission
from core.deps import get_current_user
from core.audit import audit_log
from core.auth_db import (
    db_list_custom_agents,
    db_get_custom_agent,
    db_create_custom_agent,
    db_update_custom_agent,
    db_delete_custom_agent,
    db_fork_custom_agent,
    slugify_custom_agent,
    CustomAgentSlugTaken,
)
from core.registry import registry

router = APIRouter(prefix="/custom-agents", tags=["custom_agents"])


class CustomAgentCreateReq(BaseModel):
    name: str = Field(..., min_length=1, max_length=120)
    slug: Optional[str] = Field(None, max_length=80)
    description: str = Field(..., max_length=1000)
    icon: Optional[str] = Field("🤖", max_length=16)
    system_prompt: str = Field(..., min_length=1, max_length=20000)
    tool_allowlist: List[str] = Field(default_factory=list)
    input_template: Optional[str] = Field("", max_length=5000)
    preferred_lane: Optional[str] = Field("auto", max_length=32)
    reasoning_effort: Optional[str] = Field("medium", max_length=16)
    temperature: Optional[float] = Field(0.4, ge=0.0, le=2.0)
    is_public: Optional[bool] = False


class CustomAgentUpdateReq(BaseModel):
    name: Optional[str] = Field(None, min_length=1, max_length=120)
    slug: Optional[str] = Field(None, max_length=80)
    description: Optional[str] = Field(None, max_length=1000)
    icon: Optional[str] = Field(None, max_length=16)
    system_prompt: Optional[str] = Field(None, min_length=1, max_length=20000)
    tool_allowlist: Optional[List[str]] = None
    input_template: Optional[str] = Field(None, max_length=5000)
    preferred_lane: Optional[str] = Field(None, max_length=32)
    reasoning_effort: Optional[str] = Field(None, max_length=16)
    temperature: Optional[float] = Field(None, ge=0.0, le=2.0)
    is_public: Optional[bool] = None


class ForkReq(BaseModel):
    name: Optional[str] = None


PUBLISH_PERMISSION = "custom_agents.publish"
# tools a custom agent loses when it runs as a spawn_agent sub-agent (core/subagent.py)
_SUBAGENT_DENIED = ("run_shell", "run_python", "generate_image", "generate_video", "spawn_agent")


def _reserved_slugs() -> set:
    """Names spawn_agent resolves before custom agents -- a custom slug equal to
    one of these would be silently shadowed (core/roles.resolve_role)."""
    from core.small_model import APP_CONFIG
    names = set(APP_CONFIG.get("roles", {}).keys())
    try:
        from core.agent_library import load_agent_profiles
        names |= set(load_agent_profiles())
    except Exception:
        pass
    return {n.lower() for n in names}


def _decorate(agent: dict, user: Principal) -> dict:
    uid = agent.get("user_id")
    agent["owned"] = uid == user.id
    agent["scope"] = "mine" if uid == user.id else ("template" if uid is None else "shared")
    agent["can_edit"] = agent["owned"] or bool(user.is_super_admin)
    agent["subagent_warnings"] = [t for t in (agent.get("tool_allowlist") or []) if t in _SUBAGENT_DENIED]
    return agent


def _check_publish(user: Principal, is_public) -> Optional[JSONResponse]:
    if is_public and not user_has_permission(user, PUBLISH_PERMISSION):
        return JSONResponse({"error": f"missing permission: {PUBLISH_PERMISSION}"}, status_code=403)
    return None


def _check_slug(slug: Optional[str], name: Optional[str] = None) -> Optional[JSONResponse]:
    s = slugify_custom_agent(slug or name or "")
    if s and s in _reserved_slugs():
        return JSONResponse({"error": f"'{s}' is a built-in role name; choose another slug"}, status_code=409)
    return None


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

    # Standard categorized metadata for core and registered tools
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
    # browser_* / mobile_* (core/browser_tools.py, core/device_tools.py)
    from core.browser_tools import BROWSER_TOOLS
    from core.device_tools import DEVICE_TOOLS
    for n, _fn, schema in BROWSER_TOOLS + DEVICE_TOOLS:
        cat = "Browser Testing" if n.startswith("browser_") else "Mobile Testing"
        tool_meta.setdefault(n, {"label": n.split("_", 1)[1].replace("_", " ").title(), "cat": cat,
                                 "desc": schema["function"]["description"].split(". ")[0][:140]})

    # Discover registered tools (including user's personal MCP tools)
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


def apply_input_template(messages: list, agent: Optional[dict]) -> None:
    """Wrap the first user turn in the agent's input_template ("{input}" placeholder).
    Follow-up turns are sent as typed. Mutates messages in place."""
    tpl = (agent or {}).get("input_template") or ""
    if "{input}" not in tpl:
        return
    users = [m for m in messages if isinstance(m, dict) and m.get("role") == "user"]
    if len(users) != 1 or not isinstance(users[0].get("content"), str):
        return
    text = users[0]["content"]
    if text.strip():
        users[0]["content"] = tpl.replace("{input}", text)

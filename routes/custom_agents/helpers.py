from typing import Optional
from fastapi.responses import JSONResponse
from core.auth import Principal, user_has_permission
from core.auth_db import slugify_custom_agent
from .models import PUBLISH_PERMISSION, _SUBAGENT_DENIED


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
    if not agent["owned"]:
        agent["work_dir"] = ""       # a path on someone else's machine means nothing here and is theirs to keep
    agent["can_edit"] = agent["owned"] or bool(user.is_super_admin)
    agent["subagent_warnings"] = [t for t in (agent.get("tool_allowlist") or []) if t in _SUBAGENT_DENIED]
    return agent


def can_approve(user: Principal) -> bool:
    """Whoever holds the publish permission reviews shared agents (and may share without a review)."""
    return bool(user.is_super_admin) or user_has_permission(user, PUBLISH_PERMISSION)


def _check_slug(slug: Optional[str], name: Optional[str] = None) -> Optional[JSONResponse]:
    import sys
    s = slugify_custom_agent(slug or name or "")
    fn = getattr(sys.modules.get("routes.custom_agents"), "_reserved_slugs", _reserved_slugs)
    if s and s in fn():
        return JSONResponse({"error": f"'{s}' is a built-in role name; choose another slug"}, status_code=409)
    return None


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

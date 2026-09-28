"""Named sub-agent roles: lane + tool subset + system-prompt fragment presets.

Roles live in config/roles.json (loaded via core.small_model.APP_CONFIG, same
hot-reload convention as "router"/"agent"/"capabilities"; a legacy "roles" block
still inside config/app.json overrides it) so adding or tuning a role needs no
code change or restart.
"""

from typing import Optional


def resolve_role(name: Optional[str]) -> dict:
    """role name -> {lane, max_steps, tools, system_prompt}, or {} if None/unknown."""
    if not name:
        return {}
    from .small_model import APP_CONFIG
    roles = APP_CONFIG.get("roles", {})
    cfg = roles.get(name)
    if isinstance(cfg, dict):
        return dict(cfg)

    # the caller's own custom agents, then system templates (never another
    # user's public agent -- see auth_db.db_get_custom_agent_by_slug)
    try:
        from .request_context import get_current_user_id
        from .auth_db import db_get_custom_agent_by_slug
        agent = db_get_custom_agent_by_slug(name, get_current_user_id())
        if agent:
            lane = agent.get("preferred_lane") or "auto"
            return {
                # "auto"/"cloud" -> the Settings "Sub-agents" job mapping (core/subagent.py)
                "lane": lane if lane in ("main", "executor") else None,
                "tools": agent.get("tool_allowlist") or None,
                "system_prompt": agent.get("system_prompt", ""),
                "max_steps": 30,
            }
    except Exception:
        pass

    # fall back to an admin-allowed Agent Library profile (.agents/agents/<name>.md)
    from .agent_library import profile_role
    return profile_role(name)


def known_role_names() -> list:
    """Built-in roles.json names + allowed Agent Library profiles + user custom agents."""
    from .small_model import APP_CONFIG
    from .agent_library import load_agent_profiles
    names = list(APP_CONFIG.get("roles", {}).keys())
    names += [n for n in load_agent_profiles() if n not in names]
    for ca in _own_and_template_agents():
        slug = ca.get("slug")
        if slug and slug not in names:
            names.append(slug)
    return names


def _own_and_template_agents() -> list:
    try:
        from .request_context import get_current_user_id
        from .auth_db import db_list_custom_agents
        uid = get_current_user_id()
        return [a for a in db_list_custom_agents(uid)
                if a.get("user_id") is None or (uid is not None and a.get("user_id") == uid)]
    except Exception:
        return []


def custom_agents_prompt_fragment() -> str:
    """spawn_agent roles backed by the caller's custom agents / starter templates."""
    agents = _own_and_template_agents()
    if not agents:
        return ""
    lines = ["", "Custom agents (spawn_agent roles; pass the slug as role=<slug>):"]
    for a in agents[:30]:
        lines.append(f"- {a['slug']}: {(a.get('description') or a.get('name') or '')[:140]}")
    return "\n".join(lines)

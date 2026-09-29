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

    # A skill name is a valid role. Skills and roles are advertised in the same
    # system prompt, and models routinely pass one as the other ("role=
    # webapp-testing" is a skill, not a role). Treat it as a role whose system
    # prompt is the skill body, with no lane pin so normal selection applies.
    skill = _resolve_skill(name)
    if skill:
        return skill

    # fall back to an admin-allowed Agent Library profile (.agents/agents/<name>.md)
    from .agent_library import profile_role
    return profile_role(name)


# a skill body is instructions for a whole task, not a persona; keep it bounded so
# it cannot crowd the sub-agent's window (cf. Claude Code's 5k-per-skill cap)
MAX_ROLE_SKILL_CHARS = 6000


def _resolve_skill(name: str) -> dict:
    """An installed skill as a spawn_agent role, or {} if the name isn't one."""
    try:
        from .skills import load_skills
        skills = load_skills()
    except Exception:
        return {}
    want = (name or "").strip().lower()
    if not want or want not in skills:
        return {}
    sk = skills[want]
    body = sk.get("body") or ""
    if len(body) > MAX_ROLE_SKILL_CHARS:
        body = body[:MAX_ROLE_SKILL_CHARS] + "\n... (truncated)"
    return {
        "lane": None,               # no pin: the Sub-agents job mapping decides
        "tools": None,              # the skill's own steps pick what they need
        "system_prompt": (
            f"You are running the '{sk['name']}' skill. "
            f"{sk.get('description') or ''}\n\nFollow these instructions:\n\n{body}"
        ),
        "max_steps": 30,
        "is_skill": True,
    }


def known_role_names() -> list:
    """Built-in roles.json names + allowed Agent Library profiles + user custom
    agents + installed skills (a skill name is accepted as a role)."""
    from .small_model import APP_CONFIG
    from .agent_library import load_agent_profiles
    names = list(APP_CONFIG.get("roles", {}).keys())
    names += [n for n in load_agent_profiles() if n not in names]
    for ca in _own_and_template_agents():
        slug = ca.get("slug")
        if slug and slug not in names:
            names.append(slug)
    try:
        from .skills import load_skills
        names += [n for n in load_skills() if n not in names]
    except Exception:
        pass
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

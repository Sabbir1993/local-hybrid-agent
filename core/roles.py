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
    # fall back to an admin-allowed Agent Library profile (.agents/agents/<name>.md)
    from .agent_library import profile_role
    return profile_role(name)


def known_role_names() -> list:
    """Built-in roles.json names + allowed Agent Library profiles."""
    from .small_model import APP_CONFIG
    from .agent_library import load_agent_profiles
    names = list(APP_CONFIG.get("roles", {}).keys())
    names += [n for n in load_agent_profiles() if n not in names]
    return names

import fnmatch
from typing import Optional
from .constants import BUILTIN_COMMANDS, BUILTIN_ROLES


def library_cfg() -> dict:
    from ..small_model import APP_CONFIG
    cfg = APP_CONFIG.get("agent_library")
    return cfg if isinstance(cfg, dict) else {}


def _skill_names() -> set:
    """Existing skills (.agents/skills) keep their /name over a same-named command."""
    from ..skills import load_skills
    return {n.lower() for k, s in load_skills().items() for n in (k, s["name"])}


def _builtin_roles() -> set:
    """config/roles.json names (always win over a same-named profile)."""
    from ..small_model import APP_CONFIG
    return BUILTIN_ROLES | {str(k).lower() for k in (APP_CONFIG.get("roles") or {})}


def _match(name: str, patterns) -> bool:
    return any(fnmatch.fnmatch(name.lower(), str(p).lower()) for p in (patterns or []))


def item_state(kind: str, name: str, skill_names: Optional[set] = None) -> str:
    """'allowed' | 'denied' | 'shadowed' for an agent ('agents') or command ('commands')."""
    if kind == "commands" and (name.lower() in BUILTIN_COMMANDS
                               or name.lower() in (skill_names if skill_names is not None else _skill_names())):
        return "shadowed"
    if kind == "agents" and name.lower() in _builtin_roles():
        return "shadowed"
    cfg = library_cfg()
    sect = cfg.get(kind) if isinstance(cfg.get(kind), dict) else {}
    if _match(name, sect.get("deny")):
        return "denied"
    if _match(name, sect.get("allow")):
        return "allowed"
    return "allowed" if cfg.get("default_policy") == "allow" else "denied"


def library_enabled() -> bool:
    return bool(library_cfg().get("enabled", False))

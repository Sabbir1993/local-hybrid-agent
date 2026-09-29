from typing import Optional
from .constants import (
    COMMAND_PREAMBLE,
    DEFAULT_MULTI_LANES,
    PROFILE_PREAMBLE,
)
from .loader import (
    all_agent_profiles,
    all_prompt_commands,
    load_agent_profiles,
    load_prompt_commands,
)
from .policy import (
    _skill_names,
    item_state,
    library_cfg,
    library_enabled,
)


def profile_role(name: str) -> dict:
    """Profile -> role dict for core.roles.resolve_role ({} if unknown/not allowed).
    No 'lane' key on purpose: the app's own lane selection applies."""
    if not name:
        return {}
    want = name.lower()
    prof = next((p for n, p in load_agent_profiles().items() if n.lower() == want), None)
    if not prof:
        return {}
    return {
        "tools": list(prof["tools"]),
        "system_prompt": f"{PROFILE_PREAMBLE}\n\n{prof['body']}",
    }


def _shared_text_lanes() -> list:
    try:
        from ..lanes import kind_ok, registry
        return [{"name": n, "label": d["label"]} for n, d in registry().items()
                if d["owner"] == "shared" and kind_ok("chat", d["kind"])]
    except Exception:
        return [{"name": "main", "label": "Main brain"}, {"name": "executor", "label": "Fast helper"}]


def expand_command(name: str, args: str = "") -> Optional[str]:
    """Allowed command -> ready-to-run agent prompt, or None."""
    from ..project_context import sanitize
    cmd = load_prompt_commands().get(name)
    if not cmd:
        return None
    args = (args or "").strip()
    body = cmd["body"]
    if cmd.get("source") == "native":
        from ..lanes import kind_ok, registry
        reg = registry()
        cfg_lanes = library_cfg().get("multi_lanes") or {}
        lanes = {k: (cfg_lanes.get(k) if cfg_lanes.get(k) in reg
                     and kind_ok("chat", reg[cfg_lanes[k]]["kind"]) else v)
                 for k, v in DEFAULT_MULTI_LANES.items()}
        body = body.replace("$BACKEND_LANE", lanes["backend"]).replace("$FRONTEND_LANE", lanes["frontend"])
    if "$ARGUMENTS" in body:
        body = body.replace("$ARGUMENTS", args or "(none given)")
    elif args:
        body += f"\n\nArguments from the user: {args}"
    return sanitize(f"{COMMAND_PREAMBLE.format(name=cmd['name'])}\n\n{body}")


def agent_library_prompt_fragment() -> str:
    """Short listing of allowed profiles for the main agent system prompt."""
    profiles = load_agent_profiles()
    if not profiles:
        return ""
    lines = ["", "Agent Library (extra spawn_agent roles; pass the name as role=<name>):"]
    for p in profiles.values():
        lines.append(f"- {p['name']}: {p['description'][:140]}")
    return "\n".join(lines)


def library_status() -> dict:
    """Everything the admin UI needs: every file with its state and warnings."""
    cfg = library_cfg()
    skills = _skill_names()

    def rows(kind, items):
        return [{"name": n, "description": it["description"], "state": item_state(kind, n, skills),
                 "source": it.get("source", "library"), "warnings": it["warnings"]}
                for n, it in sorted(items.items())]

    return {
        "enabled": library_enabled(),
        "default_policy": cfg.get("default_policy", "deny"),
        "agents": rows("agents", all_agent_profiles()),
        "commands": rows("commands", all_prompt_commands()),
        "config": {k: cfg.get(k, {"allow": [], "deny": []}) for k in ("agents", "commands")},
        "multi_lanes": {**DEFAULT_MULTI_LANES, **(cfg.get("multi_lanes") or {})},
        # shared text models (core/lanes.py) that can play an analyst role
        "lane_options": _shared_text_lanes(),
    }

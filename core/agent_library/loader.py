from pathlib import Path
from typing import Optional
from .constants import (
    AGENTS_DIR,
    COMMANDS_DIR,
    MAX_COMMAND_BODY_CHARS,
    MAX_PROFILE_BODY_CHARS,
    NATIVE_COMMANDS_DIR,
    _DEFENSE_RX,
)
from .parser import (
    _compat,
    _map_tools,
    _parse_frontmatter,
    _parse_tools,
    _read,
)
from .policy import (
    _skill_names,
    item_state,
    library_enabled,
)


def _scan(dir_: Path) -> list:
    if not dir_.is_dir():
        return []
    return sorted(p for p in dir_.glob("*.md") if p.is_file())


def _load_profile(path: Path) -> Optional[dict]:
    text = _read(path)
    if text is None:
        return None
    meta, body = _parse_frontmatter(text)
    body = _DEFENSE_RX.sub("", body).strip()
    if len(body) > MAX_PROFILE_BODY_CHARS:
        body = body[:MAX_PROFILE_BODY_CHARS] + "\n... (truncated)"
    name = meta.get("name") or path.stem
    return {
        "name": name,
        "description": (meta.get("description") or "")[:200],
        "tools": _map_tools(_parse_tools(meta.get("tools", ""))),
        "body": body,
        "path": str(path),
        "warnings": _compat(text),
    }


def _load_command(path: Path, source: str = "library") -> Optional[dict]:
    text = _read(path)
    if text is None:
        return None
    meta, body = _parse_frontmatter(text)
    body = body.strip()
    if len(body) > MAX_COMMAND_BODY_CHARS:
        body = body[:MAX_COMMAND_BODY_CHARS] + "\n... (truncated)"
    return {
        "name": path.stem,
        "description": (meta.get("description") or "")[:200],
        "argument_hint": meta.get("argument-hint", ""),
        "body": body,
        "path": str(path),
        "source": source,
        "warnings": _compat(text) if source == "library" else [],
    }


def all_agent_profiles() -> dict:
    """Every profile file, regardless of policy (admin listing)."""
    out = {}
    for p in _scan(AGENTS_DIR):
        prof = _load_profile(p)
        if prof:
            out[prof["name"]] = prof
    return out


def all_prompt_commands() -> dict:
    out = {}
    for dir_, source in ((COMMANDS_DIR, "library"), (NATIVE_COMMANDS_DIR, "native")):
        for p in _scan(dir_):
            cmd = _load_command(p, source)
            if cmd:
                out[cmd["name"]] = cmd
    return out


def load_agent_profiles() -> dict:
    """Enabled + allowed profiles only."""
    if not library_enabled():
        return {}
    return {n: p for n, p in all_agent_profiles().items() if item_state("agents", n) == "allowed"}


def load_prompt_commands() -> dict:
    if not library_enabled():
        return {}
    skills = _skill_names()
    return {n: c for n, c in all_prompt_commands().items() if item_state("commands", n, skills) == "allowed"}

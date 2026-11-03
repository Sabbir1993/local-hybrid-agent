"""
core/lanes/creative.py - pick a dedicated model for a creative build job (today: "html3d", 3D / WebGL pages).

The admin sets the lane up in Settings -> Models (Add a model, then "Jobs & backup" maps it to the job). When the job is not
mapped to a lane, the lane is not configured, its model file is missing or it will not load, pick() returns None and the caller
keeps the main model - a dedicated lane is an upgrade, never a requirement.
"""

import sys
from dataclasses import dataclass
from typing import Any, Optional

import re

from .routing import targets
from .target import Target

# Jobs whose lane is found by name when the admin added the model but has not mapped the job yet ("Qwen3 3D Coder" -> html3d).
# An explicit mapping always wins, including one to main.
NAME_HINTS = {"html3d": re.compile(r"(?<![a-z0-9])3\s?d(?![a-z0-9])", re.I)}

DEFAULT_MAX_TOKENS = 16000      # a small coder model can loop for minutes on one page; one turn never needs more


@dataclass
class CreativeLane:
    lane: str            # lane name, shown in the chat badge
    client: Any          # llama-server client (loaded)
    name: str            # model name for the badge and the Monitor
    max_tokens: int


async def pick(job: str, user_id: Optional[int] = None) -> Optional[CreativeLane]:
    """The first local lane of `job`'s route that is not the main model and loads; None means "use main"."""
    from .. import cloud
    from ..small_model import APP_CONFIG
    route = list(targets(job, user_id, force_local=True))
    hint = NAME_HINTS.get(job)
    if hint and not (cloud.role_map(user_id) or {}).get(job):
        named = [Target(n) for n, sp in (APP_CONFIG.get("small_models") or {}).items()
                 if isinstance(sp, dict) and str(sp.get("kind") or "chat") == "chat" and sp.get("model")
                 and hint.search(f"{n} {sp.get('label') or ''}")]
        route = named + route
    for t in route:
        if t.lane == "main":
            return None                              # the route ended at main: nothing dedicated is set up
        if t.is_cloud or not t.available():
            continue
        try:
            client = await t.client()                # loads the model on demand
        except Exception as e:
            print(f"[lanes] {job}: lane '{t.lane}' did not load, trying the next one: {type(e).__name__}: {e}", file=sys.stderr)
            continue
        spec = (APP_CONFIG.get("small_models") or {}).get(t.lane)
        spec = spec if isinstance(spec, dict) else {}
        try:
            cap = int(spec.get("max_tokens") or DEFAULT_MAX_TOKENS)
        except (TypeError, ValueError):
            cap = DEFAULT_MAX_TOKENS
        return CreativeLane(t.lane, client, t.model_name(), max(256, cap))
    return None

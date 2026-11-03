"""
core/lanes/creative.py - pick a dedicated model for a creative build job (today: "html3d", 3D / WebGL pages).

The admin sets the lane up in Settings -> Models (Add a model, then "Jobs & backup" maps it to the job). When the job is not
mapped to a lane, the lane is not configured, its model file is missing or it will not load, pick() returns None and the caller
keeps the main model - a dedicated lane is an upgrade, never a requirement.
"""

import sys
from dataclasses import dataclass
from typing import Any, Optional

from .routing import targets

DEFAULT_MAX_TOKENS = 16000      # a small coder model can loop for minutes on one page; one turn never needs more


@dataclass
class CreativeLane:
    lane: str            # lane name, shown in the chat badge
    client: Any          # llama-server client (loaded)
    name: str            # model name for the badge and the Monitor
    max_tokens: int


async def pick(job: str, user_id: Optional[int] = None) -> Optional[CreativeLane]:
    """The first local lane of `job`'s route that is not the main model and loads; None means "use main"."""
    from ..small_model import APP_CONFIG
    for t in targets(job, user_id, force_local=True):
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

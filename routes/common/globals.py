"""
routes/common/globals.py - Module-level variables and prompt/context helpers.
"""

from datetime import datetime
from pathlib import Path
from typing import Optional


# Global CLI runtime overrides
initial_profile_path: Optional[Path] = None
models_dir: Optional[Path] = None
curStatus_model_hint: Optional[str] = None


def current_date_prompt() -> str:
    """System-prompt line anchoring "now". Without it a local model assumes its
    training-cutoff year and searches the web for stale years."""
    now = datetime.now().astimezone()
    return (f"CURRENT DATE: {now:%Y-%m-%d} ({now:%A}). Treat this as 'now' - your training data ends "
            f"earlier. For recent or current data, search with {now.year} (and {now.year - 1} for the "
            f"latest full year); never assume your training-cutoff year is the current year.")


def main_ctx_tokens(cloud_main=None) -> int:
    """Context window of one main-lane conversation. llama-server divides -c
    across -np slots unless the KV pool is unified (then --kv-unified-per-slot,
    if set, is the cap)."""
    from core.process import per_slot_cap
    from core.state import state
    if cloud_main:
        return getattr(cloud_main, "ctx", 32768) or 32768
    p = state.profile if isinstance(state.profile, dict) else {}
    ctx = int(p.get("context_size") or 32768)
    n_slots = int(p.get("n_slots") or 1)
    if p.get("kv_unified"):
        return per_slot_cap(p) or ctx
    return ctx // n_slots if n_slots > 1 else ctx


# Fallback prompt window when a lane reports neither a probe nor a configured ctx.
SUBAGENT_FALLBACK_CTX = 16384

async def _subagent_window(target) -> int:
    """Prompt window (tokens) for the lane a sub-agent actually landed on.

    Mirrors routes/agent.py::_lane_window: prefer the window the running
    llama-server advertises for a slot (`/slots` -> `n_ctx`, the number it quotes
    when it refuses an oversized prompt), else the config-derived window. Both
    know about -np / -kvu / --kv-unified-per-slot; a hardcoded constant does not,
    and cannot know that a cloud lane is far larger than the local executor.
    """
    from .. import context_budget
    lane = getattr(target, "lane", "executor")
    cm = getattr(target, "cm", None)
    if cm is not None:
        return context_budget.window(lane, cfg_ctx=getattr(cm, "ctx", 0) or SUBAGENT_FALLBACK_CTX) \
            or SUBAGENT_FALLBACK_CTX
    inst = getattr(target, "inst", None)
    if inst is None and lane != "main":
        return SUBAGENT_FALLBACK_CTX
    if lane == "main":
        from routes import common
        from ..state import state
        win = (await context_budget.probe_window("main", getattr(state, "client", None))) \
            or common.main_ctx_tokens(None)
        return int(win or SUBAGENT_FALLBACK_CTX)
    cfg = (getattr(inst, "cfg", None) or {})
    n_slots = max(1, int(cfg.get("np") or 1))
    derived = context_budget.window(lane, cfg_ctx=cfg.get("ctx") or SUBAGENT_FALLBACK_CTX,
                                    n_slots=n_slots, kv_unified=n_slots > 1)
    probed = await context_budget.probe_window(lane, getattr(inst, "client", None))
    return int(probed or derived or SUBAGENT_FALLBACK_CTX)

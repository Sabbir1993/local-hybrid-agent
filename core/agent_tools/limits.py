"""Tunable limits for the agent's file tools and per-lane output caps (config: agent.*, context.*).

Every getter falls back to a default when the key is missing or invalid, so a stale config
file never breaks a run. The Settings page writes these keys through /control/agent-limits.
"""
from ..small_model import APP_CONFIG

# key -> (default, min, max)
AGENT_LIMITS = {
    "write_file_max_tokens": (4000, 500, 16000),
    "chunk_target_tokens": (3000, 500, 8000),
    "read_file_default_limit": (500, 20, 2000),
    "read_file_max_chars": (40000, 2000, 100000),
    "verify_max_retries": (3, 1, 10),
    "executor_max_tokens": (4096, 512, 16000),
    "main_max_tokens": (16000, 1024, 65536),
    "vision_max_tokens": (1024, 128, 8192),
    "plan_max_items": (20, 3, 40),
    "plan_item_max_chars": (300, 60, 1000),
    "plan_item_max_steps": (25, 5, 100),
    "python_prompt_nudge": (4, 3, 20),
}
CONTEXT_LIMITS = {
    "compaction_threshold": (0.70, 0.3, 0.95),
}
MEMORY_LIMITS = {
    "file_max_bytes": (8192, 1024, 65536),
    "max_files": (60, 5, 500),
}

CHARS_PER_TOKEN = 3.5


def _num(section: dict, key: str, spec: tuple):
    default, lo, hi = spec
    try:
        v = section.get(key, default)
        v = float(v) if isinstance(default, float) else int(v)
    except (TypeError, ValueError):
        return default
    return min(max(v, lo), hi)


def agent_limit(key: str) -> int:
    return _num(APP_CONFIG.get("agent") or {}, key, AGENT_LIMITS[key])


def context_limit(key: str) -> float:
    return _num(APP_CONFIG.get("context") or {}, key, CONTEXT_LIMITS[key])


def memory_limit(key: str) -> int:
    return _num(APP_CONFIG.get("memory") or {}, key, MEMORY_LIMITS[key])


def est_tokens(text: str) -> int:
    return int(len(text or "") / CHARS_PER_TOKEN) + 1


def executor_effort_ceiling() -> str:
    """Highest reasoning effort the executor lane may use (agent.executor_max_effort)."""
    v = str((APP_CONFIG.get("agent") or {}).get("executor_max_effort", "low")).strip().lower()
    return v if v in ("none", "low", "medium", "high", "extra") else "low"


def lane_output_cap(lane: str) -> int:
    """Hard ceiling on one generation for a lane (0 = no cap for unknown lanes)."""
    key = {"executor": "executor_max_tokens", "main": "main_max_tokens",
           "vision": "vision_max_tokens"}.get(lane or "")
    return agent_limit(key) if key else 0


def effective_max_tokens(requested, lane: str, cloud: bool = False) -> int:
    """The user's Settings value (<=0 means unlimited) bounded by the lane's cap. Cloud models are left as the
    user set them: each provider has its own output limit and rejects a value above it."""
    cap = 0 if cloud else lane_output_cap(lane)
    try:
        req = int(requested)
    except (TypeError, ValueError):
        req = -1
    if cap <= 0:
        return req
    return cap if req <= 0 else min(req, cap)

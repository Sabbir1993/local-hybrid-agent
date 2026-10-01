"""
core/supervision.py - tunable limits for llama-server process supervision.

These were module constants with no route to change them, and two of them are the knobs that
decide whether a big model loads at all:

  health_timeout_s   how long a load may take before it is called a failure. A 70B off a slow
                     NVMe can legitimately need longer than 120s, and there was no way to say so.
  vram_wall_free_mb  free VRAM on a target card below which a loading model is killed, to stop
                     WDDM spilling into shared RAM and hanging the desktop (Arc-specific).

Same shape as core/agent_tools/limits.py: key -> (default, min, max), read from the
"supervision" block of config/app.json, clamped on read so a hand-edited or stale config can
never produce a nonsensical value. Mirrors that module's contract exactly - every getter falls
back rather than raising.

The block is called "supervision", not "runtime", because app.json already has a top-level
"runtime" key holding the NAME of the llama.cpp build preset ("vulkan"/"cuda") - adding a
second "runtime" object silently replaced it and broke preset selection.

Written through POST /control/config (key names below) or by editing config/app.json. Changes
apply on the next model load, except watchdog_interval_s / max_restart_count which the running
watchdog picks up on its next tick.
"""

from .small_model import APP_CONFIG

MIB = 1024 * 1024

# key -> (default, min, max)
SUPERVISION_LIMITS = {
    "health_timeout_s": (120, 10, 3600),
    "watchdog_interval_s": (5, 1, 120),
    "max_restart_backoff_s": (60, 1, 600),
    # 0 disables the crash-loop breaker (retry forever, the pre-B2 behaviour)
    "max_restart_count": (5, 0, 100),
    "restart_reset_after_s": (300, 30, 86400),
    "vram_wall_free_mb": (256, 32, 8192),
}

# Kept so existing imports of the module constants keep working; these are the defaults the
# getters fall back to, not the effective values.
HEALTH_TIMEOUT_S = SUPERVISION_LIMITS["health_timeout_s"][0]
WATCHDOG_INTERVAL_S = SUPERVISION_LIMITS["watchdog_interval_s"][0]
MAX_RESTART_BACKOFF_S = SUPERVISION_LIMITS["max_restart_backoff_s"][0]
MAX_RESTART_COUNT = SUPERVISION_LIMITS["max_restart_count"][0]
RESTART_RESET_AFTER_S = SUPERVISION_LIMITS["restart_reset_after_s"][0]
VRAM_WALL_FREE_B = SUPERVISION_LIMITS["vram_wall_free_mb"][0] * MIB


def supervision_limit(key: str) -> int:
    default, lo, hi = SUPERVISION_LIMITS[key]
    try:
        v = int((APP_CONFIG.get("supervision") or {}).get(key, default))
    except (TypeError, ValueError):
        return default
    return max(lo, min(hi, v))


def health_timeout_s() -> int:
    return supervision_limit("health_timeout_s")


def watchdog_interval_s() -> int:
    return supervision_limit("watchdog_interval_s")


def max_restart_backoff_s() -> int:
    return supervision_limit("max_restart_backoff_s")


def max_restart_count() -> int:
    return supervision_limit("max_restart_count")


def restart_reset_after_s() -> int:
    return supervision_limit("restart_reset_after_s")


def vram_wall_free_b() -> int:
    """The VRAM-wall threshold in BYTES, which is what core/vram/devices.py compares against."""
    return supervision_limit("vram_wall_free_mb") * MIB


def view() -> dict:
    """Current effective values plus the bounds, for the Settings UI."""
    return {**{k: supervision_limit(k) for k in SUPERVISION_LIMITS},
            "limits": {k: [lo, hi] for k, (_d, lo, hi) in SUPERVISION_LIMITS.items()},
            "defaults": {k: d for k, (d, _lo, _hi) in SUPERVISION_LIMITS.items()}}
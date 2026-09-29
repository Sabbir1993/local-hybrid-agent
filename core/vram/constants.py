import json
from ..config import CONFIG_FILE

GB = 1024 ** 3
MIB = 1024 * 1024

# Bytes per KV element for each kv_cache_type (quant types incl. block scale).
_KV_BYTES_PER_ELEM = {
    "f16": 2.0, "bf16": 2.0, "f32": 4.0,
    "q8_0": 34 / 32, "q5_0": 22 / 32, "q4_0": 18 / 32
}

_DEFAULT_HEADROOM_GB = 1.0
_PREFLIGHT_MODES = ("block", "warn", "off")

# If free VRAM drops below this on any target device while llama-server is
# still loading, the allocation is about to spill into shared memory (WDDM
# thrash/hang) -> abort the load. Used by state._wait_healthy.
VRAM_WALL_FREE_B = 256 * MIB


class PreflightError(RuntimeError):
    """Raised when a launch won't fit in VRAM and preflight mode is 'block'."""

    def __init__(self, message: str, plan: dict):
        super().__init__(message)
        self.plan = plan


def _preflight_cfg() -> dict:
    cfg = {"mode": "block", "headroom_gb": _DEFAULT_HEADROOM_GB}
    try:
        d = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
        p = d.get("preflight", {})
        if isinstance(p, dict):
            if p.get("mode") in _PREFLIGHT_MODES:
                cfg["mode"] = p["mode"]
            try:
                cfg["headroom_gb"] = max(0.0, min(4.0, float(
                    p.get("headroom_gb", cfg["headroom_gb"]))))
            except (TypeError, ValueError):
                pass
    except Exception:
        pass
    return cfg


def _fmt_gb(b) -> str:
    return f"{b / GB:.1f} GB"


def preflight_mode() -> str:
    return _preflight_cfg()["mode"]

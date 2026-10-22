from ..small_model import APP_CONFIG

REPORT_DIR = ".agent/webcheck"
MAX_RESULT_CHARS = 24000


def _limit(key: str, default: int) -> int:
    cfg = APP_CONFIG.get("webcheck") if isinstance(APP_CONFIG.get("webcheck"), dict) else {}
    try:
        return max(1, int(cfg.get(key, default)))
    except (TypeError, ValueError):
        return default


def max_pages() -> int:
    return _limit("max_pages", 10)


def max_flows() -> int:
    return _limit("max_flows", 5)


def max_steps() -> int:
    return _limit("max_steps", 30)

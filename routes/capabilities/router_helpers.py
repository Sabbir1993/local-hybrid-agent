"""Router settings helpers for capabilities routes."""

import json

from fastapi.responses import JSONResponse

from core import router_policy, route_log
from core.config import CONFIG_FILE, write_app_config
from core.small_model import APP_CONFIG, reset_router_failures, router_available, router_engine_name

_LIST_KEYS = ("creation_keywords", "action_keywords", "refusal_phrases", "greetings")
_MAIN_CATEGORIES = tuple(c for c in router_policy.CATEGORIES if c != "greeting")


def _validate_router_changes(changes: dict):
    """-> (clean_changes, error_str)."""
    out = {}
    for k, v in changes.items():
        if k in _LIST_KEYS:
            vals = sorted({str(x).strip().lower() for x in (v or []) if str(x).strip()})
            if len(vals) > 80 or any(len(x) > 40 for x in vals):
                return None, f"{k}: at most 80 entries of 40 chars each"
            out[k] = vals
        elif k == "repeat_streak_limit":
            try:
                n = int(v)
            except (TypeError, ValueError):
                return None, "repeat_streak_limit must be an integer"
            if not 1 <= n <= 5:
                return None, "repeat_streak_limit must be between 1 and 5"
            out[k] = n
        elif k == "start_on_main_categories":
            vals = sorted({str(x).strip().lower() for x in (v or [])})
            bad = [x for x in vals if x not in _MAIN_CATEGORIES]
            if bad:
                return None, f"unknown categories: {', '.join(bad)} (allowed: {', '.join(_MAIN_CATEGORIES)})"
            out[k] = vals
        elif k == "confidence_threshold":
            try:
                t = round(float(v), 2)
            except (TypeError, ValueError):
                return None, "confidence_threshold must be a number"
            if not 0.5 <= t <= 0.99:
                return None, "confidence_threshold must be between 0.5 and 0.99"
            out[k] = t
        else:
            return None, f"unknown router setting: {k}"
    return out, ""


def _router_settings() -> dict:
    cur = router_policy.rcfg()
    settings = {k: cur[k] for k in router_policy.DEFAULTS}
    settings["confidence_threshold"] = (APP_CONFIG.get("router") or {}).get("confidence_threshold", 0.7)
    return settings


def _write_router(changes: dict):
    """Persist + live-apply router changes. -> (old_values, error_response|None)."""
    try:
        cfg = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
    except Exception as e:
        return None, JSONResponse({"error": f"config/app.json unreadable: {e}"}, status_code=500)
    live = _router_settings()
    old = {k: live.get(k) for k in changes}
    cfg.setdefault("router", {}).update(changes)
    try:
        write_app_config(cfg, CONFIG_FILE)
    except Exception as e:
        return None, JSONResponse({"error": f"config/app.json write failed: {e}"}, status_code=500)
    APP_CONFIG.setdefault("router", {}).update(changes)
    reset_router_failures()
    return old, None


def _router_view() -> dict:
    return {
        "settings": _router_settings(),
        "categories": list(router_policy.CATEGORIES),
        "engine": router_engine_name(),
        "router_available": router_available(),
        "stats_7d": route_log.stats(7),
        "stats_30d": route_log.stats(30),
        "suggestions": route_log.list_suggestions("pending"),
        "history": [s for s in route_log.list_suggestions(limit=20) if s["status"] != "pending"],
    }

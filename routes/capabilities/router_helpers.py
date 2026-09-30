"""Router settings helpers for capabilities routes."""

import json

from fastapi.responses import JSONResponse

from core import router_policy, route_log
from core.config import CONFIG_FILE, write_app_config
from core.small_model import APP_CONFIG, reset_router_failures, router_available, router_engine_name

from core import router_config
from core import lane_health

_LIST_KEYS = router_config.LIST_KEYS
_MAIN_CATEGORIES = router_config.MAIN_CATEGORIES


def _validate_router_changes(changes: dict):
    """-> (clean_changes, error_str)."""
    return router_config.validate(changes)


def _router_settings() -> dict:
    return router_config.settings()


def _write_router(changes: dict):
    """Persist + live-apply router changes. -> (old_values, error_response|None)."""
    old, err = router_config.write(changes)
    if err:
        return None, JSONResponse({"error": err}, status_code=500)
    return old, None


def _router_view() -> dict:
    lane_health.ensure_hydrated()
    return {
        "settings": _router_settings(),
        "categories": list(router_policy.CATEGORIES),
        "engine": router_engine_name(),
        "router_available": router_available(),
        "stats_7d": route_log.stats(7),
        "stats_30d": route_log.stats(30),
        "suggestions": route_log.list_suggestions("pending"),
        "history": [s for s in route_log.list_suggestions(limit=20) if s["status"] != "pending"],
        # what recent turns say about each lane, and the tuner's own applied changes
        "lane_health": lane_health.health.snapshot(),
        "applied": route_log.list_applied(limit=15),
    }

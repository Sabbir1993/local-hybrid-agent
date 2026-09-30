"""Validating and persisting changes to the routing rules (config/app.json "router").

Lives in core so both the admin API (routes/capabilities) and the tuner's
auto-apply path go through the same checks and the same write.
"""

import json
from typing import Optional

from . import router_policy
from .config import CONFIG_FILE, write_app_config
from .small_model import APP_CONFIG, reset_router_failures

LIST_KEYS = ("creation_keywords", "action_keywords", "refusal_phrases", "greetings")
MAIN_CATEGORIES = tuple(c for c in router_policy.CATEGORIES if c != "greeting")
BOOL_KEYS = ("tool_choice_required", "executor_fresh_context", "finish_tool", "adaptive", "auto_apply",
             "tool_shortcut")


def _num(name, v, lo, hi, cast):
    try:
        n = cast(v)
    except (TypeError, ValueError):
        return None, f"{name} must be a number"
    if not lo <= n <= hi:
        return None, f"{name} must be between {lo} and {hi}"
    return n, ""


def validate(changes: dict) -> tuple:
    """-> (clean_changes, error_str)."""
    out = {}
    for k, v in changes.items():
        if k in LIST_KEYS:
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
            bad = [x for x in vals if x not in MAIN_CATEGORIES]
            if bad:
                return None, f"unknown categories: {', '.join(bad)} (allowed: {', '.join(MAIN_CATEGORIES)})"
            out[k] = vals
        elif k == "confidence_threshold":
            n, err = _num(k, v, 0.5, 0.99, float)
            if err:
                return None, err
            out[k] = round(n, 2)
        elif k == "min_executor_success":
            n, err = _num(k, v, 0.1, 0.99, float)
            if err:
                return None, err
            out[k] = round(n, 2)
        elif k == "adaptive_min_samples":
            n, err = _num(k, v, 3, 200, int)
            if err:
                return None, err
            out[k] = n
        elif k == "adaptive_cooldown_s":
            n, err = _num(k, v, 30, 86400, int)
            if err:
                return None, err
            out[k] = n
        elif k == "classifier":
            if not isinstance(v, dict):
                return None, "classifier must be an object"
            clean = {}
            for ck, cv in v.items():
                if ck == "enabled":
                    if not isinstance(cv, bool):
                        return None, "classifier.enabled must be true or false"
                    clean[ck] = cv
                elif ck == "mode":
                    if cv not in ("shadow", "active"):
                        return None, "classifier.mode must be shadow or active"
                    clean[ck] = cv
                elif ck == "timeout_s":
                    n, err = _num("classifier.timeout_s", cv, 0.2, 10, float)
                    if err:
                        return None, err
                    clean[ck] = round(n, 2)
                else:
                    return None, f"unknown classifier setting: {ck}"
            out[k] = clean
        elif k in BOOL_KEYS:
            if not isinstance(v, bool):
                return None, f"{k} must be true or false"
            out[k] = v
        else:
            return None, f"unknown router setting: {k}"
    return out, ""


def settings() -> dict:
    """The live router settings (what rcfg() resolves plus the router-only knobs)."""
    cur = router_policy.rcfg()
    out = {k: cur[k] for k in router_policy.DEFAULTS}
    router = APP_CONFIG.get("router") or {}
    out["confidence_threshold"] = router.get("confidence_threshold", 0.7)
    out["tool_shortcut"] = bool(router.get("tool_shortcut", False))
    out["auto_apply"] = bool(router.get("auto_apply", False))
    from .small_model import classifier
    out["classifier"] = classifier.config()
    return out


def write(changes: dict) -> tuple:
    """Persist + live-apply changes. -> (old_values, error_str)."""
    try:
        cfg = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
    except Exception as e:
        return None, f"config/app.json unreadable: {e}"
    live = settings()
    old = {k: live.get(k) for k in changes}
    router = cfg.setdefault("router", {})
    for k, v in changes.items():
        if k == "classifier":                      # merge: thresholds etc. are kept
            router["classifier"] = {**(router.get("classifier") or {}), **v}
        else:
            router[k] = v
    try:
        write_app_config(cfg, CONFIG_FILE)
    except Exception as e:
        return None, f"config/app.json write failed: {e}"
    live_router = APP_CONFIG.setdefault("router", {})
    for k, v in changes.items():
        if k == "classifier":
            live_router["classifier"] = {**(live_router.get("classifier") or {}), **v}
        else:
            live_router[k] = v
    reset_router_failures()
    return old, ""

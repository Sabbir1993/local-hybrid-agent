import os
import sys
from copy import deepcopy
from pathlib import Path
from typing import Optional

from ..config import CLOUD_LANES
from ..request_context import get_current_user_id
from .store import (
    _KEY_REF,
    _get_config_file,
    _hydrate_keys,
    _key_id,
    _provider_file,
    _read_json,
    _write_providers,
    check_url_wrapper,
)

_CACHE: dict = {}
_WARNED: set = set()


def _resolve_user(user_id: Optional[int]) -> Optional[int]:
    return user_id if user_id is not None else get_current_user_id()


def _merge_sections(base_cfg: dict, override_cfg: dict) -> dict:
    out = {"provider": {}, "cloud": {}, "lanes": {}, "role_map": {}, "verification": {}}
    for src in (base_cfg, override_cfg):
        prov = src.get("provider")
        if isinstance(prov, dict):
            for name, cfg in prov.items():
                if not isinstance(cfg, dict):
                    continue
                merged = out["provider"].setdefault(name, {})
                merged.update(deepcopy(cfg))
                if isinstance(cfg.get("options"), dict):
                    merged["options"] = {**merged.get("options", {}),
                                         **deepcopy(cfg["options"])}
                if isinstance(cfg.get("models"), dict):
                    m = merged.setdefault("models", {})
                    for mid, mcfg in cfg["models"].items():
                        m[mid] = deepcopy(mcfg) if isinstance(mcfg, dict) else {}
        cl = src.get("cloud")
        if isinstance(cl, dict):
            out["cloud"].update(deepcopy(cl))
        vf = src.get("verification")
        if isinstance(vf, dict):
            out["verification"].update(deepcopy(vf))
        rm = src.get("role_map")
        if isinstance(rm, dict):
            out["role_map"].update({str(k): str(v) for k, v in rm.items() if v})
    ln = override_cfg.get("lanes")
    if isinstance(ln, dict):
        out["lanes"] = {str(k): deepcopy(v) for k, v in ln.items() if isinstance(v, dict)}
    return out


def _merged(user_id: Optional[int] = None) -> dict:
    uid = _resolve_user(user_id)
    cache_key = uid if uid is not None else "_shared"
    if cache_key not in _CACHE:
        override = _hydrate_keys(uid, _read_json(_provider_file(uid))) if uid is not None else {}
        _CACHE[cache_key] = _merge_sections(_read_json(_get_config_file()), override)
    return _CACHE[cache_key]


def reload(user_id: Optional[int] = None) -> None:
    """Drop cached config after a user's providers file / config/app.json changed."""
    global _CACHE
    if user_id is None:
        _CACHE = {}
    else:
        _CACHE.pop(user_id, None)
    _WARNED.clear()
    from .client import _CLIENTS
    _CLIENTS.clear()


def save_provider(user_id: int, name: str, data: dict) -> dict:
    """Create/update one provider in this user's config."""
    from .client import _BLOCKED_HEADERS
    name = str(name or "").strip()
    if not name:
        raise ValueError("provider name is required")
    cfg = _hydrate_keys(user_id, _read_json(_provider_file(user_id)))
    entry = (cfg.setdefault("provider", {})).setdefault(name, {})
    if data.get("npm") is not None:
        entry["npm"] = data["npm"]
    entry["name"] = str(data.get("name") or entry.get("name") or name)
    opts = entry.setdefault("options", {})
    if data.get("base_url"):
        opts["baseURL"] = check_url_wrapper(str(data["base_url"]).strip())
    if data.get("api_key"):
        opts["apiKey"] = str(data["api_key"]).strip()
    if data.get("chat_path"):
        opts["chat_path"] = str(data["chat_path"]).strip()
    if isinstance(data.get("extra_headers"), dict) and data["extra_headers"]:
        bad = [k for k in data["extra_headers"] if str(k).strip().lower() in _BLOCKED_HEADERS]
        if bad:
            raise ValueError(f"header '{bad[0]}' cannot be overridden")
        opts["extra_headers"] = {str(k): str(v) for k, v in data["extra_headers"].items()}
    if isinstance(data.get("extra_body"), dict) and data["extra_body"]:
        opts["extra_body"] = data["extra_body"]
    if data.get("stream_options") is not None:
        opts["stream_options"] = bool(data["stream_options"])
    if isinstance(data.get("models"), list) and data["models"]:
        models = {}
        for m in data["models"]:
            mid = str((m or {}).get("id") or "").strip()
            if not mid:
                continue
            mc = {"name": str(m.get("name") or mid).strip()}
            try:
                if m.get("ctx"):
                    mc["ctx"] = int(m["ctx"])
            except (TypeError, ValueError):
                pass
            models[mid] = mc
        entry["models"] = {**(entry.get("models") or {}), **models}
    _write_providers(user_id, cfg)
    reload(user_id)
    return entry


def write_user_section(user_id: int, section: str, value) -> None:
    """Replace one top-level section of this user's file."""
    cfg = _hydrate_keys(user_id, _read_json(_provider_file(user_id)))
    if value is None:
        cfg.pop(section, None)
    else:
        cfg[section] = deepcopy(value)
    _write_providers(user_id, cfg)
    reload(user_id)


def delete_model(user_id: int, provider: str, model_id: str) -> dict:
    """Remove one model from a provider and unbind any lane pointing at it."""
    provider = str(provider or "").strip()
    model_id = str(model_id or "").strip()
    key = f"{provider}/{model_id}"
    cfg = _read_json(_provider_file(user_id))
    entry = (cfg.get("provider") or {}).get(provider)
    if not isinstance(entry, dict):
        raise ValueError(f"provider not found: {provider}")
    models = entry.get("models")
    if not isinstance(models, dict) or model_id not in models:
        raise ValueError(f"model not found: {key}")
    del models[model_id]
    cl = cfg.setdefault("cloud", {})
    for lane in CLOUD_LANES:
        if cl.get(lane) == key:
            cl[lane] = None
    _write_providers(user_id, cfg)
    reload(user_id)
    return entry


def delete_provider(user_id: int, name: str) -> dict:
    """Remove a provider and unbind any lane that pointed at one of its models."""
    name = str(name or "").strip()
    cfg = _hydrate_keys(user_id, _read_json(_provider_file(user_id)))
    (cfg.get("provider") or {}).pop(name, None)
    from .. import credentials
    credentials.delete_token(_key_id(user_id, name))
    cl = cfg.setdefault("cloud", {})
    for lane in CLOUD_LANES:
        v = cl.get(lane)
        if v and str(v).split("/")[0] == name:
            cl[lane] = None
    _write_providers(user_id, cfg)
    reload(user_id)
    return cfg


def set_lanes(user_id: int, updates: dict) -> dict:
    """Bind lanes to '<provider>/<model>'."""
    cfg = _read_json(_provider_file(user_id))
    cl = cfg.setdefault("cloud", {})
    own = cfg.get("lanes") if isinstance(cfg.get("lanes"), dict) else {}
    for lane, v in updates.items():
        if lane in ("fallback_local", "routing_mode"):
            continue
        v = None if v in (None, "", "local", "null") else str(v).strip()
        if lane in CLOUD_LANES:
            cl[lane] = v
        elif lane in own and v:
            own[lane]["cloud"] = v
    if "fallback_local" in updates:
        cl["fallback_local"] = bool(updates["fallback_local"])
    if "routing_mode" in updates:
        mode = str(updates["routing_mode"] or "auto").strip().lower()
        cl["routing_mode"] = mode if mode in ("auto", "custom") else "auto"
    _write_providers(user_id, cfg)
    reload(user_id)
    return cl

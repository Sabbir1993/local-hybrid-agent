import sys
from copy import deepcopy
from typing import Optional

from ..config import CLOUD_LANES
from .client import CloudClient
from .config import _WARNED, _merged
from .models import CloudModel


def providers(user_id: Optional[int] = None) -> dict:
    return _merged(user_id).get("provider") or {}


def cloud_bindings(user_id: Optional[int] = None) -> dict:
    """{main, executor, vision: '<provider>/<model>' or None, fallback_local: bool,
    routing_mode: 'auto'|'custom'} - stripped and defaulted, whatever app.json / the user file hold."""
    cl = _merged(user_id).get("cloud") or {}
    out = {lane: (str(cl.get(lane) or "").strip() or None) for lane in CLOUD_LANES}
    out["fallback_local"] = bool(cl.get("fallback_local", True))
    mode = str(cl.get("routing_mode") or "auto").strip().lower()
    out["routing_mode"] = mode if mode in ("auto", "custom") else "auto"
    return out


def cloud_models(user_id: Optional[int] = None) -> list:
    """Every configured model that has a usable endpoint, across the user's providers."""
    out = []
    for prov_name, prov_cfg in providers(user_id).items():
        if not isinstance(prov_cfg, dict):
            continue
        models = prov_cfg.get("models")
        if not isinstance(models, dict):
            continue
        for mid, mcfg in models.items():
            cm = CloudModel(prov_name, str(mid), prov_cfg, mcfg if isinstance(mcfg, dict) else {})
            if cm.endpoint():
                out.append(cm)
    return out


def get_cloud(key: Optional[str], user_id: Optional[int] = None) -> Optional[CloudModel]:
    """Resolve "<provider>/<model-id>" (optionally "cloud:"-prefixed) to a CONFIGURED model of the
    user's own providers. An id that is not in the provider's model list does not resolve, so a
    typo or a deleted model falls back to local instead of being sent to the provider."""
    if not key:
        return None
    key = str(key).strip()
    if key.startswith("cloud:"):
        key = key[6:]
    for cm in cloud_models(user_id):
        if cm.key == key:
            return cm
    return None


def user_lanes(user_id: Optional[int] = None) -> dict:
    """Return user's custom lane configs."""
    return _merged(user_id).get("lanes") or {}


def role_map(user_id: Optional[int] = None) -> dict:
    return _merged(user_id).get("role_map") or {}


def verification(user_id: Optional[int] = None) -> dict:
    return _merged(user_id).get("verification") or {}


def cloud_lane(lane: str, user_id: Optional[int] = None) -> Optional[CloudModel]:
    """The CloudModel bound to `lane`, or None when the lane runs local.

    main/executor/vision come from the cloud bindings; with routing_mode "auto" (the default)
    executor and vision follow the main lane, "custom" binds each independently. Any other
    lane is a user-owned cloud lane that carries its own binding."""
    if lane not in CLOUD_LANES:
        d = user_lanes(user_id).get(lane)
        target = str((d or {}).get("cloud") or "").strip() if isinstance(d, dict) else ""
    else:
        b = cloud_bindings(user_id)
        target = b.get("main") if (lane in ("executor", "vision") and b["routing_mode"] == "auto") \
            else b.get(lane)
    if not target or target == "local":
        return None
    cm = get_cloud(target, user_id)
    if cm is None and (lane, target) not in _WARNED:
        _WARNED.add((lane, target))
        print(f"[cloud] lane '{lane}' bound to '{target}' but that model is not configured; "
              f"falling back to local", file=sys.stderr)
    return cm


def lane_kind(lane: str, user_id: Optional[int] = None) -> str:
    return "cloud" if cloud_lane(lane, user_id) is not None else "local"


def lane_client(lane: str, user_id: Optional[int] = None):
    cm = cloud_lane(lane, user_id)
    return CloudClient(cm) if cm is not None else None


def mask_key(k: Optional[str]) -> str:
    """Redact an API key for the UI: first 6, ten stars, last 4."""
    if not k:
        return ""
    k = str(k)
    if len(k) <= 8:
        return "*" * len(k)
    return f"{k[:6]}{'*' * 10}{k[-4:]}"


def providers_public(user_id: Optional[int] = None) -> list:
    """Public projection of this user's providers for the Cloud settings card. The shape is what
    static/js/cloud.js reads: provider, name, npm, base_url, key_masked, has_key, extra_headers,
    models[{id, name, ctx}]. Keys are never included."""
    out = []
    for name, cfg in providers(user_id).items():
        if not isinstance(cfg, dict):
            continue
        opts = cfg.get("options") or {}
        models = cfg.get("models") or {}
        raw_key = opts.get("apiKey") or opts.get("api_key")
        out.append({
            "provider": name,
            "name": str(cfg.get("name") or name),
            "npm": cfg.get("npm"),
            "base_url": str(opts.get("baseURL") or opts.get("base_url") or ""),
            "key_masked": mask_key(raw_key),
            "has_key": bool(str(raw_key or "").strip()),
            "extra_headers": opts.get("extra_headers") or {},
            "models": [{"id": str(mid), "name": str((mcfg or {}).get("name") or mid),
                        "ctx": (mcfg or {}).get("ctx")}
                       for mid, mcfg in (models.items() if isinstance(models, dict) else [])],
        })
    return out

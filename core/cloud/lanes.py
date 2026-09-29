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
    return _merged(user_id).get("cloud") or {}


def cloud_models(user_id: Optional[int] = None) -> list:
    """Return [CloudModel, ...] for all configured models across all providers."""
    out = []
    for prov_name, prov_cfg in providers(user_id).items():
        if not isinstance(prov_cfg, dict):
            continue
        models = prov_cfg.get("models") or {}
        for mid, mcfg in models.items():
            out.append(CloudModel(prov_name, mid, prov_cfg, mcfg if isinstance(mcfg, dict) else {}))
    return out


def get_cloud(key: Optional[str], user_id: Optional[int] = None) -> Optional[CloudModel]:
    """Resolve '<provider>/<model_id>' string to a CloudModel instance."""
    if not key or "/" not in str(key):
        return None
    prov_name, model_id = str(key).split("/", 1)
    prov_cfg = providers(user_id).get(prov_name)
    if not isinstance(prov_cfg, dict):
        return None
    models = prov_cfg.get("models") or {}
    mcfg = models.get(model_id)
    if mcfg is None and not (prov_cfg.get("options") or {}).get("baseURL"):
        return None
    return CloudModel(prov_name, model_id, prov_cfg, mcfg or {})


def user_lanes(user_id: Optional[int] = None) -> dict:
    """Return user's custom lane configs."""
    return _merged(user_id).get("lanes") or {}


def role_map(user_id: Optional[int] = None) -> dict:
    return _merged(user_id).get("role_map") or {}


def verification(user_id: Optional[int] = None) -> dict:
    return _merged(user_id).get("verification") or {}


def cloud_lane(lane: str, user_id: Optional[int] = None) -> Optional[CloudModel]:
    """Return the CloudModel bound to `lane`, or None if the lane runs local."""
    target = cloud_bindings(user_id).get(lane)
    if not target:
        ul = user_lanes(user_id).get(lane)
        if isinstance(ul, dict) and ul.get("cloud"):
            target = ul["cloud"]
    if not target or target == "local":
        return None
    cm = get_cloud(target, user_id)
    if cm is None:
        if (lane, target) not in _WARNED:
            _WARNED.add((lane, target))
            print(f"[cloud] lane '{lane}' bound to '{target}' but model not found; falling back to local",
                  file=sys.stderr)
        return None
    if not cm.base_url:
        if (lane, target) not in _WARNED:
            _WARNED.add((lane, target))
            print(f"[cloud] lane '{lane}' bound to '{target}' but provider has no baseURL; falling back to local",
                  file=sys.stderr)
        return None
    return cm


def lane_kind(lane: str, user_id: Optional[int] = None) -> str:
    return "cloud" if cloud_lane(lane, user_id) is not None else "local"


def lane_client(lane: str, user_id: Optional[int] = None):
    cm = cloud_lane(lane, user_id)
    return CloudClient(cm) if cm is not None else None


def mask_key(k: Optional[str]) -> str:
    """Redact API key for the UI: 'sk-12345...abcd'."""
    s = str(k or "").strip()
    if not s:
        return ""
    if len(s) <= 8:
        return "*" * len(s)
    return f"{s[:4]}...{s[-4:]}"


def providers_public(user_id: Optional[int] = None) -> list:
    """Public projection of this user's configured providers for the UI."""
    out = []
    for pname, pcfg in providers(user_id).items():
        if not isinstance(pcfg, dict):
            continue
        opts = dict(pcfg.get("options") or {})
        k = opts.get("apiKey") or opts.get("api_key")
        opts["apiKey"] = mask_key(k)
        opts["hasKey"] = bool(k)
        opts.pop("api_key", None)
        models = []
        for mid, mcfg in (pcfg.get("models") or {}).items():
            cm = CloudModel(pname, mid, pcfg, mcfg if isinstance(mcfg, dict) else {})
            models.append(cm.to_dict())
        out.append({
            "id": pname,
            "name": pcfg.get("name") or pname,
            "npm": pcfg.get("npm"),
            "options": opts,
            "models": models,
        })
    return out

from typing import Optional

from .constants import KIND_NEED, LANE_NAME_RE, MEDIA_KINDS
from .registry import allow_cloud_audio, registry, validate_mapping


def _check_fallback(name: str, fallback: Optional[str], reg: dict) -> Optional[str]:
    if not fallback:
        return None
    if fallback == name:
        return "a model can't be its own backup"
    if fallback not in reg:
        return f"backup model '{fallback}' doesn't exist"
    seen, cur = {name}, fallback
    while cur:
        if cur in seen:
            return "that backup choice would loop back to this model"
        seen.add(cur)
        cur = (reg.get(cur) or {}).get("fallback")
    return None


def _apply_fallback(name: str, kind: str, cur: dict, fallback: Optional[str], reg: dict) -> None:
    """Validate + set a lane's backup. Media lanes can only back up to the same kind."""
    reg = dict(reg)
    reg.setdefault(name, {"fallback": None, "kind": kind})
    if cur.get("fallback_enabled", True) is False and fallback:
        raise ValueError("fallback is off for this model; clear it or enable fallback first")
    err = _check_fallback(name, fallback, reg)
    if err:
        raise ValueError(err)
    if fallback and kind in MEDIA_KINDS and reg[fallback].get("kind") != kind:
        raise ValueError(f"the backup must also be {KIND_NEED[kind]}")
    if fallback:
        cur["fallback"] = fallback
    else:
        cur.pop("fallback", None)


def save_user_lane(user_id: int, name: str, data: dict) -> dict:
    """Create/update one of this user's own cloud lanes."""
    from .. import cloud
    own = dict(cloud.user_lanes(user_id))
    reg = registry(user_id)
    is_new = name not in own
    if is_new:
        if not LANE_NAME_RE.match(name):
            raise ValueError("name must be 2-32 lowercase letters, digits, '-' or '_', starting with a letter")
        if name in reg:
            raise ValueError(f"'{name}' is already used")
    cur = dict(own.get(name) or {})
    kind = str(data.get("kind") or cur.get("kind") or "chat")
    if kind not in ("chat", "vision", "image_gen", "video_gen", "stt"):
        raise ValueError("cloud models can read or write text, read images, make images or videos, "
                         "or turn speech into text (embeddings stay on this PC)")
    if kind == "stt" and not allow_cloud_audio():
        raise ValueError("speech to text stays on this PC until an admin allows cloud speech")
    if not is_new and cur.get("kind") and cur.get("kind") != kind:
        raise ValueError("a model's type can't be changed - add a new one instead")
    cur["kind"] = kind
    key = str(data.get("cloud") or cur.get("cloud") or "").strip()
    if not key or cloud.get_cloud(key, user_id) is None:
        raise ValueError("pick one of your cloud models (add a provider first if the list is empty)")
    cur["cloud"] = key
    if "vision" in data:
        # marker: this cloud model can read images, so it may serve "Reading images" too
        if data["vision"]:
            cur["vision"] = True
        else:
            cur.pop("vision", None)
    if data.get("label") is not None:
        cur["label"] = str(data["label"]).strip()[:40] or name
    cur.setdefault("label", name)
    if "fallback_enabled" in data:
        cur["fallback_enabled"] = bool(data["fallback_enabled"])
    if "fallback" in data:
        _apply_fallback(name, kind, cur, data.get("fallback") or None, reg)
    if kind not in MEDIA_KINDS and cur.get("fallback_enabled", True):
        cur.setdefault("fallback", "main")
    own[name] = cur
    cloud.write_user_section(user_id, "lanes", own)
    return cur


def delete_user_lane(user_id: int, name: str, move_jobs_to: Optional[str] = None) -> None:
    from .. import cloud
    from .registry import _own_role_map
    own = dict(cloud.user_lanes(user_id))
    if name not in own:
        raise ValueError(f"'{name}' is not one of your models")
    own.pop(name)
    for d in own.values():
        if d.get("fallback") == name:
            if d.get("kind") in MEDIA_KINDS:
                d.pop("fallback", None)
            else:
                d["fallback"] = "main"
    cloud.write_user_section(user_id, "lanes", own)
    rm = dict(_own_role_map(user_id))
    for j, ln in list(rm.items()):
        if ln == name:
            if move_jobs_to and not validate_mapping(j, move_jobs_to, user_id):
                rm[j] = move_jobs_to
            else:
                rm.pop(j)
    cloud.write_user_section(user_id, "role_map", rm)

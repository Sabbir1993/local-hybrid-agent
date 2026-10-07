from typing import Optional

from .constants import (
    _KIND_OK,
    BUILTIN_LANES,
    JOBS,
    KIND_NEED,
    MEDIA_KINDS,
    SHARED_MEDIA_JOBS,
    kind_ok,
)
from .target import _inst


def _fallback_for(cfg: dict, name: str, kind: str = "chat") -> Optional[str]:
    """The lane's fallback, honouring `fallback_enabled` (0/1, default on).

    `fallback_enabled` = 0 means "no fallback": on failure the lane's call ends
    with a clear error instead of silently escalating to main. 1 (default) keeps
    today's behaviour: explicit `fallback` wins, else "main" for non-media lanes.
    Single control read for builtin + custom + cloud lanes.
    """
    if cfg.get("fallback_enabled", True) is False:
        return None
    explicit = cfg.get("fallback")
    if explicit:
        return str(explicit)
    return None if name == "main" or kind in MEDIA_KINDS else "main"


def _main_sees() -> bool:
    """The local main model was started with an mmproj (profile `vision_capable`)."""
    from ..state import state
    return bool((state.profile or {}).get("vision_capable"))


def job_ok(job_kind: str, d: dict) -> bool:
    """`kind_ok` plus capability: any lane that can see (image reader, chat lane with an
    mmproj, cloud model marked vision) may do the image-reading job.""" 
    return kind_ok(job_kind, d["kind"]) or (job_kind == "vision" and bool(d.get("vision")))


def registry(user_id: Optional[int] = None) -> dict:
    """name -> {name, kind, label, fallback, builtin, local, cloud_key, owner}.
    `local`: a local model backs it; `cloud_key`: its effective cloud binding."""
    from .. import cloud
    from ..small_model import APP_CONFIG, lane_engine_of, lane_kind_of
    out = {}
    sm = APP_CONFIG.get("small_models") or {}
    for name, meta in BUILTIN_LANES.items():
        cfg = sm.get(name) or {}
        # A builtin lane's kind can be overridden by config so one
        # vision-capable model (gguf + mmproj) on the executor lane can serve
        # BOTH routine tool calls and image reading from a single process:
        # a vision lane accepts chat jobs (`_KIND_OK["chat"]` includes "vision")
        # and the loop's executor behaviour keys on the lane NAME, not kind.
        kind = str(cfg.get("kind") or meta["kind"]) if cfg.get("kind") else meta["kind"]
        kind = kind if kind in _KIND_OK else meta["kind"]
        out[name] = {
            "name": name,
            "kind": kind,
            "label": str(cfg.get("label") or meta["label"]),
            "fallback": _fallback_for(cfg, name, kind),
            "fallback_enabled": cfg.get("fallback_enabled", True),
            "builtin": True,
            "local": True,
            "owner": "shared",
            "vision": _main_sees() if name == "main" else (kind == "vision" or bool(cfg.get("mmproj"))),
        }
    for name, cfg in sm.items():
        if name in out or not isinstance(cfg, dict):
            continue
        kind = lane_kind_of(name, cfg)
        out[name] = {
            "name": name,
            "kind": kind,
            "label": str(cfg.get("label") or name),
            "fallback": _fallback_for(cfg, name, kind),
            "fallback_enabled": cfg.get("fallback_enabled", True),
            "builtin": False,
            "local": True,
            "owner": "shared",
            "engine": lane_engine_of(name, cfg),
            "vision": kind == "vision" or (kind == "chat" and bool(cfg.get("mmproj"))),
        }
    for name, d in cloud.user_lanes(user_id).items():
        if name in out:
            continue
        k = str(d.get("kind") or "chat")
        k = k if k in _KIND_OK else "chat"
        out[name] = {
            "name": name,
            "kind": k,
            "label": str(d.get("label") or name),
            "fallback": _fallback_for(d, name, k),
            "fallback_enabled": d.get("fallback_enabled", True),
            "builtin": False,
            "local": False,
            "owner": "user",
            "engine": "cloud",
            "vision": k == "vision" or bool(d.get("vision")),
        }
    for name, d in out.items():
        d.setdefault("engine", "llama")
        cm = None
        if d["kind"] != "embed":
            try:
                cm = cloud.cloud_lane(name, user_id)
            except Exception:
                cm = None
        d["cloud_key"] = cm.key if cm else None
        d["cloud_display"] = cm.display if cm else None
        if cm and d["owner"] != "user":
            # main/executor bound to a cloud model: that model decides what it can see
            d["vision"] = bool(cm.can_vision) or d["kind"] == "vision"
        elif cm:
            d["vision"] = d["vision"] or bool(cm.can_vision)
        d["vision"] = bool(d.get("vision")) and d["kind"] not in ("embed",) + MEDIA_KINDS
    return out


def _shared_media_lanes(job: str, reg: dict) -> list:
    """Shared local lanes that can do a media job, the loaded (or loading) one first."""
    if job not in SHARED_MEDIA_JOBS:
        return []
    need = JOBS[job]["kind"]
    names = [n for n, d in reg.items()
             if d["owner"] == "shared" and d["local"] and kind_ok(need, d["kind"])]
    return sorted(names, key=lambda n: _load_rank(_inst(n)))


def _load_rank(inst) -> int:
    """0 loaded, 1 loading, 2 not loaded."""
    if inst is None:
        return 2
    if inst.is_up():
        return 0
    return 1 if getattr(inst, "loading_since", None) else 2


def auto_vision_lane(reg: dict) -> str:
    """Who reads images when nobody was picked: the main model if it can see, else the
    helper if it can, else the dedicated image reader (the only one that needs its own process)."""
    for name in ("main", "executor"):
        if (reg.get(name) or {}).get("vision"):
            return name
    return JOBS["vision"]["default"]


def default_lane(job: str, user_id: Optional[int] = None, reg: Optional[dict] = None) -> Optional[str]:
    """A job's default lane; media jobs default to the shared model on this PC."""
    if job == "vision":
        return auto_vision_lane(reg if reg is not None else registry(user_id))
    spec = JOBS[job]
    if spec["default"] or job not in SHARED_MEDIA_JOBS:
        return spec["default"]
    shared = _shared_media_lanes(job, reg if reg is not None else registry(user_id))
    return shared[0] if shared else None


def role_map(user_id: Optional[int] = None) -> dict:
    """Effective job -> lane for every job (defaults filled in)."""
    from .. import cloud
    rm = cloud.role_map(user_id)
    reg = None
    out = {}
    for job, spec in JOBS.items():
        v = rm.get(job) or spec["default"]
        if job == "vision":
            reg = reg if reg is not None else registry(user_id)
            # no pick, or a pick that can no longer see (e.g. the helper went text-only): automatic
            if not rm.get(job) or not job_ok("vision", reg.get(v) or {"kind": ""}):
                v = auto_vision_lane(reg)
        if not v and job in SHARED_MEDIA_JOBS:
            reg = reg if reg is not None else registry(user_id)
            v = default_lane(job, user_id, reg)
        out[job] = str(v) if v else None
    return out


def allow_cloud_audio() -> bool:
    from ..small_model import APP_CONFIG
    return bool((APP_CONFIG.get("media") or {}).get("allow_cloud_audio", False))


def validate_mapping(job: str, lane: str, user_id: Optional[int] = None) -> Optional[str]:
    """None when `lane` can do `job`, else a plain-language reason."""
    spec = JOBS.get(job)
    if spec is None:
        return f"unknown job '{job}'"
    d = registry(user_id).get(lane)
    if d is None:
        return f"there is no model called '{lane}'"
    if not job_ok(spec["kind"], d):
        return f"'{spec['label']}' needs {KIND_NEED[spec['kind']]}"
    if spec.get("local_only") and not d["local"]:
        return f"'{spec['label']}' always runs on this PC, and '{d['label']}' is a cloud model"
    if spec.get("local_first") and not d["local"] and not allow_cloud_audio():
        return (f"'{spec['label']}' stays on this PC until an admin allows cloud speech "
                "(Settings -> Models & Jobs)")
    if job == "agent.reason" and d["local"] and lane not in ("main",) and not d["cloud_key"]:
        return "'Thinking & planning' needs the main model or a cloud model"
    return None


def _own_role_map(user_id: int) -> dict:
    from .. import cloud
    d = cloud._read_json(cloud._provider_file(user_id)).get("role_map")
    return d if isinstance(d, dict) else {}


def set_role_map(user_id: int, updates: dict, as_default: bool = False) -> dict:
    """Map jobs to lanes. None / "" resets a job to its default."""
    reg = registry(user_id)
    for job, lane in updates.items():
        if job not in JOBS:
            raise ValueError(f"unknown job '{job}'")
        if lane:
            err = validate_mapping(job, lane, user_id)
            if err:
                raise ValueError(err)
            if as_default and reg[lane]["owner"] == "user":
                raise ValueError(f"'{reg[lane]['label']}' is your own cloud model, so it can't be "
                                 "the default for everyone")
    if as_default:
        from ..config import update_app_config

        def _mut(cfg):
            rm = cfg.setdefault("role_map", {})
            for job, lane in updates.items():
                if lane:
                    rm[job] = lane
                else:
                    rm.pop(job, None)
        update_app_config(_mut)
    else:
        from .. import cloud
        rm = dict(_own_role_map(user_id))
        for job, lane in updates.items():
            if lane:
                rm[job] = lane
            else:
                rm.pop(job, None)
        cloud.write_user_section(user_id, "role_map", rm)
    return role_map(user_id)

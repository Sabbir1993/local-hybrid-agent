import sys
from typing import Optional

from .constants import JOBS, SHARED_MEDIA_JOBS
from .registry import (
    _load_rank,
    _shared_media_lanes,
    allow_cloud_audio,
    default_lane,
    job_ok,
    registry,
    role_map,
    validate_mapping,
)
from .target import Target


def targets(job: str, user_id: Optional[int] = None, force_local: bool = False) -> list:
    """Ordered route for a job: mapped lane first, then its fallback chain."""
    from .. import cloud
    spec = JOBS[job]
    reg = registry(user_id)
    mapped = role_map(user_id).get(job) or spec["default"]
    if mapped and validate_mapping(job, mapped, user_id):
        mapped = default_lane(job, user_id, reg)
    if not mapped:
        return []
    local_only = force_local or bool(spec.get("local_only"))
    no_cloud_audio = bool(spec.get("local_first")) and not allow_cloud_audio()

    chain, seen = [], set()
    lane = mapped
    hard_stop = False   # a lane with fallback_enabled=False ends the route here
    while lane and lane not in seen and lane in reg:
        seen.add(lane)
        chain.append(lane)
        if reg[lane].get("fallback_enabled", True) is False:
            hard_stop = True
        lane = reg[lane].get("fallback")
    # image reading: any lane that can see is a valid backup (main, then the helper), so a
    # sleeping/failed reader falls back to a model that already has eyes
    seers = tuple(n for n in ("main", "executor") if job == "vision" and reg.get(n, {}).get("vision"))
    extras = () if hard_stop else (*seers, spec["default"], *_shared_media_lanes(job, reg), "main")
    for extra in extras:
        if extra and extra not in seen and extra in reg:
            seen.add(extra)
            chain.append(extra)

    fb_local = bool(cloud.cloud_bindings(user_id).get("fallback_local", True))
    out = []
    for name in chain:
        d = reg[name]
        if not job_ok(spec["kind"], d):
            continue
        if d["cloud_key"] and not local_only and not no_cloud_audio:
            cm = cloud.cloud_lane(name, user_id)
            if cm:
                out.append(Target(name, cm))
        if d["local"]:
            out.append(Target(name))
    if job in SHARED_MEDIA_JOBS:
        slots = [i for i, t in enumerate(out) if not t.is_cloud]
        ranked = sorted((out[i] for i in slots), key=lambda t: _load_rank(t.inst))
        for i, t in zip(slots, ranked):
            out[i] = t
    if out and out[0].is_cloud and not fb_local:
        return out[:1]
    return out


def primary(job: str, user_id: Optional[int] = None, force_local: bool = False) -> Optional[Target]:
    """First usable step of a job's route, without loading anything."""
    for t in targets(job, user_id, force_local):
        if t.available():
            return t
    return None


def _account_begin(job: str, t):
    """Show a helper-model call in the Monitor. Never raises: accounting must not break the call."""
    import time
    try:
        from ..monitor import monitor_begin
        return monitor_begin(f"lane/{job}", False, model=t.model_name(), source=t.source,
                             n_msgs=1), time.time()
    except Exception:
        return None, time.time()


def _account_end(job: str, t, rid, t0: float, status: int, data) -> None:
    """Close the Monitor row and write the usage row the Report reads, for a helper-model call
    (answer check, search rewrite, ...). Before this only the main chat and agent steps were
    counted, so work done by the executor model never showed up."""
    import time
    try:
        from ..db.usage import db_record_request
        from ..monitor import monitor_end
        u = (data or {}).get("usage") or {}
        tim = (data or {}).get("timings") or {}
        dt = time.time() - t0
        ptoks, ctoks = u.get("prompt_tokens"), u.get("completion_tokens")
        tps = tim.get("predicted_per_second") or (round(ctoks / dt, 2) if ctoks and dt > 0 else None)
        ptps = tim.get("prompt_per_second")
        model = t.model_name()
        if rid is not None:
            monitor_end(rid, status, prompt_tokens=ptoks, completion_tokens=ctoks, tps=tps, duration=dt,
                        prompt_tps=ptps, model=model, source=t.source)
        db_record_request(f"lane/{job}", model, ptoks, ctoks, tps, dt, ptps, False, status,
                          is_orchestrator=(t.lane != "main"), source=t.source)
    except Exception as e:
        print(f"[lanes] accounting for {job} failed: {e}", file=sys.stderr)


async def post_chat(job: str, payload: dict, user_id: Optional[int] = None,
                    force_local: bool = False, only_if_running: bool = False,
                    timeout=None) -> tuple:
    """Non-streaming chat completion for `job`, walking the fallback chain."""
    errors = []
    for t in targets(job, user_id, force_local):
        if only_if_running and not t.is_up():
            continue
        if not t.available():
            continue
        rid, t0 = _account_begin(job, t)
        try:
            c = await t.client()
            r = await c.post("/v1/chat/completions", json=payload, timeout=timeout)
            r.raise_for_status()
            data = r.json()
            _account_end(job, t, rid, t0, 200, data)
            return data, t
        except Exception as e:
            _account_end(job, t, rid, t0, 500, None)
            errors.append(f"{t.describe()}: {type(e).__name__}: {e}")
            print(f"[lanes] {job} via {t.describe()} failed - trying next ({e})", file=sys.stderr)
    raise RuntimeError(f"no model available for '{JOBS[job]['label']}'"
                       + (f" ({'; '.join(errors)})" if errors else ""))


def message_text(data: dict) -> str:
    try:
        return str(data["choices"][0]["message"]["content"] or "")
    except (KeyError, IndexError, TypeError):
        return ""

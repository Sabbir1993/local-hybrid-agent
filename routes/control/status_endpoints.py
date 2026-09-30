import time
from typing import Optional
from fastapi import Depends
from core.auth import Principal
from core.deps import get_current_user, require_permission
from core.db import db_report
from core.gpu import get_gpu_stats, get_hardware_engine_summary, refresh_hw_async
from core.monitor import (
    _monitor_state,
    MONITOR_RECENT_MAX,
)
from core.state import state
from core import cloud

from .base import router


@router.get("/control/status")
async def status(user: Principal = Depends(get_current_user)):
    ctx_info = {"n_ctx": 32768, "n_past": 0, "n_prompt": 0, "pct": 0.0}
    if state.process and state.process.poll() is None:
        try:
            resp = await state.client.get("/slots", timeout=1.5)
            if resp.status_code == 200:
                slots = resp.json()
                if slots and isinstance(slots, list):
                    # show the caller's own slot (routes/common.py slot affinity)
                    s0 = slots[user.id % len(slots)] if user and user.id is not None else slots[0]
                    n_ctx = s0.get("n_ctx") or 32768
                    n_prompt = s0.get("n_prompt_tokens") or 0
                    n_decoded = (s0.get("next_token") or [{}])[0].get("n_decoded") or 0
                    n_past = n_prompt + n_decoded
                    pct = round((n_past / max(1, n_ctx)) * 100, 1)
                    ctx_info = {
                        "n_ctx": n_ctx,
                        "n_past": n_past,
                        "n_prompt": n_prompt,
                        "pct": pct
                    }
        except Exception:
            pass

    cm_main = cloud.cloud_lane("main", user.id)
    cm_exec = cloud.cloud_lane("executor", user.id)
    # cloud main lane has no local /slots to query -- fall back to its
    # configured ctx so the UI doesn't show the local-server default (32768)
    if cm_main and not (state.process and state.process.poll() is None):
        ctx_info = {"n_ctx": cm_main.ctx, "n_past": 0, "n_prompt": 0, "pct": 0.0}
    await refresh_hw_async()
    hw_info = get_hardware_engine_summary()
    return {
        "hardware_tag": hw_info.get("hardware_tag"),
        "discrete_gpus": hw_info.get("discrete_gpus"),
        "engine": hw_info.get("engine"),
        "profile": state.profile.get("name") if state.profile else None,
        "model": state.profile.get("model_path") if state.profile else None,
        "tensor_split": (state.profile.get("tuned", {}).get("tensor_split")
                         if state.profile else None),
        "measured_tg_tokens_per_sec": (state.profile.get("tuned", {}).get("measured_tg_tokens_per_sec")
                                       if state.profile else None),
        "uptime_s": time.time() - state.started_at if state.started_at else None,
        "restart_count": state.restart_count,
        "pid": state.process.pid if state.process and state.process.poll() is None else None,
        "keepalive": state.keepalive_enabled,
        "mtp_enabled": bool(state.profile.get("mtp_enabled")) if state.profile else False,
        "mtp_draft_path": state.profile.get("mtp_draft_path") if state.profile else None,
        "vision_capable": bool(state.profile.get("vision_capable")) if state.profile else False,
        "mmproj_path": state.profile.get("mmproj_path") if state.profile else None,
        "context": ctx_info,
        # cloud lanes: the UI shows a ☁️ pill instead of "Model unloaded"
        "main_source": "cloud" if cm_main else "local",
        "executor_source": "cloud" if cm_exec else "local",
        "cloud_main": ({"key": cm_main.key, "model": cm_main.model_id,
                        "display": cm_main.display, "provider": cm_main.provider_name}
                       if cm_main else None),
        "cloud_executor": ({"key": cm_exec.key, "model": cm_exec.model_id,
                            "display": cm_exec.display, "provider": cm_exec.provider_name}
                           if cm_exec else None),
    }


@router.get("/control/gpu")
async def gpu(user: Principal = Depends(require_permission("settings.runtime.view"))):
    return await get_gpu_stats()


@router.get("/control/monitor")
async def monitor(user: Principal = Depends(require_permission("monitor.view"))):
    now = time.time()
    # Stale watchdog: -1 disables the timeout so long generations/summaries are never reaped as 499
    STALE_S = -1
    if STALE_S > 0:
        for rid in list(_monitor_state["active"].keys()):
            req = _monitor_state["active"][rid]
            if now - req["last_token_s"] > STALE_S:
                _monitor_state["active"].pop(rid, None)
                req.update({
                    "status": 499, "completion_tokens": req.get("gen_tokens"),
                    "duration_s": round(now - req["start"], 2), "tps": None,
                    "prompt_tps": None, "end": now, "stale": True,
                })
                _monitor_state["recent"].append(req)
    if len(_monitor_state["recent"]) > MONITOR_RECENT_MAX:
        _monitor_state["recent"] = _monitor_state["recent"][-MONITOR_RECENT_MAX:]
    active = []
    for rid, req in list(_monitor_state["active"].items()):
        info = dict(req)
        elapsed = max(0.05, now - req["start"])
        info["elapsed_s"] = round(elapsed, 2)
        toks = req.get("gen_tokens", 0)
        if toks > 0 and elapsed > 0:
            cumulative_tps = toks / elapsed
            live_tps = req.get("gen_tps") or cumulative_tps
            if live_tps > cumulative_tps * 2.0 or live_tps < cumulative_tps * 0.5:
                live_tps = cumulative_tps
            info["gen_tps"] = round(live_tps, 1)
        else:
            info["gen_tps"] = 0.0
        active.append(info)
    active.sort(key=lambda x: -x["id"])
    recent = []
    for r in _monitor_state["recent"]:
        info = {k: r.get(k) for k in ("id", "endpoint", "model", "stream", "status",
                                       "prompt_tokens", "completion_tokens",
                                       "tps", "duration_s", "prompt_tps", "gen_tps",
                                       "source", "provider")}
        info["ago_s"] = round(now - r["end"], 1)
        info["ended_at"] = r["end"]
        recent.append(info)
    recent.sort(key=lambda x: -x["id"])
    return {
        "active": active,
        "recent": recent[:30],
        "llama_pid": state.process.pid if state.process and state.process.poll() is None else None,
        "keepalive": state.keepalive_enabled,
    }


@router.get("/control/report")
async def report(days: int = 30, model: Optional[str] = None,
                  start: Optional[str] = None, end: Optional[str] = None,
                  user: Principal = Depends(require_permission("usage.report.view"))):
    try:
        days = max(1, min(365, int(days)))
    except ValueError:
        days = 30
    return db_report(days, model, start, end)

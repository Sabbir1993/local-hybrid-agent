import asyncio
import time
from typing import Optional
from fastapi import Depends
from core.auth import Principal
from core.deps import get_current_user, require_permission
from core.db import db_report, db_report_runs
from core.gpu import get_gpu_stats, get_hardware_engine_summary, refresh_hw_async
from core.monitor import (
    _monitor_state,
    MONITOR_RECENT_MAX,
)
from core.config import LOCAL_HTTP_READ_TIMEOUT_S
from core.supervision import health_timeout_s, max_restart_count, supervision_limit
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
        "max_restart_count": max_restart_count(),
        "health_timeout_s": health_timeout_s(),
        "vram_wall_free_mb": supervision_limit("vram_wall_free_mb"),
        # set when the crash-loop breaker trips, so the UI can say why nothing is running
        # instead of just showing no pid
        "degraded": state.degraded,
        "last_load_error": state.last_load_error,
        "log_tail": state.log_tail(20),
        "log_tail_lines": len(state._log_tail),
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


def _memory_report() -> dict:
    """System RAM plus resident memory of this server and every child it started (llama-server lanes, whisper,
    sd-server), labelled by --port, so a session that keeps growing shows which process it is."""
    try:
        import psutil
    except ImportError:
        return {"error": "psutil is not installed (pip install psutil)"}
    import re
    vm = psutil.virtual_memory()
    me = psutil.Process()
    rows = []
    for p in [me] + me.children(recursive=True):
        try:
            cmd = " ".join(p.cmdline())
            m = re.search(r"--port\s+(\d+)", cmd)
            rows.append({"pid": p.pid, "name": p.name(), "port": int(m.group(1)) if m else None,
                         "rss_mb": round(p.memory_info().rss / 1048576),
                         "self": p.pid == me.pid})
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
    rows.sort(key=lambda r: -r["rss_mb"])
    return {"total_mb": round(vm.total / 1048576), "used_mb": round((vm.total - vm.available) / 1048576),
            "processes": rows}


@router.get("/control/memory")
async def memory(user: Principal = Depends(require_permission("settings.runtime.view"))):
    return await asyncio.get_running_loop().run_in_executor(None, _memory_report)


@router.get("/control/monitor")
async def monitor(user: Principal = Depends(require_permission("monitor.view"))):
    now = time.time()
    # Stale watchdog. This was hardcoded to -1 ("disabled") because a long generation or a
    # summary could be reaped as 499 while it was still working. Now that a request cannot go
    # silent for longer than the local read timeout (core/config.py), a threshold above that
    # cannot reap live work -- and it does collect the entries an aborted run leaves behind,
    # since a client disconnect skips every monitor_end() on the way out.
    stale_s = int(LOCAL_HTTP_READ_TIMEOUT_S) + 60
    for rid in list(_monitor_state["active"].keys()):
        req = _monitor_state["active"][rid]
        if now - req["last_token_s"] > stale_s:
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


@router.get("/control/report/runs")
async def report_runs(days: int = 7, limit: int = 10,
                      user: Principal = Depends(require_permission("usage.report.view"))):
    """Heaviest agent runs (prompt tokens, peak step, steps, how each ended)."""
    return {"days": max(1, min(365, days)), "runs": db_report_runs(days, limit)}


@router.get("/control/report")
async def report(days: int = 30, model: Optional[str] = None,
                  start: Optional[str] = None, end: Optional[str] = None,
                  user: Principal = Depends(require_permission("usage.report.view"))):
    try:
        days = max(1, min(365, int(days)))
    except ValueError:
        days = 30
    return db_report(days, model, start, end)

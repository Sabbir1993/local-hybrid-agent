"""
routes/control.py - Server management, configuration, profiling, and monitoring endpoints.
"""

import asyncio
import json
import sys
import time
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from core.audit import audit_log
from core.auth import Principal, user_has_permission
from core.deps import get_current_user, require_permission

from core.config import (
    CONFIG_DEFAULTS,
    CONFIG_INT_FIELDS,
    CONFIG_CHOICE_FIELDS,
    CONFIG_TARGETS,
    MODEL_CONFIG_KEYS,
)
from core.db import db_report
from core.gpu import get_gpu_stats
from core.profiles import (
    MODELS_DIR,
    build_dynamic_profile,
    find_mtp_draft,
    save_model_config,
    load_model_configs,
    _model_key,
)
from core.monitor import (
    _monitor_state,
    MONITOR_RECENT_MAX,
)
from core.state import state
from core import cloud
from core import vram
from . import common

router = APIRouter(tags=["control"])

class SwitchRequest(BaseModel):
    profile: Optional[str] = None
    target: Optional[str] = None


class KeepaliveRequest(BaseModel):
    enabled: bool


class ConfigRequest(BaseModel):
    updates: dict
    persist: bool = True
    restart: bool = True


def _apply_config_update(profile: dict, key: str, value) -> Optional[str]:
    if profile is None:
        return "No profile loaded to update"
    if key == "keepalive_interval_s":
        try:
            state.keepalive_interval_s = max(5, min(600, int(value)))
        except (TypeError, ValueError):
            return "keepalive_interval_s must be an integer"
        return None
    if isinstance(value, str) and not value.strip():
        value = CONFIG_DEFAULTS.get(key)
        if value is None:
            return f"{key} cannot be empty"
    if key == "mtp_enabled":
        if isinstance(value, str):
            value = value.strip().lower() in ("true", "1", "on", "yes")
        profile["mtp_enabled"] = bool(value)
        return None
    if key in CONFIG_INT_FIELDS:
        lo, hi = CONFIG_INT_FIELDS[key]
        try:
            v = int(value)
        except (TypeError, ValueError):
            return f"{key} must be an integer"
        v = max(lo, min(hi, v))
        if key == "context_size":
            v -= v % 8
        section, field = CONFIG_TARGETS[key]
        if section:
            profile.setdefault(section, {})[field] = v
        else:
            profile[field] = v
        return None
    if key in CONFIG_CHOICE_FIELDS:
        value = str(value).strip()
        if value not in CONFIG_CHOICE_FIELDS[key]:
            return f"{key} must be one of {CONFIG_CHOICE_FIELDS[key]}"
        section, field = CONFIG_TARGETS[key]
        if section:
            profile.setdefault(section, {})[field] = value
        else:
            profile[field] = value
        return None
    if key == "tensor_split":
        value = str(value).strip()
        segs = [s.strip() for s in value.split(",")]
        if not (1 <= len(segs) <= 4) or not all(s.isdigit() and int(s) >= 0 for s in segs):
            return "tensor_split must be comma-separated non-negative integers, e.g. 9,11 (0,1 = GPU 2 only, 1,0 = GPU 1 only)"
        if not any(int(s) > 0 for s in segs):
            return "tensor_split: at least one GPU share must be > 0 (e.g. 0,1 or 1,0)"
        profile.setdefault("tuned", {})["tensor_split"] = ",".join(segs)
        return None
    return f"unknown config field: {key}"


def _config_for_profile(p: dict) -> dict:
    t = p.get("tuned", {})
    return {
        "context_size": p.get("context_size", CONFIG_DEFAULTS["context_size"]),
        "n_gpu_layers": t.get("n_gpu_layers", p.get("n_gpu_layers", CONFIG_DEFAULTS["n_gpu_layers"])),
        "tensor_split": t.get("tensor_split", p.get("tensor_split", CONFIG_DEFAULTS["tensor_split"])),
        "split_mode": p.get("split_mode", CONFIG_DEFAULTS["split_mode"]),
        "threads": p.get("threads", CONFIG_DEFAULTS["threads"]),
        "threads_batch": p.get("threads_batch", CONFIG_DEFAULTS["threads_batch"]),
        "batch_size": p.get("batch_size", CONFIG_DEFAULTS["batch_size"]),
        "ubatch_size": p.get("ubatch_size", CONFIG_DEFAULTS["ubatch_size"]),
        "n_slots": p.get("n_slots", CONFIG_DEFAULTS["n_slots"]),
        "flash_attn": p.get("flash_attn", CONFIG_DEFAULTS["flash_attn"]),
        "kv_cache_type": p.get("kv_cache_type", CONFIG_DEFAULTS["kv_cache_type"]),
        "keepalive_interval_s": state.keepalive_interval_s,
        "llama_bin_dir": p.get("llama_bin_dir", CONFIG_DEFAULTS["llama_bin_dir"]),
        "gpu_devices": p.get("gpu_devices", CONFIG_DEFAULTS["gpu_devices"]),
        "mtp_available": bool(p.get("mtp_draft_path")),
        "mtp_draft_path": p.get("mtp_draft_path"),
        "mtp_enabled": bool(p.get("mtp_enabled", False)) if not p.get("mtp_draft_path") else bool(p.get("mtp_enabled", True)),
        "mtp_draft_n_max": p.get("mtp_draft_n_max", 3),
    }


def _standalone_profile(target: str) -> Optional[dict]:
    """Profile dict for a selected-but-unloaded model: saved config + MTP detection.

    Same merge logic build_dynamic_profile() uses at load time, so values shown
    and saved in the drawer match what the model will actually launch with.
    """
    p = Path(target)
    if not p.exists():
        return None
    saved = load_model_configs().get(_model_key(p)) or {}
    prof = {"name": p.stem, "model_path": str(p)}
    for k, v in CONFIG_DEFAULTS.items():
        prof.setdefault(k, v)
    for k in MODEL_CONFIG_KEYS:
        if k in saved:
            prof[k] = saved[k]
    # n_gpu_layers / tensor_split live in the "tuned" section while loaded;
    # put saved values there so _config_for_profile/save round-trip correctly
    prof.setdefault("tuned", {})
    if "n_gpu_layers" in saved:
        prof["tuned"]["n_gpu_layers"] = saved["n_gpu_layers"]
    if "tensor_split" in saved:
        prof["tuned"]["tensor_split"] = saved["tensor_split"]
    prof["mtp_draft_path"] = str(find_mtp_draft(str(p)))
    return prof


@router.get("/control/config")
async def get_config(model: Optional[str] = None, user: Principal = Depends(get_current_user)):
    # Cloud model selected in the dropdown: llama-server launch params don't
    # apply, so report a cloud-shaped config (the drawer disables the fields).
    if model and str(model).startswith("cloud:"):
        cm = cloud.get_cloud(model, user.id)
        if cm:
            return {"cloud": True, "provider": cm.provider_name, "model": cm.model_id,
                    "display": cm.display, "ctx": cm.ctx, "context_size": cm.ctx,
                    "endpoint": cm.endpoint()}
    # No ?model= given: if the main lane itself is cloud-bound, report that
    if not model:
        cm_bound = cloud.cloud_lane("main", user.id)
        if cm_bound:
            return {"cloud": True, "provider": cm_bound.provider_name, "model": cm_bound.model_id,
                    "display": cm_bound.display, "ctx": cm_bound.ctx,
                    "context_size": cm_bound.ctx, "endpoint": cm_bound.endpoint()}
    # The ?model= target wins when it names a different model than the one
    # loaded — the drawer edits the dropdown-selected model, not what's in VRAM.
    if state.profile is not None:
        loaded_key = _model_key(state.profile.get("model_path") or "")
        if not model or _model_key(model) == loaded_key:
            return _config_for_profile(state.profile)
    target = model or common.curStatus_model_hint
    if target and Path(target).exists():
        prof = _standalone_profile(target)
        if prof:
            return _config_for_profile(prof)
    if state.profile is not None:
        return _config_for_profile(state.profile)
    return JSONResponse({"error": "no profile loaded"}, status_code=400)


@router.post("/control/config")
async def set_config(req: ConfigRequest, model: Optional[str] = None,
                      user: Principal = Depends(require_permission("model.local.configure"))):
    # global common.curStatus_model_hint
    # validate + persist against the live profile when it matches the request
    # target (or no target); otherwise against a temp profile built from the
    # selected model's saved config
    if state.profile is not None:
        loaded_key = _model_key(state.profile.get("model_path") or "")
        if not model or _model_key(model) == loaded_key:
            prof = state.profile
        else:
            prof = _standalone_profile(model)
            if prof is None:
                return JSONResponse({"error": f"model file not found: {model}"}, status_code=404)
    else:
        target = model or common.curStatus_model_hint
        if not target:
            return JSONResponse({"error": "no profile loaded"}, status_code=400)
        prof = _standalone_profile(target)
        if prof is None:
            return JSONResponse({"error": f"model file not found: {target}"}, status_code=404)
    errors = []
    for key, value in req.updates.items():
        err = _apply_config_update(prof, key, value)
        if err:
            errors.append(err)
    if errors:
        return JSONResponse({"error": "; ".join(errors)}, status_code=400)
    m_id = prof.get("model_path") or prof.get("name")
    if req.persist and m_id:
        save_model_config(m_id, prof)
        common.curStatus_model_hint = m_id
    was_running = state.process is not None and state.process.poll() is None
    if req.restart and was_running and state.profile is not None:
        try:
            target = state.profile_path or state.profile
            await state.load_profile(target)
        except Exception as e:
            return JSONResponse({"error": str(e)}, status_code=500)
    return {"ok": True, "restarted": req.restart and was_running,
            "config": _config_for_profile(prof)}


@router.get("/control/status")
async def status(user: Principal = Depends(get_current_user)):
    ctx_info = {"n_ctx": 32768, "n_past": 0, "n_prompt": 0, "pct": 0.0}
    if state.process and state.process.poll() is None:
        try:
            resp = await state.client.get("/slots", timeout=1.5)
            if resp.status_code == 200:
                slots = resp.json()
                if slots and isinstance(slots, list):
                    s0 = slots[0]
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
    return {
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


@router.get("/control/profiles")
async def profiles(user: Principal = Depends(get_current_user)):
    models_out = []

    search_dirs = [MODELS_DIR]
    if common.models_dir and common.models_dir.exists() and common.models_dir != MODELS_DIR:
        search_dirs.insert(0, common.models_dir)

    seen_paths = set()
    for sdir in search_dirs:
        if sdir.exists() and sdir.is_dir():
            for gfile in sorted(sdir.glob("*.gguf")):
                if gfile.stem.lower().startswith(("mtp-", "mmproj-")):
                    continue
                if "orchestrator" in [part.lower() for part in gfile.parts]:
                    continue
                resolved_str = str(gfile.resolve())
                if resolved_str in seen_paths:
                    continue
                seen_paths.add(resolved_str)
                mtp = find_mtp_draft(str(gfile))
                try:
                    size_gb = round(gfile.stat().st_size / (1024**3), 1)
                except OSError:
                    size_gb = None
                family = gfile.stem.split("-")[0]
                models_out.append({
                    "type": "model",
                    "name": gfile.name,
                    "path": str(gfile),
                    "model_path": str(gfile),
                    "model_exists": True,
                    "mtp_available": bool(mtp),
                    "mtp_draft_path": str(mtp) if mtp else None,
                    "size_gb": size_gb,
                    "family": family,
                })

    return {"models": models_out, "profiles": [],
            "cloud": [{
                "type": "cloud",
                "provider": cm.provider,
                "provider_name": cm.provider_name,
                "key": cm.key,
                "value": f"cloud:{cm.key}",
                "model": cm.model_id,
                "name": cm.display,
                "display": cm.display,
                "size_gb": None,
                "ctx": cm.ctx,
            } for cm in cloud.cloud_models(user.id)]}


@router.get("/control/available_models")
async def available_models(user: Principal = Depends(get_current_user)):
    """Read-only trimmed model list for the nav bar: no launch params, no VRAM
    controls, just what's already loaded (local, shared) plus this user's own
    configured cloud models."""
    loaded_name = state.profile.get("name") if state.profile else None
    is_running = state.process is not None and state.process.poll() is None
    out = []
    if loaded_name:
        out.append({"id": loaded_name, "display": loaded_name, "currently_loaded": is_running})
    for cm in cloud.cloud_models(user.id):
        out.append({"id": f"cloud:{cm.key}", "display": cm.display, "currently_loaded": False})
    return {"models": out}


@router.post("/control/stop")
async def stop_server(user: Principal = Depends(require_permission("model.local.load"))):
    was = state.profile.get("name") if state.profile else None
    await state.stop()
    audit_log(user, action="model.stop", resource=was, result="allow")
    return {"ok": True, "stopped": True}


@router.get("/control/preflight")
async def preflight(target: Optional[str] = None):
    """VRAM fit projection for a model/profile without loading it.
    ?target=<gguf or profile json path> - defaults to the currently selected profile."""
    try:
        if target:
            p = Path(target)
            if p.suffix == ".json" and p.exists():
                profile = json.loads(p.read_text())
            elif p.exists():
                profile = build_dynamic_profile(p)
            else:
                return JSONResponse({"error": f"target not found: {target}"}, status_code=404)
        elif state.profile is not None:
            profile = state.profile
        else:
            return JSONResponse({"error": "no model/profile selected"}, status_code=400)
        loop = asyncio.get_event_loop()
        plan = await loop.run_in_executor(None, vram.plan_launch, profile)
        return plan
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


@router.get("/control/vram")
async def vram_devices():
    """Raw per-Vulkan-device free/total VRAM (VK_EXT memory budget)."""
    loop = asyncio.get_event_loop()
    devs = await loop.run_in_executor(None, vram.query_devices, None, True)
    return {"devices": [
        {"index": d["index"], "name": d["name"],
         "total_gb": round(d["total_b"] / (1024 ** 3), 2),
         "free_gb": round(d["free_b"] / (1024 ** 3), 2),
         "used_gb": round(d["used_b"] / (1024 ** 3), 2)}
        for d in devs]}


@router.post("/control/start")
async def start_server(user: Principal = Depends(require_permission("model.local.load"))):
    if state.profile is None and state.profile_path is None:
        return JSONResponse({"error": "No model or profile selected. Select a model/profile first."}, status_code=400)
    try:
        await state.start()
    except Exception as e:
        audit_log(user, action="model.start", result="error", detail={"error": str(e)})
        return JSONResponse({"error": str(e)}, status_code=500)
    audit_log(user, action="model.start", resource=state.profile.get("name") if state.profile else None, result="allow")
    return {"ok": True, "pid": state.process.pid if state.process else None}


@router.post("/control/restart")
async def restart_server(user: Principal = Depends(require_permission("model.local.load"))):
    try:
        await state.start()
    except Exception as e:
        audit_log(user, action="model.restart", result="error", detail={"error": str(e)})
        return JSONResponse({"error": str(e)}, status_code=500)
    audit_log(user, action="model.restart", resource=state.profile.get("name") if state.profile else None, result="allow")
    return {"ok": True, "pid": state.process.pid if state.process else None}


@router.post("/control/keepalive")
async def set_keepalive(req: KeepaliveRequest, user: Principal = Depends(require_permission("settings.runtime.view"))):
    state.keepalive_enabled = req.enabled
    state.last_activity = time.time()
    print(f"[server_manager] keepalive {'enabled' if req.enabled else 'disabled'}")
    return {"ok": True, "keepalive": state.keepalive_enabled}


@router.post("/control/switch")
async def switch(req: SwitchRequest, user: Principal = Depends(get_current_user)):
    # global common.curStatus_model_hint
    target = req.target or req.profile
    if not target:
        return JSONResponse({"error": "No profile or model target specified"}, status_code=400)

    # Cloud model: bind the MAIN lane to it and never spawn llama-server. The
    # local model, if any, is left untouched (selection is who answers you).
    # Cloud provider/model choice is fully user-managed -- no permission gate.
    if str(target).startswith("cloud:"):
        cm = cloud.get_cloud(target, user.id)
        if cm is None:
            return JSONResponse({"error": f"cloud model not configured: {target[6:]}"}, status_code=404)
        cloud.set_lanes(user.id, {"main": cm.key})
        common.curStatus_model_hint = target
        print(f"[server_manager] main lane -> cloud {cm.key} ({cm.provider_name}); "
              f"local llama-server not started")
        audit_log(user, action="model.load", resource=cm.key, detail={"cloud": True}, result="allow")
        return {"ok": True, "cloud": True, "model": cm.key, "display": cm.display,
                "provider": cm.provider_name, "endpoint": cm.endpoint()}

    # Local GGUF: launching a process on the shared GPU rig stays privileged.
    if not user_has_permission(user, "model.local.load"):
        audit_log(user, action="model.local.load", permission_key="model.local.load", result="deny")
        return JSONResponse({"error": "missing permission: model.local.load"}, status_code=403)

    path = Path(target)
    if not path.exists():
        return JSONResponse({"error": f"Target file not found: {target}"}, status_code=404)

    # Selecting a local model releases the main lane from the cloud (if bound)
    if cloud.cloud_bindings(user.id).get("main"):
        cloud.set_lanes(user.id, {"main": None})
        print("[server_manager] main lane -> local (cloud binding cleared)")
    try:
        await state.load_profile(path)
    except Exception as e:
        audit_log(user, action="model.load", resource=str(path), result="error", detail={"error": str(e)})
        return JSONResponse({"error": str(e)}, status_code=500)
    common.curStatus_model_hint = state.profile.get("model_path") if state.profile else None
    audit_log(user, action="model.load", resource=state.profile.get("name", path.stem), result="allow")
    return {"ok": True, "profile": state.profile.get("name", path.stem)}


@router.get("/control/monitor")
async def monitor():
    now = time.time()
    STALE_S = 120
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
                  user: Principal = Depends(require_permission("usage.report.view"))):
    try:
        days = max(1, min(365, int(days)))
    except ValueError:
        days = 30
    return db_report(days, model)




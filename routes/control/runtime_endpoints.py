"""Machine-level runtime settings (config/app.json `runtime` + `runtimes`).

The model config drawer edits per-model launch flags. These endpoints edit the
install-level choice of llama.cpp backend, binary directory and GPU device list —
the settings README used to say had no UI and required a hand edit of app.json.

Every write is validated by the same `select_runtime()` that runs at startup, so
a value that would stop the server from booting cannot be persisted.
"""

from __future__ import annotations

import copy
import os
from typing import Optional, Union

from fastapi import Depends, HTTPException, Request
from pydantic import BaseModel

from core.audit import audit_log
from core.auth import Principal
from core.backend import BACKENDS, BackendConfigError, get_backend
from core.config import (
    CONFIG_DEFAULTS,
    MAX_GPUS,
    RuntimeConfigError,
    select_runtime,
    update_app_config,
)
from core.deps import require_permission
from core.small_model.config import APP_CONFIG

from .base import router


class RuntimeSettingsRequest(BaseModel):
    runtime: Optional[str] = None
    backend: Optional[str] = None
    llama_bin_dir: Optional[str] = None
    # "auto" or a list of GPU indices; a comma/space string is accepted from the UI.
    gpu_devices: Optional[Union[str, list[int]]] = None
    small_model_gpu: Optional[int] = None


def _resolved(app: dict) -> dict:
    try:
        r = select_runtime(app, environ={})       # ignore LLAMA_RUNTIME for the saved view
        return {k: r.get(k) for k in ("name", "backend", "llama_bin_dir", "gpu_devices",
                                      "gpu_auto", "small_model_gpu")} | {"error": None}
    except (RuntimeError, ValueError) as exc:
        return {"error": str(exc)[:300]}


def _snapshot() -> dict:
    app = APP_CONFIG
    runtimes = app.get("runtimes") if isinstance(app.get("runtimes"), dict) else {}
    return {
        "runtime": str(app.get("runtime") or CONFIG_DEFAULTS["backend"]),
        "runtimes": copy.deepcopy(runtimes),
        "available_backends": sorted(BACKENDS),
        "active": _resolved(app),
        # An environment override wins over the saved choice until it is removed.
        "env_override": os.environ.get("LLAMA_RUNTIME") or None,
        "requires_restart": True,
    }


def _clean_gpus(value) -> Optional[Union[str, list[int]]]:
    if value is None:
        return None
    if isinstance(value, str):
        text = value.strip()
        if text.lower() == "auto":
            return "auto"
        parts = [p.strip() for p in text.replace(";", ",").split(",")]
    elif isinstance(value, (list, tuple)):
        parts = [str(p).strip() for p in value]
    else:
        raise HTTPException(status_code=400, detail="gpu_devices must be 'auto' or a list of indices")
    out: list[int] = []
    for part in parts:
        if not part:
            continue
        try:
            idx = int(part)
        except ValueError:
            raise HTTPException(status_code=400, detail=f"invalid GPU index: {part[:40]}")
        if idx < 0 or idx >= MAX_GPUS:
            raise HTTPException(status_code=400, detail=f"GPU index must be 0..{MAX_GPUS - 1}: {idx}")
        if idx in out:
            raise HTTPException(status_code=400, detail=f"duplicate GPU index: {idx}")
        out.append(idx)
    if not out:
        raise HTTPException(status_code=400, detail="gpu_devices must name at least one GPU, or 'auto'")
    return out


@router.get("/control/runtime")
async def get_runtime(user: Principal = Depends(require_permission("settings.runtime.view"))):
    return _snapshot()


@router.put("/control/runtime")
async def set_runtime(req: RuntimeSettingsRequest, request: Request,
                      user: Principal = Depends(require_permission("model.local.configure"))):
    before = _snapshot()
    name = str(req.runtime or before["runtime"]).strip().lower()
    if not name or len(name) > 64:
        raise HTTPException(status_code=400, detail="runtime name must be 1-64 characters")

    backend = None
    if req.backend is not None:
        try:
            backend = get_backend(req.backend).name
        except BackendConfigError as exc:
            raise HTTPException(status_code=400, detail=str(exc))

    gpus = _clean_gpus(req.gpu_devices)
    bin_dir = req.llama_bin_dir.strip() if req.llama_bin_dir is not None else None
    if bin_dir is not None and not bin_dir:
        raise HTTPException(status_code=400, detail="llama_bin_dir must be a non-empty path")
    small_gpu = req.small_model_gpu
    if small_gpu is not None and (isinstance(small_gpu, bool) or small_gpu < 0):
        raise HTTPException(status_code=400, detail="small_model_gpu must be a non-negative integer")

    def mutate(cfg: dict) -> None:
        cfg["runtime"] = name
        runtimes = cfg.get("runtimes")
        if not isinstance(runtimes, dict):
            runtimes = cfg["runtimes"] = {}
        preset = runtimes.get(name)
        if not isinstance(preset, dict):
            # A new preset starts from the shipped defaults, never from an empty dict.
            preset = {
                "llama_bin_dir": CONFIG_DEFAULTS["llama_bin_dir"],
                "backend": CONFIG_DEFAULTS["backend"],
                "gpu_devices": list(CONFIG_DEFAULTS["gpu_devices"]),
            }
            runtimes[name] = preset
        if backend is not None:
            preset["backend"] = backend
        if bin_dir is not None:
            preset["llama_bin_dir"] = bin_dir
        if gpus is not None:
            preset["gpu_devices"] = gpus
        if small_gpu is not None:
            preset["small_model_gpu"] = small_gpu
        # Fail before writing if this would leave the app unbootable.
        select_runtime(cfg, environ={})

    try:
        cfg = update_app_config(mutate)
    except (RuntimeError, ValueError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)[:400])
    except OSError as exc:
        raise HTTPException(status_code=500, detail=f"could not write config: {type(exc).__name__}")

    APP_CONFIG["runtime"] = name
    APP_CONFIG.setdefault("runtimes", {})[name] = copy.deepcopy(cfg["runtimes"][name])

    audit_log(user, action="runtime.configure", resource=name,
              detail={"backend": backend, "gpu_devices": gpus, "small_model_gpu": small_gpu},
              result="allow", ip=request.client.host if request.client else None)

    result = {
        "ok": True,
        "runtime": name,
        "requires_restart": True,
        "active": _resolved(cfg),
        "env_override": os.environ.get("LLAMA_RUNTIME") or None,
    }
    if result["env_override"]:
        result["warning"] = ("LLAMA_RUNTIME is set in the environment and wins over the saved "
                             "runtime; remove it and restart for this change to take effect.")
    return result

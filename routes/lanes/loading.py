import asyncio
import time
from fastapi import Depends
from core.audit import audit_log
from core.auth import Principal, user_has_permission
from core.deps import get_current_user

from .helpers import _err, _view, router


_LOAD_TASKS: set = set()


async def _bg_load(inst) -> None:
    try:
        await inst.load()
    except Exception as e:
        print(f"[lanes] loading {inst.role} failed: {type(e).__name__}: {str(e)[:200]}")


@router.post("/control/lanes/{name}/load")
async def lanes_load(name: str, user: Principal = Depends(get_current_user)):
    """Load a local image/video model (stable-diffusion.cpp). The only way it starts:
    a request never loads it. Returns at once; the page polls /control/lanes."""
    from core.small_model import small_models
    from core import media
    if not user_has_permission(user, "model.local.load"):
        audit_log(user, action="lanes.load", resource=name, result="deny")
        return _err("You don't have permission to load models on this PC - ask an admin.", 403)
    inst = small_models.instances.get(name)
    if inst is None:
        return _err(f"'{name}' is not a local model", 404)
    if not media._is_sdcpp(inst):
        return _err("Only image and video models on this PC are loaded by hand - "
                    "the others start by themselves when needed.")
    if not inst.is_up() and not inst.loading_since:
        # one image model at a time: another loaded (or loading) one must be unloaded first
        if inst.kind == "image_gen":
            other = next((o for n, o in small_models.instances.items()
                          if n != name and media._is_sdcpp(o) and o.kind == "image_gen"
                          and (o.is_up() or o.loading_since)), None)
            if other is not None:
                label = other.cfg.get("label") or other.role
                return _err(f"'{label}' is already {'loaded' if other.is_up() else 'loading'} - "
                            "only one image model can be loaded at a time. Unload it first.", 409)
        if not inst.available:
            from core.small_model import find_sd_server
            return _err("stable-diffusion.cpp isn't installed yet - see the setup steps."
                        if find_sd_server() is None else "A model file is missing - pick the files again.")
        inst.loading_since = time.time()          # visible as "loading" right away
        t = asyncio.create_task(_bg_load(inst))
        _LOAD_TASKS.add(t)
        t.add_done_callback(_LOAD_TASKS.discard)
        audit_log(user, action="lanes.load", resource=name, result="allow")
    return {"ok": True, **_view(user)}


@router.post("/control/lanes/{name}/stop")
async def lanes_stop(name: str, user: Principal = Depends(get_current_user)):
    from core.small_model import small_models
    if not user_has_permission(user, "model.local.load"):
        return _err("You don't have permission to stop local models.", 403)
    inst = small_models.instances.get(name)
    if inst is None:
        return _err(f"'{name}' is not a local helper model", 404)
    if getattr(inst, "busy", 0):
        return _err("It's making something right now - wait for it to finish or cancel it first.", 409)
    if inst.is_up():
        inst._stop()
    audit_log(user, action="lanes.stop", resource=name, result="allow")
    return {"ok": True, **_view(user)}

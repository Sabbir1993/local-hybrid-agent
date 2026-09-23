"""
routes/plugins.py - Plugin catalog: browse the curated plugin_catalog/, install /
uninstall a plugin, enable/disable one, hot-reload. Plugins run as in-process
Python shared by every user, so every mutation needs settings.orchestration.configure
and is audit-logged. See core/plugins.py.
"""

import json

from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from core.config import CONFIG_FILE
from core import plugins as plugins_core
from core.audit import audit_log
from core.auth import Principal
from core.deps import require_permission

_PERM = "settings.orchestration.configure"
_manage = require_permission(_PERM)

router = APIRouter(prefix="/plugins", tags=["plugins"])


class EnableReq(BaseModel):
    enabled: bool


def _err(msg: str, code: int = 400) -> JSONResponse:
    return JSONResponse({"error": msg}, status_code=code)


def _set_disabled(name: str, disabled: bool) -> None:
    """Persist capabilities.plugins_disabled to config/app.json and the live config."""
    cfg = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
    caps = cfg.setdefault("capabilities", {})
    cur = [n for n in (caps.get("plugins_disabled") or []) if n != name]
    if disabled:
        cur.append(name)
    caps["plugins_disabled"] = sorted(cur)
    CONFIG_FILE.write_text(json.dumps(cfg, indent=2), encoding="utf-8")
    plugins_core._caps()["plugins_disabled"] = caps["plugins_disabled"]


def _entry(name: str):
    return next((p for p in plugins_core.plugins_status() if p["name"] == name), None)


@router.get("/catalog")
async def catalog():
    installed = {p["name"]: p for p in plugins_core.plugins_status()}
    items = []
    for e in plugins_core.catalog_entries():
        inst = installed.get(e["name"])
        items.append({**e,
                      "installed": inst is not None,
                      "modified": bool(inst and inst["modified"]),
                      "loaded": bool(inst and inst["loaded"])})
    return {"enabled": plugins_core.plugins_enabled(), "plugins": items}


@router.post("/{name}/install")
async def install(name: str, user: Principal = Depends(_manage)):
    if not plugins_core.valid_name(name):
        return _err("invalid plugin name")
    try:
        plugins_core.install_from_catalog(name)
    except ValueError as e:
        return _err(str(e))
    except OSError as e:
        return _err(f"install failed: {e}", 500)
    # a fresh install starts enabled
    if name in plugins_core.disabled_names():
        _set_disabled(name, False)
    if plugins_core.plugins_enabled():
        plugins_core.load_plugin(name)
    audit_log(user, action="plugins.install", resource=name, permission_key=_PERM)
    return {"ok": True, "plugin": _entry(name)}


@router.delete("/{name}")
async def uninstall(name: str, user: Principal = Depends(_manage)):
    if not plugins_core.valid_name(name):
        return _err("invalid plugin name")
    try:
        plugins_core.uninstall(name)
    except ValueError as e:
        return _err(str(e))
    except OSError as e:
        return _err(f"uninstall failed: {e}", 500)
    if name in plugins_core.disabled_names():
        _set_disabled(name, False)
    audit_log(user, action="plugins.uninstall", resource=name, permission_key=_PERM)
    return {"ok": True}


@router.post("/{name}/enable")
async def enable(name: str, req: EnableReq, user: Principal = Depends(_manage)):
    if not plugins_core.is_installed(name):
        return _err(f"'{name}' is not installed", 404)
    try:
        _set_disabled(name, not req.enabled)
    except (OSError, ValueError) as e:
        return _err(f"config/app.json update failed: {e}", 500)
    if req.enabled and plugins_core.plugins_enabled():
        plugins_core.load_plugin(name)
    elif not req.enabled:
        plugins_core.unload_plugin(name)
    audit_log(user, action="plugins.enable", resource=name, permission_key=_PERM,
              detail={"enabled": req.enabled})
    return {"ok": True, "plugin": _entry(name)}


@router.post("/reload")
async def reload_all(user: Principal = Depends(_manage)):
    """Re-import every enabled plugin (picks up edits to plugins/*/plugin.py)."""
    plugins_core.unload_all()
    plugins_core.load_plugins()
    audit_log(user, action="plugins.reload", permission_key=_PERM)
    return {"ok": True, "plugins": plugins_core.plugins_status()}

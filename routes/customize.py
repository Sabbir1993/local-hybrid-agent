"""
routes/customize.py - Customize page: one browse/install surface over the three
capability catalogs.

  skills      skill_catalog/      -> .agents/skills/     (core/skills.py)
  plugins     plugin_catalog/     -> plugins/            (core/plugins.py)
  connectors  mcp_catalog.PRESETS -> capabilities.mcp_servers (routes/mcp_manager.py)

Everything installable is reviewed, in-repo content - nothing is fetched from the
internet on install. The public MCP Registry is proxied browse-only. Browsing needs
a session; every install/uninstall needs capabilities.install and is audit-logged.
"""

from typing import Optional

from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from core.small_model import APP_CONFIG
from core import credentials, mcp_catalog
from core import mcp as mcp_core
from core import plugins as plugins_core
from core import skills as skills_core
from core.audit import audit_log
from core.auth import Principal
from core.deps import require_permission
from routes import mcp_manager
from routes.plugins import _set_disabled as _set_plugin_disabled

_PERM = "capabilities.install"
_manage = require_permission(_PERM)

KINDS = ("skills", "plugins", "connectors")
MAX_PREVIEW_CHARS = 20000

router = APIRouter(prefix="/customize", tags=["customize"])


class InstallReq(BaseModel):
    secrets: dict = {}        # connectors only: {ENV_KEY: value} -> OS keychain


def _err(msg: str, code: int = 400) -> JSONResponse:
    return JSONResponse({"error": msg}, status_code=code)


def _caps() -> dict:
    return APP_CONFIG.get("capabilities", {}) or {}


# ---------------- listing ----------------

def _skills_items() -> list:
    catalog = {e["name"]: e for e in skills_core.catalog_entries()}
    installed = skills_core.load_skills() if skills_core.SKILLS_DIR.is_dir() else {}
    items = []
    for name in sorted(set(catalog) | set(installed)):
        cat, inst = catalog.get(name), installed.get(name)
        src = cat or inst
        modified = bool(inst) and skills_core.is_modified(name)
        items.append({
            "id": name,
            "title": (cat or {}).get("title") or (inst or {}).get("title") or src["name"],
            "description": src["description"],
            "category": src["category"],
            "author": src["author"] or ("local" if not cat else ""),
            "version": src.get("version", ""),
            "in_catalog": cat is not None,
            "installed": inst is not None,
            "modified": bool(cat) and modified,
            "can_uninstall": bool(inst) and bool(cat) and not modified,
            "triggers": (cat or inst)["triggers"][:10],
        })
    return items


def _plugins_items() -> list:
    catalog = {e["name"]: e for e in plugins_core.catalog_entries()}
    installed = {p["name"]: p for p in plugins_core.plugins_status()}
    items = []
    for name in sorted(set(catalog) | set(installed)):
        cat, inst = catalog.get(name), installed.get(name)
        items.append({
            "id": name,
            "title": (cat or inst)["title"],
            "description": (cat or inst)["description"],
            "category": (cat or {}).get("category") or "general",
            "author": (cat or {}).get("author") or ("local" if not cat else ""),
            "version": (cat or {}).get("version", ""),
            "in_catalog": cat is not None,
            "installed": inst is not None,
            "modified": bool(inst and inst["modified"]),
            "can_uninstall": bool(inst and inst["from_catalog"] and not inst["modified"]),
            "tools": (inst["tools"] if inst and inst["tools"] else (cat or {}).get("tools", [])),
            "loaded": bool(inst and inst["loaded"]),
            "enabled": bool(inst and inst["enabled"]),
            "error": inst.get("error") if inst else None,
        })
    return items


def _connectors_items() -> list:
    presets = {p["id"]: p for p in mcp_catalog.PRESETS}
    configured = mcp_core.configured_servers(APP_CONFIG)
    live = {s["name"]: s for s in mcp_core.mcp_status()}
    items = []
    for sid in sorted(set(presets) | set(configured)):
        pre, scfg = presets.get(sid), configured.get(sid)
        st = live.get(sid) or {}
        items.append({
            "id": sid,
            "title": pre["name"] if pre else sid,
            "description": pre["description"] if pre else (
                f"Custom server: {scfg.get('command') or scfg.get('url') or ''} "
                f"{' '.join(str(a) for a in scfg.get('args') or [])}".strip()),
            "category": pre["category"] if pre else "custom",
            "author": pre["author"] if pre else "local",
            "homepage": pre.get("homepage") if pre else None,
            "in_catalog": pre is not None,
            "installed": scfg is not None,
            "modified": False,
            # custom (hand-added) servers are managed in Settings -> Capabilities -> MCP
            "can_uninstall": scfg is not None and pre is not None,
            "egress": bool(pre and pre.get("egress")),
            "secret_env": (pre or {}).get("secret_env", []),
            "command": " ".join([pre["command"], *pre.get("args", [])]) if pre else None,
            "status": st.get("status") or ("disabled" if scfg and scfg.get("disabled") else
                                           ("configured" if scfg else None)),
            "error": st.get("error"),
            "tools": [t["name"] for t in st.get("tools") or []],
        })
    return items


_LISTERS = {"skills": _skills_items, "plugins": _plugins_items, "connectors": _connectors_items}
_ENABLED_KEY = {"skills": "skills", "plugins": "plugins", "connectors": "mcp"}


@router.get("/connectors/registry")
async def registry_search(q: Optional[str] = None):
    """Browse-only public MCP Registry lookup, normalized for cards. No install path."""
    raw = await mcp_catalog.fetch_public_registry((q or "").strip()[:100] or None)
    if raw.get("error"):
        return {"error": raw["error"], "servers": []}
    out = []
    for row in (raw.get("servers") or [])[:60]:
        s = row.get("server", row) if isinstance(row, dict) else {}
        if not isinstance(s, dict) or not s.get("name"):
            continue
        repo = s.get("repository") or {}
        out.append({
            "name": str(s["name"])[:120],
            "title": str(s.get("title") or s["name"].split("/")[-1])[:80],
            "description": str(s.get("description") or "")[:300],
            "version": str(s.get("version") or "")[:20],
            "url": str(repo.get("url") or s.get("websiteUrl") or "")[:300] or None,
        })
    return {"servers": out}


@router.get("/{kind}")
async def list_kind(kind: str):
    if kind not in KINDS:
        return _err(f"unknown kind '{kind}'", 404)
    items = _LISTERS[kind]()
    categories: dict = {}
    for it in items:
        if it["in_catalog"]:
            categories[it["category"]] = categories.get(it["category"], 0) + 1
    return {"kind": kind, "enabled": bool(_caps().get(_ENABLED_KEY[kind])),
            "items": items, "categories": categories}


@router.get("/{kind}/{item_id}/preview")
async def preview(kind: str, item_id: str):
    """Read-only source so an admin can review exactly what an install would add."""
    if kind == "skills":
        if not skills_core.valid_name(item_id):
            return _err("invalid skill name")
        for base in (skills_core.CATALOG_DIR, skills_core.SKILLS_DIR):
            p = base / item_id / "SKILL.md"
            if p.is_file():
                return {"format": "markdown", "text": p.read_text(encoding="utf-8", errors="replace")[:MAX_PREVIEW_CHARS]}
    elif kind == "plugins":
        if not plugins_core.valid_name(item_id):
            return _err("invalid plugin name")
        for base in (plugins_core.CATALOG_DIR, plugins_core.PLUGINS_DIR):
            p = base / item_id / "plugin.py"
            if p.is_file():
                return {"format": "python", "text": p.read_text(encoding="utf-8", errors="replace")[:MAX_PREVIEW_CHARS]}
    elif kind == "connectors":
        pre = mcp_catalog.get_preset(item_id)
        if pre:
            return {"format": "text", "text": " ".join([pre["command"], *pre.get("args", [])])}
    else:
        return _err(f"unknown kind '{kind}'", 404)
    return _err(f"'{item_id}' not found", 404)


# ---------------- install / uninstall ----------------

def _item(kind: str, item_id: str) -> Optional[dict]:
    return next((i for i in _LISTERS[kind]() if i["id"] == item_id), None)


@router.post("/{kind}/{item_id}/install")
async def install(kind: str, item_id: str, req: Optional[InstallReq] = None,
                  user: Principal = Depends(_manage)):
    req = req or InstallReq()
    detail: dict = {}
    try:
        if kind == "skills":
            if not skills_core.valid_name(item_id):
                return _err("invalid skill name")
            skills_core.install_from_catalog(item_id)
        elif kind == "plugins":
            if not plugins_core.valid_name(item_id):
                return _err("invalid plugin name")
            plugins_core.install_from_catalog(item_id)
            if item_id in plugins_core.disabled_names():
                _set_plugin_disabled(item_id, False)
            if plugins_core.plugins_enabled():
                plugins_core.load_plugin(item_id)
        elif kind == "connectors":
            res = await _install_connector(item_id, req, user)
            if isinstance(res, JSONResponse):
                return res
            detail = res
        else:
            return _err(f"unknown kind '{kind}'", 404)
    except ValueError as e:
        return _err(str(e))
    except OSError as e:
        return _err(f"install failed: {e}", 500)
    audit_log(user, action=f"customize.{kind}.install", resource=item_id, permission_key=_PERM,
              detail=detail or None)
    return {"ok": True, "item": _item(kind, item_id)}


async def _install_connector(item_id: str, req: InstallReq, user: Principal):
    pre = mcp_catalog.get_preset(item_id)
    if not pre:
        return _err(f"'{item_id}' is not in the connector catalog", 404)
    if item_id in mcp_core.configured_servers(APP_CONFIG):
        return _err(f"connector '{item_id}' is already installed", 409)
    wanted = [s["key"] for s in pre.get("secret_env", [])]
    missing = [k for k in wanted if not str(req.secrets.get(k) or "").strip()]
    if missing:
        return _err(f"missing required value(s): {', '.join(missing)}")
    sreq = mcp_manager.McpServerReq(
        name=item_id, transport=pre["transport"], command=pre.get("command"),
        args=list(pre.get("args", [])), url=pre.get("url"),
        # only the preset's declared keys are accepted - no arbitrary env injection
        secret_env={k: str(req.secrets[k]).strip() for k in wanted})
    err = mcp_manager._validate(sreq)
    if err:
        return _err(err)
    # _save_server stores secrets in the keychain, writes app.json, audits and connects
    saved = await mcp_manager._save_server(sreq, user, "mcp.server.add", [])
    return {"command": sreq.command, "secret_env_keys": wanted, "status": saved["status"]["status"]}


@router.delete("/{kind}/{item_id}")
async def uninstall(kind: str, item_id: str, user: Principal = Depends(_manage)):
    try:
        if kind == "skills":
            if not skills_core.valid_name(item_id):
                return _err("invalid skill name")
            skills_core.uninstall(item_id)
        elif kind == "plugins":
            if not plugins_core.valid_name(item_id):
                return _err("invalid plugin name")
            plugins_core.uninstall(item_id)
            if item_id in plugins_core.disabled_names():
                _set_plugin_disabled(item_id, False)
        elif kind == "connectors":
            if not mcp_catalog.get_preset(item_id):
                return _err("custom servers are managed in Settings → Capabilities → MCP")
            scfg = mcp_core.configured_servers(APP_CONFIG).get(item_id)
            if scfg is None:
                return _err(f"'{item_id}' is not installed")
            mcp_core.disconnect_one(item_id)
            for k in scfg.get("secret_env_keys") or []:
                credentials.delete_token(mcp_core.secret_env_ref(item_id, k))
            mcp_manager._write_server(item_id, None)
        else:
            return _err(f"unknown kind '{kind}'", 404)
    except ValueError as e:
        return _err(str(e))
    except OSError as e:
        return _err(f"uninstall failed: {e}", 500)
    audit_log(user, action=f"customize.{kind}.uninstall", resource=item_id, permission_key=_PERM)
    return {"ok": True}

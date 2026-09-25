"""
routes/customize.py - Customize page: one browse/install surface over the three
capability catalogs.

  skills      skill_catalog/      -> .agents/skills/     (core/skills.py)
  plugins     plugin_catalog/     -> plugins/            (core/plugins.py)
  connectors  mcp_catalog.PRESETS -> capabilities.mcp_servers (routes/mcp_manager.py)

Everything installable from the in-repo catalogs is reviewed, in-repo content.
The public MCP Registry is proxied browse-only. The Marketplace tab browses an
external plugin registry and installs plugins via URL: every fetch goes through
the SSRF guard (core.net_guard.guarded_get), the manifest + code preview must be
reviewed first, and installs need capabilities.install and are audit-logged.
Browsing needs a session; every install/uninstall needs capabilities.install
and is audit-logged.
"""

from typing import Optional

import asyncio
import hashlib
import json

from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from core.net_guard import BlockedURLError, guarded_get
from core.small_model import APP_CONFIG
from core import credentials, mcp_catalog
from core import mcp as mcp_core
from core import plugins as plugins_core
from core import skills as skills_core
from core.audit import audit_log
from core.auth import Principal
from core.deps import get_current_user, require_permission
from routes import mcp_manager
from routes.plugins import _set_disabled as _set_plugin_disabled

_PERM = "capabilities.install"
_manage = require_permission(_PERM)

KINDS = ("skills", "plugins", "connectors")
MAX_PREVIEW_CHARS = 20000

# Remote plugin registry + URL installs.
# An operator can point this at any JSON registry of the form
# {"plugins": [{"name": ..., "manifest_url": ..., "code_url": ..., ...}]}.
# Every URL fetch below goes through core.net_guard.guarded_get (SSRF guard,
# private-network block, capped body). Nothing is written to plugins/ until the
# user reviews the manifest + code preview and confirms install.
DEFAULT_MARKETPLACE_URL = (
    "https://raw.githubusercontent.com/Sabbir1993/local-hybrid-agent/main/plugin_registry.json"
)
MARKETPLACE_TIMEOUT_S = 15
REMOTE_MAX_MANIFEST_BYTES = 64 * 1024
REMOTE_MAX_CODE_BYTES = 512 * 1024
REMOTE_PREVIEW_CHARS = 12000

router = APIRouter(prefix="/customize", tags=["customize"])


class InstallReq(BaseModel):
    secrets: dict = {}        # connectors only: {ENV_KEY: value} -> OS keychain


class RemoteInspectReq(BaseModel):
    manifest_url: str = ""
    code_url: str = ""          # optional override; else taken from manifest's code_url/source_url


class RemoteInstallReq(BaseModel):
    manifest_url: str = ""
    code_url: str = ""          # optional override
    name: str = ""              # optional override (must match manifest name when both given)


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


# ---------------- remote marketplace (plugins via URL) ----------------

def _marketplace_url() -> str:
    return str((_caps().get("plugin_marketplace_url") or DEFAULT_MARKETPLACE_URL)).strip() \
        or DEFAULT_MARKETPLACE_URL


async def _fetch_url(url: str, max_bytes: int):
    try:
        return await asyncio.to_thread(
            guarded_get, url, MARKETPLACE_TIMEOUT_S,
            {"User-Agent": "local-hybrid-agent/1.0"}, max_bytes)
    except BlockedURLError as e:
        raise ValueError(f"blocked URL: {e}")


def _normalize_registry_entry(e: dict) -> Optional[dict]:
    if not isinstance(e, dict):
        return None
    manifest_url = str(e.get("manifest_url") or e.get("plugin_url") or "").strip()
    code_url = str(e.get("code_url") or e.get("source_url") or "").strip()
    name = str(e.get("name") or "").strip()
    if not (manifest_url or code_url):
        return None
    if name and not plugins_core.valid_name(name):
        return None
    return {"name": name,
            "title": str(e.get("title") or name or manifest_url or code_url)[:80],
            "description": str(e.get("description") or "")[:400],
            "version": str(e.get("version") or "")[:20],
            "author": str(e.get("author") or "")[:80],
            "category": str(e.get("category") or "general")[:40],
            "manifest_url": manifest_url, "code_url": code_url,
            "homepage": str(e.get("homepage") or "")[:200]}


def _validate_manifest_dict(m: dict) -> dict:
    if not isinstance(m, dict):
        raise ValueError("manifest must be a JSON object")
    name = str(m.get("name") or "").strip()
    if not plugins_core.valid_name(name):
        raise ValueError("manifest 'name' must match ^[a-z0-9][a-z0-9_-]{0,40}$")
    code_url = str(m.get("code_url") or m.get("source_url") or "").strip()
    if code_url:
        from core.net_guard import check_url
        try:
            check_url(code_url)
        except BlockedURLError as e:
            raise ValueError(f"manifest code_url blocked: {e}")
    tools = m.get("tools") or []
    if not isinstance(tools, list):
        raise ValueError("manifest 'tools' must be a list")
    return {"name": name,
            "title": str(m.get("title") or name)[:80],
            "description": str(m.get("description") or "")[:400],
            "version": str(m.get("version") or "")[:20],
            "author": str(m.get("author") or "")[:80],
            "category": str(m.get("category") or "general")[:40],
            "tools": [str(t)[:60] for t in tools][:20],
            "code_url": code_url,
            "homepage": str(m.get("homepage") or "")[:200]}


async def _fetch_manifest_and_code(manifest_url: str, code_url_override: str = "") -> dict:
    from core.net_guard import check_url
    manifest_url = (manifest_url or "").strip()
    if not manifest_url:
        raise ValueError("manifest_url required")
    try:
        check_url(manifest_url)
    except BlockedURLError as e:
        raise ValueError(f"blocked URL: {e}")
    try:
        mresp = await _fetch_url(manifest_url, REMOTE_MAX_MANIFEST_BYTES)
    except ValueError as e:
        raise e
    if mresp.status_code >= 400:
        raise ValueError(f"manifest fetch failed: HTTP {mresp.status_code}")
    if mresp.truncated:
        raise ValueError("manifest too large (> 64 KiB)")
    try:
        manifest = json.loads(mresp.text)
    except ValueError:
        raise ValueError("manifest is not valid JSON")
    info = _validate_manifest_dict(manifest)
    code_url = (code_url_override or "").strip() or info["code_url"]
    code_text, code_sha256, code_truncated = "", None, False
    if code_url:
        try:
            check_url(code_url)
        except BlockedURLError as e:
            raise ValueError(f"blocked URL: {e}")
        try:
            cresp = await _fetch_url(code_url, REMOTE_MAX_CODE_BYTES)
        except ValueError as e:
            raise e
        if cresp.status_code >= 400:
            raise ValueError(f"code fetch failed: HTTP {cresp.status_code}")
        code_text = cresp.content.decode(cresp.encoding or "utf-8", errors="replace")
        code_sha256 = hashlib.sha256(cresp.content).hexdigest()
        code_truncated = bool(cresp.truncated)
    return {"manifest": info, "manifest_url": manifest_url, "code_url": code_url,
            "code_text": code_text, "code_sha256": code_sha256,
            "code_truncated": code_truncated}


@router.get("/plugins/registry")
async def plugin_registry(q: Optional[str] = None,
                          url: Optional[str] = None,
                          user: Principal = Depends(get_current_user)):
    target = (url or "").strip() or _marketplace_url()
    try:
        from core.net_guard import check_url
        check_url(target)
    except BlockedURLError as e:
        return _err(f"blocked URL: {e}")
    try:
        resp = await _fetch_url(target, REMOTE_MAX_CODE_BYTES)
    except ValueError as e:
        return _err(str(e))
    if resp.status_code >= 400:
        return _err(f"registry fetch failed: HTTP {resp.status_code}", 502)
    try:
        raw = json.loads(resp.text)
    except ValueError:
        return _err("registry did not return valid JSON", 502)
    entries = raw.get("plugins") if isinstance(raw, dict) else raw
    if not isinstance(entries, list):
        return _err('registry JSON must be {"plugins": [...]}', 502)
    items = [n for n in (_normalize_registry_entry(x) for x in entries) if n]
    needle = (q or "").strip().lower()
    if needle:
        items = [i for i in items
                 if needle in " ".join(str(i.get(k) or "") for k in
                                       ("name", "title", "description", "author", "category")).lower()]
    installed = {p["name"] for p in plugins_core.plugins_status()}
    for i in items:
        i["installed"] = bool(i["name"] and i["name"] in installed)
    return {"registry_url": target, "count": len(items), "items": items[:200]}


@router.post("/plugins/inspect-remote")
async def inspect_remote_plugin(req: RemoteInspectReq,
                                user: Principal = Depends(get_current_user)):
    try:
        data = await _fetch_manifest_and_code(req.manifest_url, req.code_url)
    except ValueError as e:
        return _err(str(e))
    import ast
    if data.get("code_text"):
        try:
            ast.parse(data["code_text"])
            data["code_syntax"] = "ok"
        except SyntaxError as e:
            data["code_syntax"] = f"syntax error: {e}"
    else:
        data["code_syntax"] = "no-code-url" if not data.get("code_url") else "empty"
    data["code_preview"] = (data.pop("code_text") or "")[:REMOTE_PREVIEW_CHARS]
    data["name_taken"] = plugins_core.is_installed(data["manifest"]["name"])
    return data


@router.post("/plugins/install-remote")
async def install_remote_plugin(req: RemoteInstallReq, user: Principal = Depends(_manage)):
    name_override = (req.name or "").strip()
    if name_override and not plugins_core.valid_name(name_override):
        return _err("invalid plugin name")
    try:
        data = await _fetch_manifest_and_code(req.manifest_url, req.code_url)
    except ValueError as e:
        return _err(str(e))
    manifest = data["manifest"]
    name = manifest["name"]
    if name_override and name_override != name:
        return _err(f"name override '{name_override}' does not match manifest name '{name}'")
    code_text = data.get("code_text") or ""
    if not data.get("code_url"):
        return _err("manifest has no code_url/source_url - nothing to install")
    if data.get("code_truncated"):
        return _err("remote code too large (> 512 KiB)")
    if plugins_core.is_installed(name) and not plugins_core.is_modified(name):
        return _err(f"plugin '{name}' is already installed", 409)
    if plugins_core.is_installed(name) and plugins_core.is_modified(name):
        return _err(f"plugins/{name} already exists with local changes - remove it manually first", 409)
    try:
        compile(code_text, f"<remote {name}/plugin.py>", "exec")
    except SyntaxError as e:
        return _err(f"remote code has a syntax error: {e}")
    try:
        dest = plugins_core.PLUGINS_DIR / name
        dest.mkdir(parents=True, exist_ok=True)
        (dest / "plugin.py").write_text(code_text, encoding="utf-8")
        (dest / "plugin.json").write_text(json.dumps(
            {"title": manifest["title"], "description": manifest["description"],
             "version": manifest["version"], "author": manifest["author"],
             "category": manifest["category"], "tools": manifest["tools"]}, indent=2),
            encoding="utf-8")
        (dest / ".remote_source").write_text(json.dumps(
            {"manifest_url": data["manifest_url"], "code_url": data["code_url"],
             "sha256": data["code_sha256"]}, indent=2), encoding="utf-8")
        if name in plugins_core.disabled_names():
            _set_plugin_disabled(name, False)
        if plugins_core.plugins_enabled():
            plugins_core.load_plugin(name)
    except OSError as e:
        return _err(f"install failed: {e}", 500)
    audit_log(user, action="customize.plugins.install-remote", resource=name,
              permission_key=_PERM,
              detail={"manifest_url": data["manifest_url"], "code_url": data["code_url"],
                      "sha256": data["code_sha256"]})
    api = plugins_core._loaded.get(name)
    return {"ok": True, "name": name, "loaded": api is not None,
            "sha256": data["code_sha256"], "error": plugins_core._errors.get(name)}


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

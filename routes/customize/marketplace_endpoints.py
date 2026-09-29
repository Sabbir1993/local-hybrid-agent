from typing import Optional
import hmac
import json
from fastapi import Depends
from core.net_guard import BlockedURLError
from core import plugins as plugins_core
from core.audit import audit_log
from core.auth import Principal
from core.deps import get_current_user
from routes.plugins import _set_disabled as _set_plugin_disabled

from .base import (
    LOCAL_REGISTRY_FILE,
    REMOTE_MAX_CODE_BYTES,
    REMOTE_PREVIEW_CHARS,
    RemoteInspectReq,
    RemoteInstallReq,
    _PERM,
    _err,
    _manage,
    router,
)
from .marketplace import (
    _fetch_manifest_and_code,
    _fetch_url,
    _marketplace_url,
    _normalize_registry_entry,
)


@router.get("/plugins/registry")
async def plugin_registry(q: Optional[str] = None,
                          user: Principal = Depends(get_current_user)):
    # No per-request URL override: users can't point the server at a registry of
    # their choosing; only the admin-set config value (or the local file) is used.
    target = _marketplace_url()
    if not target:
        try:
            raw = json.loads(LOCAL_REGISTRY_FILE.read_text(encoding="utf-8"))
        except FileNotFoundError:
            raw = {"plugins": []}
        except ValueError:
            return _err("plugin_registry.json is not valid JSON", 500)
    else:
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
    # The code is fetched again here; without this pin the server could serve
    # different code to install than it showed in the review.
    if not req.code_sha256:
        return _err("code_sha256 from the reviewed preview is required")
    if not hmac.compare_digest(req.code_sha256.lower(), str(data.get("code_sha256") or "")):
        return _err("remote code changed since it was reviewed - inspect it again", 409)
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

from typing import Optional
import asyncio
import hashlib
import json
from core.net_guard import BlockedURLError, guarded_get
from core import plugins as plugins_core

from .base import MARKETPLACE_TIMEOUT_S, REMOTE_MAX_CODE_BYTES, REMOTE_MAX_MANIFEST_BYTES, _caps


def _marketplace_url() -> str:
    """Admin-configured remote registry, or "" for the local plugin_registry.json."""
    return str(_caps().get("plugin_marketplace_url") or "").strip()


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

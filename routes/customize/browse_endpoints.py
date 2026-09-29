from typing import Optional
from core import mcp_catalog
from core import plugins as plugins_core
from core import skills as skills_core

from .base import KINDS, MAX_PREVIEW_CHARS, _caps, _err, router
from .listing import _ENABLED_KEY, _LISTERS


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

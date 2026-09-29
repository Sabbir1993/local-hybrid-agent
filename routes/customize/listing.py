from typing import Optional
from core.small_model import APP_CONFIG
from core import mcp_catalog
from core import mcp as mcp_core
from core import plugins as plugins_core
from core import skills as skills_core


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


def _item(kind: str, item_id: str) -> Optional[dict]:
    return next((i for i in _LISTERS[kind]() if i["id"] == item_id), None)

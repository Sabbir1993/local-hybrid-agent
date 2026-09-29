import hashlib
import json
from pathlib import Path
import shutil
from typing import Optional
from ..registry import registry
from .constants import (
    _get_catalog_dir,
    _get_plugins_dir,
    disabled_names,
    valid_name,
)
from .manager import _errors, _loaded, unload_plugin


def _sha256(path: Path) -> Optional[str]:
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError:
        return None


def _read_manifest(d: Path) -> dict:
    try:
        m = json.loads((d / "plugin.json").read_text(encoding="utf-8"))
        return m if isinstance(m, dict) else {}
    except (OSError, ValueError):
        return {}


def catalog_entries() -> list:
    """Curated plugins shipped in plugin_catalog/ (only dirs with plugin.py)."""
    out = []
    catalog_dir = _get_catalog_dir()
    if not catalog_dir.is_dir():
        return out
    for d in sorted(catalog_dir.iterdir()):
        if not (d.is_dir() and valid_name(d.name) and (d / "plugin.py").is_file()):
            continue
        m = _read_manifest(d)
        out.append({
            "name": d.name,
            "title": str(m.get("title") or d.name)[:80],
            "description": str(m.get("description") or "")[:400],
            "version": str(m.get("version") or "")[:20],
            "author": str(m.get("author") or "")[:80],
            "category": str(m.get("category") or "general")[:40],
            "tools": [str(t)[:60] for t in (m.get("tools") or [])][:20],
            "sha256": _sha256(d / "plugin.py"),
        })
    return out


def _catalog_dir(name: str) -> Optional[Path]:
    d = _get_catalog_dir() / name
    return d if valid_name(name) and (d / "plugin.py").is_file() else None


def is_installed(name: str) -> bool:
    return valid_name(name) and (_get_plugins_dir() / name / "plugin.py").is_file()


def is_modified(name: str) -> bool:
    """True if the installed plugin.py differs from its catalog copy (or has none)."""
    src = _catalog_dir(name)
    if not src:
        return True
    return _sha256(src / "plugin.py") != _sha256(_get_plugins_dir() / name / "plugin.py")


def install_from_catalog(name: str) -> None:
    """Copy plugin_catalog/<name> into plugins/<name>. Raises ValueError on refusal."""
    src = _catalog_dir(name)
    if not src:
        raise ValueError(f"'{name}' is not in the plugin catalog")
    plugins_dir = _get_plugins_dir()
    dst = plugins_dir / name
    if dst.exists():
        if not is_modified(name):
            return  # already installed, identical
        raise ValueError(f"plugins/{name} already exists with local changes — remove it manually first")
    plugins_dir.mkdir(parents=True, exist_ok=True)
    shutil.copytree(src, dst, ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))


def uninstall(name: str) -> None:
    """Remove plugins/<name>. Only catalog plugins with no local edits can be removed
    here (they can always be reinstalled); hand-written plugins stay on disk."""
    if not is_installed(name):
        raise ValueError(f"'{name}' is not installed")
    if is_modified(name):
        raise ValueError(f"'{name}' is hand-written or locally modified — remove plugins/{name} manually")
    unload_plugin(name)
    shutil.rmtree(_get_plugins_dir() / name)


def plugins_status() -> list:
    """Every installed plugin (loaded, disabled or broken)."""
    out = []
    skip = disabled_names()
    catalog = {e["name"] for e in catalog_entries()}
    names = set(_loaded)
    plugins_dir = _get_plugins_dir()
    if plugins_dir.is_dir():
        names |= {d.name for d in plugins_dir.iterdir()
                  if d.is_dir() and valid_name(d.name) and (d / "plugin.py").is_file()}
    for name in sorted(names):
        api = _loaded.get(name)
        m = _read_manifest(plugins_dir / name)
        out.append({
            "name": name,
            "title": str(m.get("title") or name)[:80],
            "description": str(m.get("description") or "")[:400],
            "loaded": api is not None,
            "enabled": name not in skip,
            "error": _errors.get(name),
            "from_catalog": name in catalog,
            "modified": name in catalog and is_modified(name),
            "tools": [t.name for t in registry.list(source=f"plugin:{name}")],
            "prompt_fragments": len(api.prompt_fragments) if api else 0,
        })
    return out

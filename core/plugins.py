"""Plugins capability: plugins/<name>/plugin.py with register(registry_api).

Each plugin module gets a PluginAPI object:
  api.add_tool(name, fn, description, parameters)     -> registers plugin:<name> tool
  api.add_system_prompt(fragment)                     -> appended to agent system prompt
  api.on_event(hook, fn)                              -> before_tool | after_tool | session_start
Failures are isolated: one broken plugin never breaks the server.

Catalog: plugin_catalog/<name>/{plugin.py, plugin.json} is the curated, in-repo
(code-reviewed) set the Settings UI can browse and install. Installing copies the
bundle into plugins/<name>/ and hot-loads it. There is deliberately no remote
download: plugins run as in-process Python, so only reviewed code gets installed.
Per-plugin disable list lives in capabilities.plugins_disabled (config/app.json).
"""

import hashlib
import importlib.util
import json
import re
import shutil
import sys
import traceback
from pathlib import Path
from typing import Callable, Optional

from .config import BASE_DIR
from .registry import registry

PLUGINS_DIR = BASE_DIR / "plugins"
CATALOG_DIR = BASE_DIR / "plugin_catalog"

MAX_PROMPT_FRAG_CHARS = 1500

NAME_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,40}$")


class PluginAPI:
    """What a plugin.py gets to interact with."""

    def __init__(self, name: str):
        self.name = name
        self.prompt_fragments: list[str] = []
        self.hooks: dict[str, list[Callable]] = {"before_tool": [], "after_tool": [], "session_start": []}

    def add_tool(self, name: str, fn: Callable, description: str = "", parameters: Optional[dict] = None) -> bool:
        if not name or fn is None:
            return False
        full = f"plugin__{self.name}__{name}"
        return registry.register(
            full, fn,
            {"type": "function", "function": {
                "name": full,
                "description": (f"[plugin:{self.name}] " + (description or name))[:400],
                "parameters": parameters or {"type": "object", "properties": {}},
            }},
            source=f"plugin:{self.name}",
            meta={"label": f"{self.name}/{name}"}, replace=True)

    def add_system_prompt(self, fragment: str) -> None:
        frag = str(fragment).strip()
        if frag and len(frag) > MAX_PROMPT_FRAG_CHARS:
            frag = frag[:MAX_PROMPT_FRAG_CHARS] + "…"
        if frag:
            self.prompt_fragments.append(frag)

    def on_event(self, hook: str, fn: Callable) -> None:
        if hook in self.hooks and callable(fn):
            self.hooks[hook].append(fn)


# loaded plugins: {name: PluginAPI}
_loaded: dict[str, PluginAPI] = {}
# last load error per plugin name (shown in the UI)
_errors: dict[str, str] = {}


def _caps() -> dict:
    from .small_model import APP_CONFIG
    return APP_CONFIG.setdefault("capabilities", {})


def plugins_enabled() -> bool:
    return bool(_caps().get("plugins", False))


def disabled_names() -> set:
    return set(_caps().get("plugins_disabled") or [])


def valid_name(name: str) -> bool:
    return bool(name) and bool(NAME_RE.match(name))


def _sha256(path: Path) -> Optional[str]:
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError:
        return None


def _load_one(plugin_dir: Path) -> Optional[PluginAPI]:
    mod_file = plugin_dir / "plugin.py"
    if not mod_file.is_file():
        return None
    name = plugin_dir.name
    api = PluginAPI(name)
    try:
        spec = importlib.util.spec_from_file_location(f"plugin_{name}", mod_file)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        reg = getattr(mod, "register", None)
        if not callable(reg):
            _errors[name] = "no register() function"
            print(f"[plugins] {name}: no register() function — skipped", file=sys.stderr)
            return None
        reg(api)
        _errors.pop(name, None)
        return api
    except Exception as e:
        # a half-registered plugin must not leave stray tools behind
        registry.unregister_source(f"plugin:{name}")
        _errors[name] = f"{type(e).__name__}: {e}"
        print(f"[plugins] {name} failed to load:\n{traceback.format_exc()}", file=sys.stderr)
        return None


def load_plugin(name: str) -> Optional[PluginAPI]:
    """(Re)load one installed plugin; replaces any previously loaded copy."""
    unload_plugin(name)
    d = PLUGINS_DIR / name
    if not valid_name(name) or not d.is_dir():
        return None
    api = _load_one(d)
    if api:
        _loaded[name] = api
        n_tools = len(registry.list(source=f"plugin:{name}"))
        print(f"[plugins] loaded '{name}' ({n_tools} tool(s), "
              f"{len(api.prompt_fragments)} prompt frag(s))")
    return api


def unload_plugin(name: str) -> None:
    """Drop a plugin's tools, prompt fragments and hooks from the running server."""
    _loaded.pop(name, None)
    registry.unregister_source(f"plugin:{name}")
    sys.modules.pop(f"plugin_{name}", None)


def load_plugins() -> dict:
    """Import every enabled plugins/<dir>/plugin.py; returns {name: api}."""
    if not plugins_enabled() or not PLUGINS_DIR.is_dir():
        return _loaded
    skip = disabled_names()
    for d in sorted(PLUGINS_DIR.iterdir()):
        if d.is_dir() and valid_name(d.name) and d.name not in skip:
            load_plugin(d.name)
    return _loaded


def unload_all() -> None:
    for name in list(_loaded):
        unload_plugin(name)


def plugins_prompt_fragment() -> str:
    frags = [f for api in _loaded.values() for f in api.prompt_fragments]
    return "\n\n".join(frags)


async def fire_hook(hook: str, *args) -> None:
    """Run every plugin's hook callbacks (errors isolated)."""
    for api in _loaded.values():
        for fn in api.hooks.get(hook, []):
            try:
                out = fn(*args)
                if hasattr(out, "__await__"):
                    await out
            except Exception:
                print(f"[plugins] hook error in {api.name}.{hook}:\n{traceback.format_exc()}",
                      file=sys.stderr)


# ---------------- catalog ----------------

def _read_manifest(d: Path) -> dict:
    try:
        m = json.loads((d / "plugin.json").read_text(encoding="utf-8"))
        return m if isinstance(m, dict) else {}
    except (OSError, ValueError):
        return {}


def catalog_entries() -> list:
    """Curated plugins shipped in plugin_catalog/ (only dirs with plugin.py)."""
    out = []
    if not CATALOG_DIR.is_dir():
        return out
    for d in sorted(CATALOG_DIR.iterdir()):
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
    d = CATALOG_DIR / name
    return d if valid_name(name) and (d / "plugin.py").is_file() else None


def is_installed(name: str) -> bool:
    return valid_name(name) and (PLUGINS_DIR / name / "plugin.py").is_file()


def is_modified(name: str) -> bool:
    """True if the installed plugin.py differs from its catalog copy (or has none)."""
    src = _catalog_dir(name)
    if not src:
        return True
    return _sha256(src / "plugin.py") != _sha256(PLUGINS_DIR / name / "plugin.py")


def install_from_catalog(name: str) -> None:
    """Copy plugin_catalog/<name> into plugins/<name>. Raises ValueError on refusal."""
    src = _catalog_dir(name)
    if not src:
        raise ValueError(f"'{name}' is not in the plugin catalog")
    dst = PLUGINS_DIR / name
    if dst.exists():
        if not is_modified(name):
            return  # already installed, identical
        raise ValueError(f"plugins/{name} already exists with local changes — remove it manually first")
    PLUGINS_DIR.mkdir(parents=True, exist_ok=True)
    shutil.copytree(src, dst, ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))


def uninstall(name: str) -> None:
    """Remove plugins/<name>. Only catalog plugins with no local edits can be removed
    here (they can always be reinstalled); hand-written plugins stay on disk."""
    if not is_installed(name):
        raise ValueError(f"'{name}' is not installed")
    if is_modified(name):
        raise ValueError(f"'{name}' is hand-written or locally modified — remove plugins/{name} manually")
    unload_plugin(name)
    shutil.rmtree(PLUGINS_DIR / name)


def plugins_status() -> list:
    """Every installed plugin (loaded, disabled or broken)."""
    out = []
    skip = disabled_names()
    catalog = {e["name"] for e in catalog_entries()}
    names = set(_loaded)
    if PLUGINS_DIR.is_dir():
        names |= {d.name for d in PLUGINS_DIR.iterdir()
                  if d.is_dir() and valid_name(d.name) and (d / "plugin.py").is_file()}
    for name in sorted(names):
        api = _loaded.get(name)
        m = _read_manifest(PLUGINS_DIR / name)
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

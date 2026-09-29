import importlib.util
from pathlib import Path
import sys
import traceback
from typing import Optional
from ..registry import registry
from .api import PluginAPI
from .constants import (
    _get_plugins_dir,
    disabled_names,
    plugins_enabled,
    valid_name,
)

# loaded plugins: {name: PluginAPI}
_loaded: dict[str, PluginAPI] = {}
# last load error per plugin name (shown in the UI)
_errors: dict[str, str] = {}


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
    d = _get_plugins_dir() / name
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
    plugins_dir = _get_plugins_dir()
    if not plugins_enabled() or not plugins_dir.is_dir():
        return _loaded
    skip = disabled_names()
    for d in sorted(plugins_dir.iterdir()):
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

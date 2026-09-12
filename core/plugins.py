"""Plugins capability: plugins/<name>/plugin.py with register(registry_api).

Each plugin module gets a PluginAPI object:
  api.add_tool(name, fn, description, parameters)     -> registers plugin:<name> tool
  api.add_system_prompt(fragment)                     -> appended to agent system prompt
  api.on_event(hook, fn)                              -> before_tool | after_tool | session_start
Failures are isolated: one broken plugin never breaks the server.
"""

import importlib.util
import sys
import traceback
from pathlib import Path
from typing import Callable, Optional

from .config import BASE_DIR
from .registry import registry

PLUGINS_DIR = BASE_DIR / "plugins"

MAX_PROMPT_FRAG_CHARS = 1500


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
            print(f"[plugins] {name}: no register() function — skipped", file=sys.stderr)
            return None
        reg(api)
        return api
    except Exception:
        print(f"[plugins] {name} failed to load:\n{traceback.format_exc()}", file=sys.stderr)
        return None


def load_plugins() -> dict:
    """Import every plugins/<dir>/plugin.py; returns {name: api}."""
    from .small_model import APP_CONFIG
    if not APP_CONFIG.get("capabilities", {}).get("plugins", False):
        return {}
    if not PLUGINS_DIR.is_dir():
        return {}
    for d in sorted(PLUGINS_DIR.iterdir()):
        if d.is_dir():
            api = _load_one(d)
            if api:
                _loaded[d.name] = api
                n_tools = len(registry.list(source=f"plugin:{d.name}"))
                print(f"[plugins] loaded '{d.name}' ({n_tools} tool(s), "
                      f"{len(api.prompt_fragments)} prompt frag(s))")
    return _loaded


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


def plugins_status() -> list:
    out = []
    for name, api in _loaded.items():
        out.append({
            "name": name,
            "tools": [t.name for t in registry.list(source=f"plugin:{name}")],
            "prompt_fragments": len(api.prompt_fragments),
        })
    return out

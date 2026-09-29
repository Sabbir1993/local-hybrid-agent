from typing import Callable, Optional
from ..registry import registry
from .constants import MAX_PROMPT_FRAG_CHARS


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

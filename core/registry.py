"""ToolRegistry: uniform registration surface for agent tools.

All tool sources (builtin, web, skills, mcp:<server>, plugin:<name>) register
here. The registry produces the OpenAI-shaped tool schema list plus the
name -> implementation lookup used by run_tool().

A tool registered with an owner (a user's personal MCP server) is only visible
to that user: get/list/schemas resolve against the calling request's user
(core/request_context.py). Global tools win on a name clash.
"""

import asyncio
import inspect
from typing import Callable, Optional

from .request_context import get_current_user_id, run_in_executor_ctx


class RegisteredTool:
    __slots__ = ("name", "fn", "schema", "source", "meta", "enabled", "owner")

    def __init__(self, name: str, fn: Callable, schema: dict, source: str, meta: dict, enabled: bool = True,
                 owner: Optional[int] = None):
        self.name = name
        self.fn = fn
        self.schema = schema          # {"type":"function","function":{...}} shape
        self.source = source          # "builtin" | "web" | "skill" | "mcp:<srv>" | "plugin:<name>"
        self.meta = meta or {}        # free-form: {"label":..., "description":...}
        self.enabled = enabled
        self.owner = owner            # user id for a personal tool, None = every user


class ToolRegistry:
    def __init__(self):
        self._tools: dict[str, RegisteredTool] = {}
        self._owned: dict[int, dict[str, RegisteredTool]] = {}   # user id -> personal tools

    def register(self, name: str, fn: Optional[Callable], schema: dict,
                 source: str = "builtin", meta: Optional[dict] = None,
                 replace: bool = False, owner: Optional[int] = None) -> bool:
        """Register (or re-register) a tool. Returns True if it took effect.
        fn=None is a deferred registration (e.g. MCP server still starting): schema only."""
        if not name or not isinstance(schema, dict):
            return False
        table = self._tools if owner is None else self._owned.setdefault(owner, {})
        if name in table and not replace:
            return False
        table[name] = RegisteredTool(name, fn, schema, source, meta, True, owner)
        return True

    def _visible(self) -> list:
        """Global tools plus the calling user's personal ones (global wins on a clash)."""
        out = list(self._tools.values())
        uid = get_current_user_id()
        if uid is not None:
            out += [t for n, t in self._owned.get(uid, {}).items() if n not in self._tools]
        return out

    def unregister(self, name: str) -> None:
        self._tools.pop(name, None)

    def set_enabled(self, name: str, enabled: bool) -> None:
        t = self._tools.get(name)
        if t:
            t.enabled = enabled

    def unregister_source(self, source: str) -> None:
        """Drop every tool from one source (e.g. all tools from a disconnected MCP server)."""
        for table in (self._tools, *self._owned.values()):
            for name in [n for n, t in table.items() if t.source == source]:
                table.pop(name, None)

    def set_source_enabled(self, source: str, enabled: bool) -> None:
        """Toggle every tool from one source (e.g. all 'web' tools)."""
        for table in (self._tools, *self._owned.values()):
            for t in table.values():
                if t.source == source:
                    t.enabled = enabled

    def get(self, name: str) -> Optional[RegisteredTool]:
        t = self._tools.get(name)
        if t is None:
            uid = get_current_user_id()
            t = self._owned.get(uid, {}).get(name) if uid is not None else None
        return t if (t and t.enabled) else None

    def sources(self) -> list:
        seen, out = set(), []
        for t in self._visible():
            if t.source not in seen:
                seen.add(t.source)
                out.append(t.source)
        return out

    def list(self, source: Optional[str] = None, include_disabled: bool = False) -> list:
        out = []
        for t in self._visible():
            if source and t.source != source:
                continue
            if not t.enabled and not include_disabled:
                continue
            out.append(t)
        return out

    def schemas(self) -> list:
        """OpenAI 'tools' array for the LLM request (enabled tools only)."""
        return [t.schema for t in self._visible() if t.enabled]

    async def async_run(self, name: str, args: dict) -> str:
        """Dispatch like run_tool(): coroutines awaited, sync fns in executor."""
        t = self.get(name)
        if t is None or t.fn is None:
            return f"error: tool '{name}' is not available (disabled or not loaded)"
        try:
            if inspect.iscoroutinefunction(t.fn):
                return await t.fn(args)
            return await run_in_executor_ctx(t.fn, args)
        except Exception as e:
            return f"error: {type(e).__name__}: {e}"


# ---------------- global registry ----------------
registry = ToolRegistry()


def bootstrap_builtin_tools() -> None:
    """Register the built-in agent tools into the global registry.

    Keeps agent_tools.TOOL_IMPLS as the single source of truth; the registry
    simply mirrors it so other sources register alongside uniformly.
    """
    from .agent_tools import AGENT_TOOLS, TOOL_IMPLS
    for spec in AGENT_TOOLS:
        name = spec["function"]["name"]
        if name in TOOL_IMPLS:
            registry.register(name, TOOL_IMPLS[name], spec, source="builtin",
                               meta={"label": spec["function"].get("description", "")[:80]},
                               replace=True)

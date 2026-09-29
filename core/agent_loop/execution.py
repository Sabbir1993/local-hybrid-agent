import inspect
import sys
from ..agent_tools import TOOL_IMPLS
from ..registry import registry
from ..request_context import run_in_executor_ctx, tool_allowed
from .repair import validate_and_repair_tool_args
from .sandbox import fast_sandbox_check


async def run_tool(name: str, args: dict) -> str:
    # every lane (main loop, router, sub-agents, chat) ends up here, so the write
    # sandbox and the custom agent's tool allowlist are enforced here too
    if not tool_allowed(name):
        return f"error: tool '{name}' is not enabled for the active custom agent"
    approved, note = fast_sandbox_check(name, args or {})
    if not approved:
        return f"error: sandbox violation — {note}"
    # registry first (covers builtin + web + skills + mcp + plugins);
    # fall back to the raw builtin table for the executor lane's core set
    reg = getattr(sys.modules.get("core.registry"), "registry", registry)
    if reg.get(name) is not None:
        return await reg.async_run(name, args)
    tool_impls = getattr(sys.modules.get("core.agent_tools"), "TOOL_IMPLS", TOOL_IMPLS)
    impl = tool_impls.get(name)
    if not impl:
        return f"error: unknown tool {name}"
    try:
        repaired, val_err = validate_and_repair_tool_args(name, args)
        if val_err:
            return val_err
        args = repaired
        if inspect.iscoroutinefunction(impl):
            return await impl(args)
        return await run_in_executor_ctx(impl, args)
    except Exception as e:
        return f"error: {type(e).__name__}: {e}"


def all_tools() -> list:
    """Full tool schema list: builtins + every registered capability."""
    reg = getattr(sys.modules.get("core.registry"), "registry", registry)
    return reg.schemas()

import inspect
import re
import sys
import uuid
from pathlib import PurePosixPath
from ..agent_tools import TOOL_IMPLS
from ..registry import registry
from ..request_context import PERSONAL_BLOCKED_TOOLS, personal_scope, run_in_executor_ctx, tool_allowed
from .repair import validate_and_repair_tool_args
from .sandbox import fast_sandbox_check


async def run_tool(name: str, args: dict, unique_done: bool = False) -> str:
    # every lane (main loop, router, sub-agents, chat) ends up here, so the write
    # sandbox and the custom agent's tool allowlist are enforced here too
    if not tool_allowed(name):
        return f"error: tool '{name}' is not enabled for the active custom agent"
    if personal_scope():
        refusal = _personal_refusal(name, args or {})
        if refusal:
            return refusal
        if name in ("write_file", "doc_create") and not unique_done:   # the agent route renames up front so the UI shows the real name
            args = _unique_create(args or {})
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
        return _unknown_tool(name)
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


def _personal_refusal(name: str, args: dict) -> str:
    """Personal Agent runs never change the user's machine; the reason is written for the model."""
    if name in PERSONAL_BLOCKED_TOOLS:
        return (f"error: '{name}' is not available to a Personal Agent. For system information use run_shell with "
                "a read-only command (systeminfo, tasklist, ipconfig, dir ...); read, search and write files in "
                "your work folder with the file tools. Do not retry '{name}'.".replace("{name}", name))
    if name == "run_shell":
        from ..shell_tools import personal_write_violation
        from ..tool_args import shell_command
        why = personal_write_violation(shell_command(args))
        if why:
            return "error: " + why
    return ""


_ID_TAIL = re.compile(r"(?:[-_][0-9a-f]{8})+$")


def unique_file_name(path_arg: str) -> str:
    """'reports/weekly.md' -> 'reports/weekly_1a2b3c4d.md': the folder is kept, the name gets a fresh id.
    An id an earlier run already added is replaced, never stacked."""
    p = PurePosixPath(str(path_arg).strip().replace("\\", "/"))
    stem = _ID_TAIL.sub("", p.stem) or "file"
    return str(p.with_name(f"{stem}_{uuid.uuid4().hex[:8]}{p.suffix or '.txt'}"))


def unique_create_args(args: dict) -> dict:
    return _unique_create(args)


def _unique_create(args: dict) -> dict:
    """A Personal Agent never overwrites or appends to a file with write_file/doc_create: every file it
    creates is new, so an old file cannot be changed or reused by accident. edit_file is the way to change one."""
    args = dict(args)
    key = next((k for k in ("path", "file", "filename") if args.get(k)), "path")
    args[key] = unique_file_name(args.get(key) or "output.txt")
    args.pop("append", None)
    return args


def _unknown_tool(name: str) -> str:
    """Models often call a skill by its name ("webapp-testing") as if it were a tool. Serve the
    skill instead of failing the step; for anything else, name the closest real tools."""
    try:
        from ..skills import load_skills, tool_read_skill
        if any(str(name).strip().lower() in (k.lower(), sk["name"].lower()) for k, sk in load_skills().items()):
            body = tool_read_skill({"name": name})
            return (f"note: '{name}' is a skill, not a tool - here is its content (use read_skill('{name}') "
                    f"next time). Follow it using the real tools.\n\n{body}")
    except Exception:
        pass
    import difflib
    names = sorted({str((t.get("function") or {}).get("name") or "") for t in all_tools()} - {""})
    close = difflib.get_close_matches(str(name), names, n=3, cutoff=0.5)
    hint = f" Did you mean: {', '.join(close)}?" if close else ""
    return f"error: unknown tool {name}.{hint} Call only tools that are in your tool list."


def all_tools() -> list:
    """Full tool schema list: builtins + every registered capability."""
    reg = getattr(sys.modules.get("core.registry"), "registry", registry)
    return reg.schemas()

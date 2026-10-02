"""
core/agent_loop/schema_check.py - generic JSON-Schema enforcement for tool arguments.

Why this exists: `required` and per-property `type` in a tool schema were documentation for
the model, not enforcement. core/agent_loop/repair.py validates eight tools by hand
(write_file, edit_file, append_file, insert_at_line, read_file, revert, run_shell, run_python),
and everything else - doc_edit, doc_create, create_plan, update_plan_item,
spawn_parallel_agents, all 18 browser tools, all MCP and plugin tools - executed with whatever
the model emitted. A comment in execution.py claimed the validation "covers every tool on every
lane"; it did not, and a comment like that eventually gets someone to delete a check that IS
load-bearing.

This module reads the schema the registry already holds (RegisteredTool.schema, OpenAI
function shape) and enforces the subset that a hand-written validator can do reliably:

  * `required` properties must be present and non-null
  * declared `type` must match (object / array / string / number / integer / boolean)
  * a property typed `object`/`array` must not be handed a bare scalar

Deliberately NOT enforced here:

  * enum / minimum / pattern - left to each tool, because several tools accept a wider set
    than their advertised enum and a false rejection here would block a legitimate call
  * additionalProperties: false - MCP servers routinely declare it loosely

Fail-open by design on anything unparseable: a malformed schema must not disable every tool.
Path-traversal containment is NOT this module's job and does not live here - it is enforced by
core/agent_tools/workspace.py::_ws_resolve, which resolves and then checks containment. This is
an API-contract check, not a safety check.
"""

from typing import Optional

# JSON-Schema type name -> acceptable python types. bool is excluded from number deliberately:
# in Python True is an int, but a boolean is never an acceptable number field.
_TYPES = {
    "object": (dict,),
    "array": (list, tuple),
    "string": (str,),
    "integer": (int,),
    "number": (int, float),
    "boolean": (bool,),
    "null": (type(None),),
}


def _function_schema(tool_schema: dict) -> dict:
    """The function object from either the OpenAI wrapper shape or a bare function shape."""
    if not isinstance(tool_schema, dict):
        return {}
    fn = tool_schema.get("function")
    if isinstance(fn, dict):
        return fn
    if "parameters" in tool_schema:
        return tool_schema
    return {}


def _label(tool_name: str, prop: str) -> str:
    return f"{tool_name}.{prop}"


def check_required(tool_name: str, args: dict, schema: dict) -> Optional[str]:
    """Model-facing error string when `args` violates the schema, else None.

    `schema` is RegisteredTool.schema (or a bare function schema). An empty/absent schema
    means "nothing to enforce" - the eight hand-validated tools in repair.py keep their own
    stricter, path-aware rules, and a tool registered without a parameters block is not
    blocked from running by this.
    """
    if not isinstance(args, dict):
        return f"error: {tool_name} arguments must be an object"
    params = _function_schema(schema).get("parameters")
    if not isinstance(params, dict):
        return None
    props = params.get("properties")
    if not isinstance(props, dict):
        props = {}

    for key in params.get("required") or []:
        if not isinstance(key, str):
            continue
        if key not in args or args[key] is None:
            want = (props.get(key) or {}).get("type")
            hint = f" ({want})" if isinstance(want, str) and want else ""
            return (f"error: {tool_name} is missing required argument '{key}'{hint}. "
                    "Nothing was changed - resend the call with that argument.")

    for key, value in args.items():
        spec = props.get(key)
        if not isinstance(spec, dict):
            continue
        want = spec.get("type")
        if not isinstance(want, str):
            continue
        allowed = _TYPES.get(want)
        if allowed is None:
            continue
        if want in ("integer", "number") and isinstance(value, bool):
            return f"error: {_label(tool_name, key)} must be a {want}, got a boolean"
        if not isinstance(value, allowed):
            got = type(value).__name__
            return f"error: {_label(tool_name, key)} must be a {want}, got {got}"
    return None


def registered_schema(tool_name: str):
    """The registry's schema for `tool_name`, or None if it is not registered.

    Imported lazily and defensively: this runs on every tool call, and a registry that fails
    to import must not turn into "every call is invalid". None means "no schema to check",
    which check_required treats as pass.
    """
    try:
        from ..registry import registry
        t = registry.get(tool_name)
        return t.schema if t is not None else None
    except Exception:
        return None
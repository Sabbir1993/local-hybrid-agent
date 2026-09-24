"""JSON helpers: validate / pretty-print, and dotted-path lookup."""
import json
import re

MAX_OUT = 20000
_PATH_TOKEN = re.compile(r"([^.\[\]]+)|\[(\d+)\]")


def register(api):
    api.add_tool(
        "json_format",
        _format,
        "Validate JSON and pretty-print it (or report the exact parse error position).",
        {"type": "object",
         "properties": {"text": {"type": "string"},
                        "indent": {"type": "integer", "description": "spaces, default 2"}},
         "required": ["text"]},
    )
    api.add_tool(
        "json_get",
        _get,
        "Extract a value from JSON with a dotted path like data.items[0].id",
        {"type": "object",
         "properties": {"text": {"type": "string"}, "path": {"type": "string"}},
         "required": ["text", "path"]},
    )


def _parse(text):
    try:
        return json.loads(text), None
    except json.JSONDecodeError as e:
        return None, f"invalid JSON: {e.msg} at line {e.lineno} column {e.colno}"


def _format(args: dict) -> str:
    obj, err = _parse(str(args.get("text") or ""))
    if err:
        return f"error: {err}"
    indent = max(0, min(8, int(args.get("indent") or 2)))
    return json.dumps(obj, indent=indent, ensure_ascii=False)[:MAX_OUT]


def _get(args: dict) -> str:
    obj, err = _parse(str(args.get("text") or ""))
    if err:
        return f"error: {err}"
    cur = obj
    for key, idx in _PATH_TOKEN.findall(str(args.get("path") or "")):
        try:
            cur = cur[int(idx)] if idx else cur[key]
        except (KeyError, IndexError, TypeError):
            return f"error: path not found at '{key or '[' + idx + ']'}'"
    return json.dumps(cur, indent=2, ensure_ascii=False)[:MAX_OUT]

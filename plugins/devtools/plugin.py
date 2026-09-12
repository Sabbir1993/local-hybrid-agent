"""Example plugin: project stats + dev conveniences.

Demonstrates the full API: tool registration, system prompt fragment,
and event hooks.
"""
import time
from pathlib import Path


def register(api):
    api.add_system_prompt(
        "The devtools plugin is active: prefer report_file_stats for size questions "
        "and use timestamp_now instead of guessing dates."
    )

    api.on_event("after_tool", lambda name, args, result: None)

    api.add_tool(
        "report_file_stats",
        _file_stats,
        "Report line/word/char counts and last-modified time for one workspace file.",
        {"type": "object",
         "properties": {"path": {"type": "string", "description": "workspace-relative path"}},
         "required": ["path"]},
    )

    api.add_tool(
        "timestamp_now",
        _timestamp,
        "Get the current local date/time in ISO format (never guess dates).",
        {"type": "object", "properties": {}},
    )


def _file_stats(args: dict) -> str:
    from core.agent_tools import _ws_resolve
    p = _ws_resolve(args.get("path") or "")
    if not p.is_file():
        return f"error: file not found: {args.get('path')}"
    text = p.read_text(encoding="utf-8", errors="replace")
    return (f"{args['path']}: {len(text.splitlines())} lines, "
            f"{len(text.split())} words, {len(text)} chars, "
            f"modified {time.strftime('%Y-%m-%d %H:%M', time.localtime(p.stat().st_mtime))}")


def _timestamp(args: dict) -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S")

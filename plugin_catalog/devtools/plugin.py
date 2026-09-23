"""Example plugin: project stats + dev conveniences.

Demonstrates the full API: tool registration, system prompt fragment,
and event hooks. File access goes through the companion (the user's own
device) -- plugins must never read the server's disk.
"""
import time


def register(api):
    api.add_system_prompt(
        "The devtools plugin is active: prefer report_file_stats for size questions "
        "and use timestamp_now instead of guessing dates."
    )

    api.on_event("after_tool", lambda name, args, result: None)

    api.add_tool(
        "report_file_stats",
        _file_stats,
        "Report line/word/char counts for one workspace file.",
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


async def _file_stats(args: dict) -> str:
    from core import companion_bridge
    from core.agent_tools import _ws_resolve, require_device_workspace
    uid, _ = require_device_workspace()
    p = _ws_resolve(args.get("path") or "")
    data = await companion_bridge.call(uid, "fs.read", {"path": str(p)})
    text = data.get("content")
    if text is None:
        return f"error: file not found: {args.get('path')}"
    return (f"{args['path']}: {len(text.splitlines())} lines, "
            f"{len(text.split())} words, {len(text)} chars")


def _timestamp(args: dict) -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S")

"""Admin-defined command hooks around agent tool calls (config `hooks` in config/app.json).

    "hooks": {"enabled": true, "timeout_s": 15, "rules": [
        {"id": "fmt", "event": "post_tool", "tool": "edit_file|write_file", "command": "ruff format {path}"},
        {"id": "no-push", "event": "pre_tool", "tool": "run_shell", "command": "python scripts/check_cmd.py"}]}

  pre_tool   runs before the tool; a non-zero exit blocks the call and its output is told to the model.
  post_tool  runs after the tool; a non-zero exit adds the hook's output to the result as feedback.

The command runs on the user's machine through the companion (never on the server), in the project folder. It is
admin configuration, not model output, so it is trusted the way an allow-listed command is. What the model supplied
reaches the command only through two placeholders, `{tool}` and `{path}`, and only when the value is made of safe
characters; an unsafe value fails closed for pre_tool (the call is blocked) and skips a post_tool hook, so a crafted
file name can never become shell syntax. Hooks are off unless `hooks.enabled` is true.
"""

import fnmatch
import re
from typing import Any, Optional

from core.small_model import APP_CONFIG

EVENTS = ("pre_tool", "post_tool")
MAX_HOOKS_PER_CALL = 3
DEFAULT_TIMEOUT_S = 15
MAX_OUTPUT = 400
_SAFE_PATH_RX = re.compile(r"^[A-Za-z0-9_./\\ :+@=-]{1,260}$")
_SAFE_TOOL_RX = re.compile(r"^[A-Za-z0-9_]{1,80}$")
_PLACEHOLDER_RX = re.compile(r"\{(tool|path)\}")
_PATH_KEYS = ("path", "file", "file_path", "filename")


def _cfg() -> dict:
    c = APP_CONFIG.get("hooks")
    return c if isinstance(c, dict) else {}


def enabled() -> bool:
    return bool(_cfg().get("enabled", False))


def matching(event: str, tool: str) -> list:
    """The hooks that apply to this event and tool, in config order (at most MAX_HOOKS_PER_CALL)."""
    if not enabled() or event not in EVENTS:
        return []
    out = []
    for r in _cfg().get("rules") or []:
        if not isinstance(r, dict) or r.get("event") != event or not str(r.get("command") or "").strip():
            continue
        raw = r.get("tool", "*")
        names = raw.split("|") if isinstance(raw, str) else list(raw or ["*"])
        if any(fnmatch.fnmatchcase(tool.lower(), str(n).strip().lower()) for n in names if str(n).strip()):
            out.append(r)
    return out[:MAX_HOOKS_PER_CALL]


def path_arg(args: Any) -> str:
    if isinstance(args, dict):
        for k in _PATH_KEYS:
            v = args.get(k)
            if isinstance(v, str) and v.strip():
                return v.strip()
    return ""


def build_command(rule: dict, tool: str, args: Any) -> Optional[str]:
    """The command line with `{tool}` / `{path}` filled in, or None when a value is not safe to put in a shell."""
    cmd = str(rule.get("command") or "").strip()
    wanted = set(_PLACEHOLDER_RX.findall(cmd))
    values = {"tool": tool, "path": path_arg(args)}
    for key in wanted:
        v = values[key]
        if key == "tool" and not _SAFE_TOOL_RX.match(v):
            return None
        if key == "path" and (not _SAFE_PATH_RX.match(v) or v.startswith("-") or ".." in v.replace("\\", "/").split("/")):
            return None
    return _PLACEHOLDER_RX.sub(lambda m: f'"{values[m.group(1)]}"' if m.group(1) == "path" else values[m.group(1)], cmd)


async def _run(command: str) -> tuple:
    """(exit_code, output) of one hook on the user's machine; never raises."""
    from core import companion_bridge
    from core.agent_tools import require_device_workspace
    try:
        uid, ws = require_device_workspace()
        timeout = int(_cfg().get("timeout_s") or DEFAULT_TIMEOUT_S)
        data = await companion_bridge.call(uid, "shell.run", {"command": command, "cwd": str(ws), "timeout": timeout,
                                                              "approved_in_app": True}, timeout=timeout + 10)
        text = ((data.get("stdout") or "") + (data.get("stderr") or "")).strip()
        return int(data.get("exit_code") if data.get("exit_code") is not None else 1), text[:MAX_OUTPUT]
    except Exception as e:                       # no device, companion gone, timeout: the caller decides what that means
        return 1, f"hook could not run: {type(e).__name__}"


async def run_pre(tool: str, args: Any) -> Optional[str]:
    """None to let the call proceed, or the reason it is blocked. A hook that cannot run blocks the call."""
    for rule in matching("pre_tool", tool):
        cmd = build_command(rule, tool, args)
        rid = str(rule.get("id") or "hook")
        if cmd is None:
            return f"blocked by hook '{rid}': the file name has characters a hook cannot take safely"
        code, out = await _run(cmd)
        if code != 0:
            return f"blocked by hook '{rid}'" + (f": {out}" if out else "")
    return None


async def run_post(tool: str, args: Any) -> str:
    """Feedback lines from failing post_tool hooks (empty when all passed or were skipped)."""
    notes = []
    for rule in matching("post_tool", tool):
        cmd = build_command(rule, tool, args)
        if cmd is None:
            continue
        code, out = await _run(cmd)
        if code != 0:
            notes.append(f"hook '{rule.get('id') or 'hook'}' failed (exit {code})" + (f": {out}" if out else ""))
    return "\n".join(notes)

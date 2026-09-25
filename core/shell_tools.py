"""Shell execution tool with a permission gate.

Policy (config/app.json -> capabilities.shell):
  enabled        — tool registered at all
  ask_first      — pause and ask the user unless the command matches an allow pattern
  allow_patterns — fnmatch wildcard list, e.g. "git *", "npx *", or "*" to allow everything.
                   A pattern only ever auto-approves a *single* command: anything
                   containing shell operators (& | < > ^ ; ` newline, $( ) ...)
                   needs the literal "*" pattern or an explicit per-command OK.
  timeout_s      — hard kill after N seconds

The agent SSE loop installs `permission_callback` before running tasks; when
set and the command is not allow-listed, the callback resolves (allowed, note)
after the user answers the modal. Callbacks are async.
"""

import asyncio
import contextvars
import fnmatch
import os
import re
import subprocess
import sys
import time
from typing import Callable, Optional

from .registry import registry
from .small_model import APP_CONFIG
from . import companion_bridge

# Installed by server_manager at request time: async fn(cmd) -> tuple[bool, str]
permission_callback: Optional[Callable] = None

MAX_OUTPUT_CHARS = 20000
FORBIDDEN_SUBSTR = ("format ", "del /f /s /q c:\\", "rd /s /q c:\\", "remove-item -recurse c:\\")


DEFAULT_EXEC_TIMEOUT_S = 60


def shell_cfg() -> dict:
    caps = APP_CONFIG.get("capabilities", {})
    return caps.get("shell") or {"enabled": False}


# cmd.exe / PowerShell operators that chain, redirect, pipe or substitute:
# "git *" must not auto-approve "git status & del /q ..." or "git log > x.bat".
_SHELL_META_RE = re.compile(r"[&|<>^;`\r\n]|\$\(|%[^%\s]+%")

# The exact command line the agent loop approved (allow-list or permission
# modal) for this request. A contextvar -- not a tool argument -- so a model
# can't mark its own call pre-approved by emitting {"_pre_approved": true}.
_approved_cmd: contextvars.ContextVar = contextvars.ContextVar("shell_approved_cmd", default=None)


def is_compound(cmd: str) -> bool:
    return bool(_SHELL_META_RE.search(cmd or ""))


# Arguments that turn an innocent-looking allow pattern ("git diff*", "type *") into a
# file write, code execution, or a read outside the project. A command carrying one is
# never auto-approved -- it still runs if the user approves it in the modal.
_RISKY_ARG_RE = re.compile(
    r"(?:^|\s)(?:--output\b|-o\b|--ext-diff\b|--upload-pack\b|--receive-pack\b|--exec\b"
    r"|-c\s|--config\b|--git-dir\b|--work-tree\b)"
    r"|![^!\s]+!"                               # cmd.exe delayed expansion
    # absolute / UNC path argument ("/x" alone is a cmd.exe switch, "/etc/x" a path)
    r"|(?:^|\s)[\"']?(?:[a-z]:|\\|/[^\s/]*/)"
    r"|\.\.[\\/]|[\\/]\.\.(?:$|\s)")   # parent-directory traversal


def command_allowed(cmd: str, patterns: list) -> bool:
    """True when `cmd` is auto-approved by one of the allow patterns."""
    c = (cmd or "").strip().lower()
    pats = [str(p).strip().lower() for p in patterns or [] if str(p).strip()]
    if "*" in pats:
        return True             # user explicitly allowed everything
    if not c or is_compound(c):
        return False
    first, _, rest = c.partition(" ")
    if rest and _RISKY_ARG_RE.search(" " + rest):
        return False
    return any(fnmatch.fnmatch(c, p) for p in pats)


_is_allowed = command_allowed   # backwards-compatible name


def mark_approved(cmd: str) -> None:
    """Agent loop: the user/allow-list approved exactly this command line."""
    _approved_cmd.set((cmd or "").strip())


def _sanity(cmd: str) -> Optional[str]:
    """Blatantly destructive commands are always refused, even with '*'."""
    c = cmd.strip().lower()
    if any(s in c for s in FORBIDDEN_SUBSTR):
        return "refused: command matches the always-forbidden list (disk format / recursive OS dir wipe)"
    return None


async def tool_run_shell(args: dict) -> str:
    cmd = (args.get("command") or args.get("cmd") or "").strip()
    if not cmd:
        raise ValueError("command required")
    cfg = shell_cfg()
    if not cfg.get("enabled", False):
        return "error: shell execution is disabled in capabilities (config/app.json -> capabilities.shell.enabled)"

    err = _sanity(cmd)
    if err:
        return f"error: {err}"

    # The agent SSE loop pre-approves via the permission modal (mark_approved);
    # this gate is a fallback for direct/other callers.
    approved = _approved_cmd.get()
    if approved is not None:
        _approved_cmd.set(None)          # single use
    if approved != cmd:
        allowed_by_pattern = command_allowed(cmd, cfg.get("allow_patterns", []) or [])
        if cfg.get("ask_first", True) and not allowed_by_pattern:
            if permission_callback is not None:
                allowed, note = await permission_callback(cmd)
            else:
                allowed, note = False, "no permission channel available"
            if not allowed:
                return f"error: user denied shell command: {cmd}" + (f" ({note})" if note else "")

    raw_t = cfg.get("timeout_s", 0)
    # 0 = "default", not "forever": the server stops waiting after this, so the
    # companion must kill the process then too instead of leaving it running
    timeout = int(raw_t) if raw_t and int(raw_t) > 0 else DEFAULT_EXEC_TIMEOUT_S
    from .agent_tools import require_device_workspace
    # Agent shell commands run ONLY on the user's machine via the companion.
    # (Skill installs used to run server-side in BASE_DIR -- that was agent
    # code execution on the server, so they now run in the user's project too.)
    uid, ws = require_device_workspace()
    target_cwd = str(ws)

    # Automatically add -y / --yes for npx / npm commands if not present so skills installation doesn't hang
    exec_cmd = cmd
    if exec_cmd.strip().startswith("npx ") and " -y" not in exec_cmd and " --yes" not in exec_cmd:
        exec_cmd = re.sub(r"^npx\s+", "npx -y ", exec_cmd.strip())

    try:
        data = await companion_bridge.call(
            uid, "shell.run", {"command": exec_cmd, "cwd": target_cwd, "timeout": timeout},
            timeout=(timeout or 60) + 10)
    except TimeoutError:
        return f"error: command timed out after {timeout}s"
    except Exception as e:
        return f"error: companion shell exec failed: {e}"
    out = (data.get("stdout") or "")[-MAX_OUTPUT_CHARS:]
    err_out = (data.get("stderr") or "")[-4000:]
    result = f"exit code {data.get('exit_code')}"
    if out:
        result += f"\n--- stdout ---\n{out}"
    if err_out:
        result += f"\n--- stderr ---\n{err_out}"
    return result


def add_allow_pattern(pattern: str) -> None:
    """Persist a new allow pattern (from 'Always allow') into config/app.json."""
    import json
    from .config import CONFIG_FILE, write_app_config
    pattern = pattern.strip()
    if not pattern:
        return
    cfg_path = CONFIG_FILE
    try:
        cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
        shell = cfg.setdefault("capabilities", {}).setdefault("shell", {})
        pats = shell.setdefault("allow_patterns", [])
        if pattern not in pats:
            pats.append(pattern)
        write_app_config(cfg, CONFIG_FILE)
        # live-update the in-memory config too
        caps = APP_CONFIG.setdefault("capabilities", {})
        sc = caps.setdefault("shell", {})
        lst = sc.setdefault("allow_patterns", [])
        if pattern not in lst:
            lst.append(pattern)
    except Exception as e:
        print(f"[shell] failed persisting allow pattern: {e}", file=sys.stderr)


def register_shell_tools() -> None:
    if not shell_cfg().get("enabled", False):
        return
    registry.register(
        "run_shell", tool_run_shell,
        {"type": "function", "function": {
            "name": "run_shell",
            "description": ("Run a shell command in the workspace directory (Windows). "
                            "Returns stdout/stderr + exit code. Use for git, npm/npx, pip, "
                            "build tools, directory listings. Avoid for anything destructive."),
            "parameters": {"type": "object",
                           "properties": {"command": {"type": "string", "description": "the shell command line to run"}},
                           "required": ["command"]},
        }},
        source="shell", meta={"label": "Shell execution"}, replace=True)

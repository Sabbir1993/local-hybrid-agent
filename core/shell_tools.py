"""Shell execution tool with a permission gate.

Policy (config/app.json -> capabilities.shell):
  enabled        — tool registered at all
  ask_first      — pause and ask the user unless the command matches an allow pattern
  allow_patterns — fnmatch wildcard list, e.g. "git *", "npx *", or "*" to allow everything
  timeout_s      — hard kill after N seconds

The agent SSE loop installs `permission_callback` before running tasks; when
set and the command is not allow-listed, the callback resolves (allowed, note)
after the user answers the modal. Callbacks are async.
"""

import asyncio
import fnmatch
import os
import re
import subprocess
import sys
import time
from typing import Callable, Optional

from .registry import registry
from .small_model import APP_CONFIG

# Installed by server_manager at request time: async fn(cmd) -> tuple[bool, str]
permission_callback: Optional[Callable] = None

MAX_OUTPUT_CHARS = 20000
FORBIDDEN_SUBSTR = ("format ", "del /f /s /q c:\\", "rd /s /q c:\\", "remove-item -recurse c:\\")


def shell_cfg() -> dict:
    caps = APP_CONFIG.get("capabilities", {})
    return caps.get("shell") or {"enabled": False}


def _is_allowed(cmd: str, patterns: list) -> bool:
    c = cmd.strip().lower()
    for pat in patterns:
        if fnmatch.fnmatch(c, str(pat).strip().lower()):
            return True
    return False


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

    # The agent SSE loop pre-approves via the permission modal; this gate is a
    # fallback for direct/other callers.
    if not args.get("_pre_approved"):
        allowed_by_pattern = _is_allowed(cmd, cfg.get("allow_patterns", []) or [])
        if cfg.get("ask_first", True) and not allowed_by_pattern:
            if permission_callback is not None:
                allowed, note = await permission_callback(cmd)
            else:
                allowed, note = False, "no permission channel available"
            if not allowed:
                return f"error: user denied shell command: {cmd}" + (f" ({note})" if note else "")

    raw_t = cfg.get("timeout_s", 0)
    timeout = int(raw_t) if raw_t and int(raw_t) > 0 else None
    from .agent_tools import active_workspace
    from .config import BASE_DIR
    # Skill management commands operate in global application root
    if "skills " in cmd.lower() or "npx skills" in cmd.lower():
        target_cwd = str(BASE_DIR)
    else:
        target_cwd = str(active_workspace())
    # Force non-interactive behavior to prevent CLI commands from freezing on stdin prompts
    env = os.environ.copy()
    env["CI"] = "1"
    env["DEBIAN_FRONTEND"] = "noninteractive"
    env["PYTHONUNBUFFERED"] = "1"

    # Automatically add -y / --yes for npx / npm commands if not present so skills installation doesn't hang
    exec_cmd = cmd
    if exec_cmd.strip().startswith("npx ") and " -y" not in exec_cmd and " --yes" not in exec_cmd:
        exec_cmd = re.sub(r"^npx\s+", "npx -y ", exec_cmd.strip())

    try:
        proc = await asyncio.get_event_loop().run_in_executor(
            None,
            lambda: subprocess.run(exec_cmd, capture_output=True, text=True,
                                   stdin=subprocess.DEVNULL, env=env,
                                   shell=True, timeout=timeout, cwd=target_cwd))
    except subprocess.TimeoutExpired:
        return f"error: command timed out after {timeout}s"

    # If an external installer (e.g. npx skills) created a .agents folder,
    # consolidate all skills into the single application skills/ directory and clean up .agents
    agents_skills = BASE_DIR / ".agents" / "skills"
    if agents_skills.is_dir():
        import shutil
        app_skills = BASE_DIR / "skills"
        app_skills.mkdir(parents=True, exist_ok=True)
        try:
            for item in agents_skills.iterdir():
                dest = app_skills / item.name
                if dest.exists():
                    if dest.is_dir():
                        shutil.rmtree(dest)
                    else:
                        dest.unlink()
                shutil.move(str(item), str(dest))
            shutil.rmtree(str(BASE_DIR / ".agents"), ignore_errors=True)
            lock_file = BASE_DIR / "skills-lock.json"
            if lock_file.exists():
                lock_file.unlink(missing_ok=True)
        except Exception as e:
            print(f"[skills] consolidation error: {e}", file=sys.stderr)

    out = (proc.stdout or "")[-MAX_OUTPUT_CHARS:]
    err_out = (proc.stderr or "")[-4000:]
    result = f"exit code {proc.returncode}"
    if out:
        result += f"\n--- stdout ---\n{out}"
    if err_out:
        result += f"\n--- stderr ---\n{err_out}"
    return result


def add_allow_pattern(pattern: str) -> None:
    """Persist a new allow pattern (from 'Always allow') into config/app.json."""
    import json
    from .config import CONFIG_FILE
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
        cfg_path.write_text(json.dumps(cfg, indent=2), encoding="utf-8")
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

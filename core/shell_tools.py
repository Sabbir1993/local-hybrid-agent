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
from .tool_args import shell_command
from .text_clip import clip_head_tail
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


SHELL_INTERPRETERS = frozenset({"powershell", "pwsh", "cmd", "bash", "sh", "wsl", "wmic"})


def command_allowed(cmd: str, patterns: list) -> bool:
    """True when `cmd` is auto-approved by one of the allow patterns."""
    c = (cmd or "").strip().lower()
    pats = [str(p).strip().lower() for p in patterns or [] if str(p).strip()]
    if "*" in pats:
        return True             # user explicitly allowed everything
    # A pattern for a shell that runs an arbitrary command string ("powershell -NoProfile *") would approve ANY
    # code. It never counts here; a Personal Agent matches it through personal_command_allowed, which checks
    # that the command is read-only first.
    pats = [p for p in pats if p.split(None, 1)[0] not in SHELL_INTERPRETERS]
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


_approved_code: contextvars.ContextVar = contextvars.ContextVar("python_approved_code", default=None)

# Execution timeout (seconds) for ONE call, set only by trusted server code (run_tests: test suites outlast the
# default). A contextvar, not a tool argument, so the model cannot raise the admin-configured limit itself.
_exec_timeout: contextvars.ContextVar = contextvars.ContextVar("shell_exec_timeout", default=None)


def mark_code_approved(code: str) -> None:
    """Agent loop: the user approved exactly this run_python code in the web app."""
    _approved_code.set(code or "")


def take_code_approval(code: str) -> bool:
    """Single use: was exactly `code` approved in the web app (or ask_first is off)?"""
    approved = _approved_code.get()
    _approved_code.set(None)
    return approved == (code or "") or not shell_cfg().get("ask_first", True)


# Personal Agent shell commands: anything that redirects output, changes files, installs software or starts a
# shell/script host is refused before it is even offered for approval. python / py / node are NOT refused:
# they run only after the user approves that exact command (the card shows it), because a one-liner is how an
# agent checks a runtime or parses a log. Such code can reach beyond the work folder, so approval is the gate.
_PERSONAL_WRITE_RE = re.compile(
    r"(?<![<>=-])>|\|\s*tee\b"
    r"|(?:^|[\s&|;(])(?:del|erase|rd|rmdir|rm|mv|move|ren|rename|copy|cp|xcopy|robocopy|mkdir|md|touch|ln|"
    r"chmod|chown|attrib|icacls|takeown|format|diskpart|shutdown|taskkill|kill|reg|sc|net|netsh|schtasks|"
    r"new-item|set-content|add-content|out-file|clear-content|remove-item|move-item|copy-item|rename-item|"
    r"set-itemproperty|new-itemproperty|remove-itemproperty|invoke-expression|iex|invoke-webrequest|iwr|"
    r"invoke-restmethod|irm|start-process|stop-process|stop-service|start-service|set-service|"
    r"curl|wget|pip|pip3|npm|npx|yarn|pnpm|winget|choco|scoop|"
    r"cmd|bash|sh|wsl|start|powershell|pwsh)(?![\w-])"      # not the front of Format-Table, Sort-Object ...
    r"|\bsed\s+-i\b|\bgit\s+(?:commit|checkout|switch|reset|clean|add|apply|am|merge|pull|push|rebase|stash|"
    r"init|clone|rm|mv|restore|cherry-pick|revert|tag|branch\s+-[dD])\b", re.I)


# `wmic <alias> where ProcessId > 0 get ...`: the > there is a comparison, not a redirect. Only the text between
# where and get is ignored, so a real redirect after `get ...` is still caught.
# `2>nul`, `>$null`, `2>&1` only discard or merge output; they write no file
_HARMLESS_REDIRECT_RE = re.compile(r"\d?>>?\s*(?:nul|\$null|/dev/null)(?![\w.])|\d?>&\d", re.I)
_WMIC_WHERE_RE =re.compile(r"^\s*wmic\b.*?\bwhere\b(.*?)\bget\b", re.I)


# PowerShell as a read-only inspection tool: `powershell -NoProfile -Command "Get-Process | Sort-Object CPU"`.
# The wrapper is allowed only when everything after it passes a default-deny check: every Verb-Noun cmdlet must
# have a read-only verb, and the routes to writing without a cmdlet (.NET statics, methods, aliases, scripts,
# call operators, encoded commands) are refused. The exact command is still shown on the approval card.
_PS_WRAPPER_RE = re.compile(r"^\s*(?:&\s*)?(?:powershell|pwsh)(?:\.exe)?(?=\s|$)(.*)$", re.I | re.S)
_PS_SAFE_FLAGS = frozenset({"noprofile", "nop", "noninteractive", "noni", "nologo", "nol"})
_PS_READ_VERBS = frozenset({"get", "select", "where", "sort", "measure", "group", "format", "foreach", "compare",
                            "test", "resolve", "split", "join", "convertto", "convertfrom"})
_PS_READ_CMDLETS = frozenset({"out-string", "out-host", "write-output", "write-host"})
_PS_CMDLET_RE = re.compile(r"(?<![\w$.-])([A-Za-z]+)-([A-Za-z]\w*)")
_PS_RISKY_RE = re.compile(
    r"::|`|&|\$\(|[\\/]\.\.[\\/]"
    r"|\.(?:delete|kill|start|write\w*|create\w*|move\w*|copy\w*|remove\w*|set\w*|invoke\w*|save|dispose|close)\s*\("
    r"|(?:^|[\s|;({])\.\s+\S"                                         # dot-sourcing
    r"|\.(?:ps1|bat|cmd|exe|vbs|js|msi|dll)(?![\w])"                  # running a script / program
    r"|(?:^|[\s|;({])(?:ri|rni|sc|ac|si|ni|cpi|mi|del|erase|rd|rm|mv|cp|kill|spps|sasv|spsv|clc|iex|icm|iwr|irm|"
    r"saps|sal|sv|rmdir|move|copy|ren|md|mkdir)(?![\w-])"             # write aliases
    r"|format-volume", re.I)


# pure-computation .NET statics ([math]::Round, [datetime]::Now): no file, process or network access
_PS_SAFE_STATIC_RE = re.compile(
    r"\[(?:math|datetime|timespan|string|int|long|double|decimal)\]::\w+"
    # read-only host facts: OS, machine, CPU count, uptime (not Environment.Exit / SetEnvironmentVariable)
    r"|\[(?:system\.)?environment\]::(?:osversion|machinename|username|is64bitoperatingsystem|is64bitprocess"
    r"|processorcount|tickcount64?|version|newline|systemdirectory)\b"
    r"|\[(?:system\.)?runtime\.interopservices\.runtimeinformation\]::(?:osdescription|osarchitecture"
    r"|processarchitecture|frameworkdescription)\b", re.I)


def _write_refusal(found: str) -> str:
    return (f"a Personal Agent cannot run this command: `{found}` is not allowed (it may not redirect output, "
            "change files, install software or start a shell; any > or >> counts as a redirect, so filter "
            "the output instead of comparing with >). PowerShell is allowed for read-only inspection only: "
            "powershell -NoProfile -Command \"Get-... | Select-... | Format-...\". To save results use "
            "write_file (it saves into your work folder); to change a file use edit_file. "
            "Do not retry this command or run the same thing through run_python.")


def _plain_violation(cmd: str) -> Optional[str]:
    m = _WMIC_WHERE_RE.match(cmd)
    if m:
        cmd = cmd[:m.start(1)] + " " + cmd[m.end(1):]
    cmd = _HARMLESS_REDIRECT_RE.sub(" ", cmd)
    hit = _PERSONAL_WRITE_RE.search(cmd)
    return _write_refusal(hit.group(0).strip()) if hit else None


def _powershell_violation(rest: str) -> Optional[str]:
    """`rest` is everything after the powershell/pwsh word."""
    body = rest
    while True:
        m = re.match(r"\s*-(\w+)\s*", body)
        if not m:
            break
        flag = m.group(1).lower()
        if flag in _PS_SAFE_FLAGS:
            body = body[m.end():]
            continue
        if flag in ("command", "c"):
            body = body[m.end():]
            break
        return _write_refusal(f"powershell -{m.group(1)}")
    body = body.strip()
    if not body:
        return _write_refusal("powershell without a -Command")
    body = _HARMLESS_REDIRECT_RE.sub(" ", body)
    hit = _PS_RISKY_RE.search(_PS_SAFE_STATIC_RE.sub(" ", body))
    if hit:
        return _write_refusal(hit.group(0).strip())
    for verb, noun in _PS_CMDLET_RE.findall(body):
        name = f"{verb}-{noun}".lower()
        if verb.lower() not in _PS_READ_VERBS and name not in _PS_READ_CMDLETS:
            return _write_refusal(f"{verb}-{noun}")
    # quotes off so a program name right behind one ("powershell ...", 'cmd /c') still starts a word
    return _plain_violation(body.replace('"', " ").replace("'", " "))


def personal_command_allowed(cmd: str, patterns: list) -> bool:
    """A Personal Agent asks for every command, except read-only PowerShell the user saved an allow pattern for
    (e.g. `powershell -NoProfile *`). Only patterns that start with powershell/pwsh count, and the command must
    pass the read-only check; that check, not is_compound, is what makes the pipes and `;` in it safe."""
    c = (cmd or "").strip().lower()
    if not _PS_WRAPPER_RE.match(c) or personal_write_violation(cmd):
        return False
    pats = [str(p).strip().lower() for p in patterns or []]
    return any(fnmatch.fnmatch(c, p) for p in pats if p.startswith(("powershell", "pwsh")))


def personal_write_violation(cmd: str) -> Optional[str]:
    """Why a Personal Agent may not run `cmd`, or None. Read-only inspection is what remains."""
    cmd = cmd or ""
    ps = _PS_WRAPPER_RE.match(cmd)
    if ps:
        return _powershell_violation(ps.group(1))
    return _plain_violation(cmd)


def _sanity(cmd: str) -> Optional[str]:
    """Blatantly destructive commands are always refused, even with '*'."""
    c = cmd.strip().lower()
    if any(s in c for s in FORBIDDEN_SUBSTR):
        return "refused: command matches the always-forbidden list (disk format / recursive OS dir wipe)"
    return None


def _background_args(args: dict) -> dict:
    """{"background": True, "wait_for_port": N, "wait": secs} for a dev-server style command, else {}."""
    truthy = lambda v: v is True or str(v).strip().lower() in ("1", "true", "yes")
    if not isinstance(args, dict) or not truthy(args.get("background")):
        return {}
    out = {"background": True}
    try:
        port = int(args.get("wait_for_port") or 0)
    except (TypeError, ValueError):
        port = 0
    if 0 < port < 65536:
        out["wait_for_port"] = port
    try:
        out["wait"] = min(max(int(args.get("wait") or 30), 1), 120)
    except (TypeError, ValueError):
        out["wait"] = 30
    return out


async def tool_run_shell(args: dict) -> str:
    cmd = shell_command(args)
    if not cmd:
        raise ValueError('command required - pass the shell command line as "command", e.g. {"command": "dir"}')
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
    timeout = _exec_timeout.get() or timeout
    bg = _background_args(args)
    from .agent_tools import require_device_workspace
    # Agent shell commands run ONLY on the user's machine via the companion.
    # (Skill installs used to run server-side in BASE_DIR -- that was agent
    # code execution on the server, so they now run in the user's project too.)
    try:
        uid, ws = require_device_workspace()
    except PermissionError as e:       # WorkspaceAccessDenied: no device / project - a result the model can act on
        return f"error: {e}"
    target_cwd = str(ws)

    # Automatically add -y / --yes for npx / npm commands if not present so skills installation doesn't hang
    exec_cmd = cmd
    if exec_cmd.strip().startswith("npx ") and " -y" not in exec_cmd and " --yes" not in exec_cmd:
        exec_cmd = re.sub(r"^npx\s+", "npx -y ", exec_cmd.strip())

    try:
        data = await companion_bridge.call(
            # the gate above passed (web-app card, allow rule or ask_first off): the
            # companion skips its own dialog unless the user turned local confirmation on
            uid, "shell.run", {"command": exec_cmd, "cwd": target_cwd, "timeout": timeout,
                               "approved_in_app": True, **bg},
            timeout=(timeout or 60) + (bg.get("wait", 0) if bg else 0) + 10)
    except TimeoutError:
        return f"error: command timed out after {timeout}s"
    except Exception as e:
        return f"error: companion shell exec failed: {e}"
    # command output keeps its start and (mostly) its end, with a note: the result is usually the last lines
    out = clip_head_tail(data.get("stdout") or "", MAX_OUTPUT_CHARS, head_frac=0.2)
    err_out = clip_head_tail(data.get("stderr") or "", 4000, head_frac=0.2)
    # A command broken by the filtered environment reports it in stderr only
    # (Windows: "'x' is not recognized as an internal or external command", exit
    # code 1). Spell that case out: the companion now hands agent commands a
    # filtered env (companion/shellops.js: buildChildEnv), and a silently
    # narrowed one is the kind of breakage a model retries forever.
    _err_low = (data.get("stderr") or "").lower()
    if ("is not recognized as an internal" in _err_low
            or "is not defined" in _err_low or "command not found" in _err_low):
        err_out += ("\n[env] the companion runs agent commands with a filtered environment "
                    "(secrets removed); if this command needs one, re-run it with the value "
                    "passed explicitly, or set COMPANION_ENV_ALLOW for that variable.")
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
                            "build tools, directory listings. Avoid for anything destructive. "
                            "A command that never exits (a dev server) must be started with "
                            "background=true, otherwise it is killed after the timeout."),
            "parameters": {"type": "object",
                           "properties": {"command": {"type": "string", "description": "the shell command line to run"},
                                          "background": {"type": "boolean", "description": "start detached and return once it is listening (dev servers); result gives the pid - stop it with taskkill /PID <pid> /T /F"},
                                          "wait_for_port": {"type": "integer", "description": "with background: return when this local port accepts connections"}},
                           "required": ["command"]},
        }},
        source="shell", meta={"label": "Shell execution"}, replace=True)
    from .agent_tools.test_runner import RUN_TESTS_SCHEMA, tool_run_tests
    registry.register("run_tests", tool_run_tests, RUN_TESTS_SCHEMA, source="shell",
                      meta={"label": "Run tests"}, replace=True)

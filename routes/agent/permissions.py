import asyncio
from typing import Optional
from fastapi import Depends
from fastapi.responses import JSONResponse
from core.auth import Principal, user_has_permission
from core.deps import get_current_user
from core.audit import audit_log
from core.db import db_add_project_allow_pattern, db_owned_project_id
from core import auth_db
from core.agent_tools import get_active_project
from core.config import TOOL_CANCEL_GRACE_S, TOOL_MAX_S
from core.shell_tools import add_allow_pattern, command_allowed

from .base import router
from .models import PermissionAnswerReq


# ---------------- shell permission flow ----------------
# pending shell permission requests: req_id -> {cmd, event, result}
_perm_pending: dict[str, dict] = {}


@router.post("/agent/permission")
async def agent_permission_answer(req: PermissionAnswerReq, user: Principal = Depends(get_current_user)):
    """UI answers a permission_request emitted on the agent SSE stream."""
    rec = _perm_pending.get(req.req_id)
    # only the user whose agent run raised the request may answer it
    if rec is None or rec.get("user_id") != user.id:
        return JSONResponse({"error": "unknown or expired permission request"}, status_code=404)
    if req.decision in ("always", "project", "user") and req.pattern:
        err = _pattern_error(req.pattern, rec)
        if err:
            return JSONResponse({"error": err}, status_code=400)
    if req.decision == "always" and req.pattern:
        # "always" edits the global allowlist for every user -- same permission
        # as the Settings shell card (routes/capabilities.py)
        if not user_has_permission(user, "settings.shell.configure"):
            audit_log(user, action="settings.shell.configure", permission_key="settings.shell.configure",
                      result="deny", detail={"pattern": req.pattern, "via": "agent/permission"})
            return JSONResponse({"error": "missing permission: settings.shell.configure "
                                          "(choose 'allow for me' instead)"}, status_code=403)
        add_allow_pattern(req.pattern)
        audit_log(user, action="shell.allow_pattern.add", resource=req.pattern,
                  permission_key="settings.shell.configure", detail={"scope": "global"})
    elif req.decision == "project" and req.pattern:
        # Save to project-specific allowed patterns -- caller's own project only
        target_proj = req.project_id or get_active_project()
        if target_proj:
            pid = db_owned_project_id(target_proj, user.id)
            if pid is None:
                return JSONResponse({"error": "project not found"}, status_code=404)
            db_add_project_allow_pattern(pid, req.pattern)
            audit_log(user, action="shell.allow_pattern.add", resource=req.pattern,
                      detail={"scope": "project", "project_id": pid})
    elif req.decision == "user" and req.pattern:
        # Save to this user's own allow list -- never affects other users
        auth_db.add_user_allow_pattern(user.id, req.pattern)
        audit_log(user, action="shell.allow_pattern.add", resource=req.pattern, detail={"scope": "user"})
    rec["result"] = {"allow": req.decision != "deny", "decision": req.decision,
                     "note": f"pattern allowed for {req.decision}" if req.decision in ("always", "project", "user") else ""}
    rec["event"].set()
    saved = req.decision in ("always", "project", "user") and bool(req.pattern)
    return {"ok": True, "decision": req.decision, "saved": saved}


def can_save_pattern(cmd: str) -> bool:
    """A saved '<first word> *' only auto-approves a *single, plain* command (core/shell_tools.py
    command_allowed): a command with & | > ; or risky options is asked about every time, so
    offering to remember it would be a promise the app cannot keep."""
    parts = (cmd or "").strip().split()
    return bool(parts) and command_allowed(cmd, [f"{parts[0]} *"])


@router.get("/agent/allowed-commands")
async def my_allowed_commands(user: Principal = Depends(get_current_user)):
    """The commands this user chose to always allow ('Always allow for me')."""
    return {"patterns": auth_db.get_user_allow_patterns(user.id)}


@router.delete("/agent/allowed-commands")
async def forget_allowed_command(pattern: str, user: Principal = Depends(get_current_user)):
    auth_db.remove_user_allow_pattern(user.id, pattern)
    audit_log(user, action="shell.allow_pattern.remove", resource=pattern, detail={"scope": "user"})
    return {"ok": True}


def _pattern_error(pattern: str, rec: dict) -> Optional[str]:
    """A saved allow pattern must come from the command that was actually shown:
    the exact command line or '<first word> *'. Never a bare '*', never for code."""
    if rec.get("kind") == "python":
        return "run_python code can only be allowed once"
    if rec.get("kind") == "media":
        return "a cloud image/video can only be allowed once"
    pat = pattern.strip().lower()
    cmd = str(rec.get("cmd") or "").strip().lower()
    first = cmd.split()[0] if cmd.split() else ""
    if not first or pat not in (cmd, f"{first} *"):
        return "pattern must be the approved command or '<command> *'"
    return None


async def _await_permission(req_id: str, ev: asyncio.Event):
    """Wait for the UI's answer to a shell permission request (180s cap)."""
    try:
        await asyncio.wait_for(ev.wait(), timeout=180)
    except asyncio.TimeoutError:
        _perm_pending.pop(req_id, None)
        return False, "permission request timed out (180s)"
    rec = _perm_pending.pop(req_id, None) or {}
    res = rec.get("result") or {"allow": False, "note": "no answer"}
    return res.get("allow", False), res.get("note", "")


PERMISSION_WAIT_S = 180
PING = ": ping\n\n"          # SSE comment: ignored by the client, keeps proxies and idle timers from cutting the stream


async def _permission_stream(req_id: str, ev: asyncio.Event, ping_s: float = 15, wait_s: float = PERMISSION_WAIT_S):
    """Like _await_permission, but an async generator so the agent stream stays alive while the user decides.
    Yields ("ping", PING) every `ping_s`, then one ("done", (allowed, note, decision))."""
    waited = 0.0
    try:
        while waited < wait_s:
            step = min(ping_s, wait_s - waited)
            try:
                await asyncio.wait_for(ev.wait(), timeout=step)
                break
            except asyncio.TimeoutError:
                waited += step
                yield "ping", PING
        else:
            yield "done", (False, f"permission request timed out ({int(wait_s)}s)", "timeout")
            return
        rec = _perm_pending.get(req_id) or {}
        res = rec.get("result") or {"allow": False, "note": "no answer"}
        yield "done", (res.get("allow", False), res.get("note", ""), res.get("decision", ""))
    finally:
        # Every pop used to sit inline, and both were skipped when the client disconnected and
        # GeneratorExit was thrown at the ping yield. Each abandoned run then left its entry
        # -- holding the full command or script text -- in a module global for the life of the
        # process, with no other code path to reclaim it.
        _perm_pending.pop(req_id, None)


async def keepalive(coro, interval: float = 15, max_s: float = TOOL_MAX_S):
    """Run `coro` as a task and yield ("ping", PING) while it is still running, then ("done", result).
    An exception from `coro` is raised here; if this generator is closed first, the task is cancelled and
    then awaited, so a disconnected client does not leave a tool running unattended.

    `max_s` bounds one tool call. It was unbounded: a tool that never returned held its slot for
    the rest of the run, and since the agent's wall-clock budget is only checked between steps
    nothing else could end it. On expiry the task is cancelled and the timeout is reported to the
    caller like any other result, so the model gets told what happened instead of the stream dying."""
    task = asyncio.ensure_future(coro)
    waited = 0.0
    try:
        while True:
            if max_s and waited >= max_s:
                task.cancel()
                await _drain(task)
                yield "done", ("error: the tool was cancelled: it ran for longer than "
                               f"{int(max_s)}s without finishing ({_task_desc(task)})")
                return
            done, _ = await asyncio.wait({task}, timeout=interval)
            if done:
                break
            waited += interval
            yield "ping", PING
        yield "done", task.result()
    finally:
        if not task.done():
            task.cancel()
            # cancelling is a request, not a fact. Without awaiting it, a tool that ignores
            # cancellation keeps running on the user's device after the client is gone.
            await _drain(task)


async def _drain(task) -> None:
    """Best-effort: wait for a cancelled task to actually finish, bounded by a grace period."""
    try:
        await asyncio.wait({task}, timeout=TOOL_CANCEL_GRACE_S)
    except Exception:
        pass


def _task_desc(task) -> str:
    """A tool name for the cancellation message. Never the args: they can hold user code."""
    fn = getattr(task, "get_coro", lambda: None)()
    return getattr(fn, "__qualname__", None) or getattr(fn, "__name__", None) or "the tool"

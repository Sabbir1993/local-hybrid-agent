"""Working memory: a per-session, structured record of the task in progress.

When the agent's context is compacted, the older turns become a digest. The digest alone is a
transcript; this adds a structured summary on top (goal, files touched, open problems, plan,
next step). It is built from the messages and the plan table - no model call - and is stored in
the session row so it survives a restart and is loaded when the task is resumed. Each save
replaces the previous one (rewritten, never appended).

The summary can also be mirrored to `.agent/working_memory.md` and `PLAN.md` on the user's
device (agent.mirror_to_workspace), so the user can read it in their project.
"""
import re
import time
from typing import Optional

from .agent_loop.compaction import _call_index
from .agent_loop.executor_view import current_request_index, _text
from .agent_tools.file_ops import FILE_WRITE_TOOLS
from .db.common import _get_projects_db
from .sqlite_util import transaction

MAX_SUMMARY_CHARS = 2600
_PLAN_MARKS = {"pending": "[ ]", "in_progress": "[~]", "done": "[x]", "failed": "[!]"}


def _clip(s: str, n: int) -> str:
    s = " ".join(str(s).split())
    return s if len(s) <= n else s[:n - 1] + "…"


def _is_error(text: str) -> bool:
    t = str(text or "")
    return t.startswith("error:") or "verify: FAILED" in t[:4000]


def build_summary(msgs: list, plan_items: Optional[list] = None, previous: str = "") -> str:
    """The structured summary of a run so far. Deterministic and bounded."""
    calls = _call_index(msgs)
    results = {m.get("tool_call_id"): str(m.get("content") or "") for m in msgs if m.get("role") == "tool"}

    idx = current_request_index(msgs)
    goal = _clip(_text(msgs[idx].get("content")), 400) if idx is not None else ""

    touched: dict = {}          # path -> [created?, edits]
    reads: list = []
    cmds: list = []
    failures: list = []         # (key, text) newest last
    for m in msgs:
        for tc in (m.get("tool_calls") or []):
            cid = tc.get("id")
            name, args = calls.get(cid, ("?", {}))
            res = results.get(cid, "")
            path = str(args.get("path") or args.get("file") or "")
            ok = bool(res) and not res.startswith("error:")
            if name in FILE_WRITE_TOOLS and path:
                if ok:
                    rec = touched.setdefault(path, {"created": False, "edits": 0})
                    if name == "write_file" and "(created)" in res:
                        rec["created"] = True
                    else:
                        rec["edits"] += 1
                    failures = [f for f in failures if f[0] != path]
                    if "verify: FAILED" in res:
                        failures.append((path, f"{path}: {_clip(res.split('verify: FAILED', 1)[1], 140)}"))
                elif res:
                    failures.append((path, f"{name} {path}: {_clip(res, 140)}"))
            elif name == "read_file" and path and path not in reads:
                reads.append(path)
            elif name in ("run_shell", "run_python"):
                cmd = str(args.get("command") or args.get("code") or "")
                cmds.append((_clip(cmd, 70), _clip(res.splitlines()[0] if res else "", 60)))
                if _is_error(res) or re.search(r"exit code [1-9]", res):
                    failures.append((f"cmd:{cmd[:40]}", f"{name} `{_clip(cmd, 50)}`: {_clip(res, 120)}"))

    lines = [f"Goal: {goal}" if goal else "Goal: (not recorded)"]
    if touched:
        lines.append("Files touched:")
        for p, r in list(touched.items())[-12:]:
            what = ("created" if r["created"] else "") + (", " if r["created"] and r["edits"] else "")
            what += f"edited {r['edits']}x" if r["edits"] else ""
            lines.append(f"- {p} ({what})")
    if reads:
        lines.append("Read: " + ", ".join(reads[-8:]))
    if cmds:
        lines.append("Last commands: " + "; ".join(f"`{c}`" + (f" -> {o}" if o else "") for c, o in cmds[-3:]))
    if failures:
        lines.append("Open problems:")
        lines += [f"- {t}" for _, t in failures[-4:]]
    plan_items = plan_items or []
    if plan_items:
        lines.append("Plan:")
        for it in plan_items[:25]:
            lines.append(f"{_PLAN_MARKS.get(it.get('status'), '[ ]')} {it['ord']}. {_clip(it['text'], 120)}"
                         + (f" ({_clip(it['note'], 60)})" if it.get("note") else ""))
        nxt = next((it for it in plan_items if it.get("status") in ("in_progress", "pending", "failed")), None)
        lines.append(f"Next step: {nxt['ord']}. {_clip(nxt['text'], 160)}" if nxt else
                     "Next step: all plan steps are done - verify and give the final answer.")
    text = "\n".join(lines)
    return text if len(text) <= MAX_SUMMARY_CHARS else text[:MAX_SUMMARY_CHARS - 1] + "…"


def save(session_id: int, user_id: int, text: str) -> None:
    if not session_id or not text:
        return
    from .db.sessions import _at_rest
    now = time.time()
    with transaction(_get_projects_db()) as c:
        c.execute(
            "INSERT INTO session_working_memory (session_id, user_id, content, updated_at) VALUES (?, ?, ?, ?) "
            "ON CONFLICT(session_id) DO UPDATE SET content = excluded.content, updated_at = excluded.updated_at, "
            "user_id = excluded.user_id", (int(session_id), user_id, _at_rest(text), now))


def load(session_id: int, user_id: int) -> str:
    if not session_id:
        return ""
    row = _get_projects_db().execute(
        "SELECT content FROM session_working_memory WHERE session_id = ? AND user_id = ?",
        (int(session_id), user_id)).fetchone()
    return row["content"] if row else ""


def prompt_block(text: str) -> str:
    if not text.strip():
        return ""
    return ("--- WORKING MEMORY (your notes on this task from before; may be out of date - the files and the plan "
            "are the truth) ---\n" + text.strip() + "\n--- END WORKING MEMORY ---")


def plan_markdown(plan_items: list) -> str:
    """PLAN.md content: a checkbox list of the tracked plan."""
    if not plan_items:
        return ""
    box = {"pending": "[ ]", "in_progress": "[~]", "done": "[x]", "failed": "[!]"}
    return "# Plan\n\n" + "\n".join(
        f"- {box.get(it.get('status'), '[ ]')} {it['text']}" + (f" - {it['note']}" if it.get("note") else "")
        for it in plan_items) + "\n"


async def mirror_to_device(uid: int, workspace, summary: str, plan_items: list) -> None:
    """Best effort: write .agent/working_memory.md and PLAN.md into the user's project folder."""
    from pathlib import Path
    from . import companion_bridge
    try:
        base = Path(workspace)
        if summary:
            await companion_bridge.call(uid, "fs.write", {
                "path": str(base / ".agent" / "working_memory.md"),
                "content": f"# Working memory\n\n{summary}\n", "append": False})
        plan = plan_markdown(plan_items)
        if plan:
            await companion_bridge.call(uid, "fs.write", {"path": str(base / "PLAN.md"), "content": plan,
                                                          "append": False})
    except Exception:
        pass

"""Plan discipline for agent (Build) mode: plan first, one step at a time, finish only when done.

Pure functions over the plan rows (db_get_plan_items): routes/agent/run.py calls them, and they are unit-tested
on their own because the loop itself cannot be driven without servers.
"""
import re
from typing import Optional

OPEN = ("pending", "in_progress")

# Verbs that make a request a multi-step job. Two or more of them (or a long request) need a plan.
_ACTION_RX = re.compile(
    r"\b(create|build|make|write|implement|add|generate|develop|design|set ?up|install|configure|refactor|"
    r"fix|debug|update|migrate|convert|deploy|test|integrate|run|connect|scaffold|port)\b", re.I)
LONG_REQUEST_CHARS = 200


def plan_mode_setting(cfg: dict) -> str:
    v = str((cfg or {}).get("plan_required", "auto")).strip().lower()
    return v if v in ("auto", "always", "off") else "auto"


# /init and "analyze / explore / summarize this project" are read-then-report jobs: a forced todo list only adds
# create_plan / update_plan_item steps (each one a full model round trip) to something that has no steps to track.
INIT_PROMPT_PREFIX = "Initialize this project for future agent sessions"
_ANALYSIS_RX = re.compile(
    r"^\s*(please\s+)?(analy[sz]e|explore|summari[sz]e|review|understand|scan|explain|walk me through|give me an overview of)\b"
    r".{0,80}\b(project|codebase|code base|repo|repository|app|application|source)\b", re.I)


def is_analysis_request(query: str) -> bool:
    q = (query or "").strip()
    return q.startswith(INIT_PROMPT_PREFIX) or bool(_ANALYSIS_RX.match(q))


_FILE_RX = re.compile(r"\b[\w./-]*\w\.[A-Za-z][A-Za-z0-9]{0,5}\b")
_PROJECT_WORD_RX = re.compile(r"\b(app|application|game|site|website|project|api|service|system|bot|tool|"
                              r"dashboard|library|package|module|script)s?\b", re.I)
SINGLE_FILE_MAX_CHARS = LONG_REQUEST_CHARS // 2


def is_single_file_task(query: str) -> bool:
    """Short request that names exactly one file and asks for little else."""
    q = (query or "").strip()
    if not q or len(q) > SINGLE_FILE_MAX_CHARS or _PROJECT_WORD_RX.search(q):
        return False
    if len({m.lower() for m in _FILE_RX.findall(q)}) != 1:
        return False
    return len({m.lower() for m in _ACTION_RX.findall(q)}) <= 2


def needs_plan(category: str, query: str, agent_cfg: dict = None, personal: bool = False) -> bool:
    """Should this request start with a todo list? auto: creation requests, several action verbs, or a long request.
    A Personal Agent (small model, read-only inspection and reports) needs a higher bar: three verbs or a request
    twice as long, so "check my disk and tell me what is big" is not turned into a planning exercise."""
    mode = plan_mode_setting(agent_cfg)
    if mode == "off" or category == "greeting":
        return False
    if mode == "always":
        return True
    if is_analysis_request(query):
        return False
    q = (query or "").strip()
    if category == "creation":
        # one named file, a short request and at most two action verbs ("write notes.txt, then read it
        # back") has no steps to track: a plan only adds model round trips before the first write
        return not is_single_file_task(q)
    long_chars = LONG_REQUEST_CHARS * (2 if personal else 1)
    if category == "question" and len(q) < long_chars:
        return False
    return len(q) >= long_chars or len({m.lower() for m in _ACTION_RX.findall(q)}) >= (3 if personal else 2)


def open_items(items: list) -> list:
    return [i for i in items if i.get("status") in OPEN]


def current_item(items: list):
    """The step being worked on: the in_progress one, else the first pending one."""
    for i in items:
        if i.get("status") == "in_progress":
            return i
    for i in items:
        if i.get("status") == "pending":
            return i
    return None


def focus_message(items: list, chunk_tokens: int = 3000) -> str:
    """One control message naming the current step. Starts with a prefix the executor view treats as loop-injected."""
    cur = current_item(items)
    if cur is None:
        return ""
    done = sum(1 for i in items if i.get("status") == "done")
    failed = sum(1 for i in items if i.get("status") == "failed")
    nxt = next((i for i in items if i["ord"] > cur["ord"] and i.get("status") in OPEN), None)
    return (f"[plan reminder] {done}/{len(items)} steps done"
            + (f", {failed} failed" if failed else "")
            + f". Current step #{cur['ord']}: \"{cur['text']}\". Work ONLY on this step now - "
            f"one file or one section per call, each write under about {chunk_tokens} tokens. "
            f"When it is finished call update_plan_item(item={cur['ord']}, status='done'); if it cannot be done "
            "use status='failed' with a note."
            + (f" Do not start step #{nxt['ord']} first." if nxt else ""))


# Tools that only manage the plan: they are never evidence that work was done.
PLAN_TOOLS = frozenset({"create_plan", "update_plan_item", "get_plan", "finish"})
# What counts as having changed something / run something, for the steps that say they did.
WORK_TOOLS = frozenset({"write_file", "write_file_common", "append_file", "edit_file", "insert_at_line", "revert",
                        "run_shell", "run_python", "run_tests", "doc_edit", "doc_create"})
RUN_TOOLS = frozenset({"run_shell", "run_python", "run_tests"})
_NEEDS_WORK_RX = re.compile(r"\b(create|write|build|implement|fix|edit|add|refactor|update|rename|delete|install|"
                            r"generate|scaffold|port|migrate|convert|change|remove|replace)\b", re.I)
_NEEDS_RUN_RX = re.compile(r"\b(test|tests|verify|run|check|lint|validate|execute|launch)\b", re.I)


def work_since_last_plan_change(actions: list) -> list:
    """Successful non-plan tool calls made after the plan was last created or a step last changed status.

    The window matters: `create_plan` is itself a tool call, so "any action at all" was always true and the
    old receipt check could never fire. Evidence for step N is what happened since step N-1 was closed.
    """
    acts = actions or []
    start = 0
    for i, a in enumerate(acts):
        if a.get("name") in ("create_plan", "update_plan_item") and a.get("ok", True):
            start = i + 1
    return [a for a in acts[start:] if a.get("ok") and a.get("name") not in PLAN_TOOLS]


def _is_run_tool(name: str) -> bool:
    return name in RUN_TOOLS or str(name).startswith(("browser_", "mobile_"))


def check_execution_receipt(items: list, item: int, actions: list, warned: Optional[set] = None) -> Optional[str]:
    """Refuse 'done' for a step with no evidence of work since the previous step closed.

    What a step claims decides what counts: "write/fix/add ..." needs a file change or command, "test/run/
    verify ..." needs a command or test run, anything else needs some successful tool call. The first refusal
    per step is advisory: work done for several steps at once (one big write covering steps 2 and 3) is
    legitimate, so when the same step is marked done again the model has confirmed it and it goes through.
    """
    target = next((i for i in items if i["ord"] == item), None)
    text = target.get("text", "") if target else f"step #{item}"
    evidence = {a.get("name") for a in work_since_last_plan_change(actions)}
    needs_work, needs_run = bool(_NEEDS_WORK_RX.search(text)), bool(_NEEDS_RUN_RX.search(text))
    if needs_work or needs_run:
        ok = (needs_work and bool(evidence & WORK_TOOLS)) or (needs_run and any(_is_run_tool(n) for n in evidence))
        want = ("a file change or a command" if needs_work and not needs_run else
                "a command or test run" if needs_run and not needs_work else "a file change, command or test run")
    else:
        ok, want = bool(evidence), "at least one tool call"
    if ok:
        return None
    if warned is not None:
        if ("receipt", item) in warned:
            return None
        warned.add(("receipt", item))
    return (f"step #{item} ('{text[:60]}') has no evidence of work: nothing succeeded since the previous step was "
            f"closed, and this step needs {want}. Do the work now. If an earlier step already did all of it, "
            "call update_plan_item for this step again to confirm.")


def check_files_before_done(item: int, broken: list, warned: Optional[set] = None) -> Optional[str]:
    """Refuse 'done' while a file written this session still fails its syntax check (the verify loop's last
    word on it). Advisory once per step, like the receipt: a restored or replaced file can leave the failure
    count stale, and a step must not be trapped by bookkeeping."""
    if not broken:
        return None
    if warned is not None:
        if ("files", item) in warned:
            return None
        warned.add(("files", item))
    shown = ", ".join(str(b) for b in broken[:3])
    return (f"step #{item} cannot be done yet: {shown} still fails its syntax check (see the last 'verify: FAILED'). "
            "Fix it, or mark the step failed with a note. If it was already fixed or restored, call update_plan_item "
            "again to confirm.")


def check_update(items: list, item: int, status: str):
    """Error text when the status change breaks the one-step-at-a-time rule, else None."""
    if status == "in_progress":
        other = next((i for i in items if i.get("status") == "in_progress" and i["ord"] != item), None)
        if other:
            return (f"step #{other['ord']} is already in progress. Finish it (done or failed) before "
                    f"starting step #{item}.")
    if status == "done":
        earlier = next((i for i in items if i["ord"] < item and i.get("status") in OPEN), None)
        if earlier:
            return (f"step #{earlier['ord']} ({earlier['text'][:80]}) is not finished. Do the steps in order: "
                    f"finish #{earlier['ord']} first, or mark it failed with a note.")
    return None


def run_summary(items: list) -> Optional[str]:
    """A bounded code for how a run ended against its plan, or None if there was no plan.

    Nothing measured this before: route_events.outcome says what a turn did,
    never whether the plan finished. Without it, "should a plan be a DAG?" has no
    number on either side of the question.

      plan:done            every step reached a terminal success
      plan:open:N/M        N steps still pending/in_progress out of M
      plan:failed:N        N steps failed
      plan:failed:N/open:K/M   both, which is the case that actually matters

    Codes only - counts, never step text (a step can quote a file path).
    An unrecognised status counts as open, so a new status can never read as done.
    """
    if not items:
        return None
    total = len(items)
    done = failed = 0
    for it in items:
        status = str(it.get("status") or "pending")
        if status == "done":
            done += 1
        elif status == "failed":
            failed += 1
    open_n = total - done - failed
    if failed and open_n:
        return f"plan:failed:{failed}/open:{open_n}/{total}"
    if failed:
        return f"plan:failed:{failed}"
    if open_n:
        return f"plan:open:{open_n}/{total}"
    return "plan:done"


def stuck_action(steps_on_item: int, limit: int) -> str:
    """ok | warn, by steps spent on the current step with no progress (no file written, no status change).
    It never fails a step by itself: a step that keeps writing can take as long as it needs, and a run that
    really goes nowhere is stopped by the loop guard."""
    if limit <= 0:
        return "ok"
    if steps_on_item >= limit:
        return "warn"
    return "ok"


def stuck_message(item: dict, steps: int) -> str:
    return (f"[plan reminder] Step #{item['ord']} (\"{item['text'][:120]}\") has used {steps} steps without "
            "progress. Split the work into smaller pieces (write a skeleton, then append section by section), "
            "or mark it failed with a note and move on.")

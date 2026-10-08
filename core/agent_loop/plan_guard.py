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


def needs_plan(category: str, query: str, agent_cfg: dict = None, personal: bool = False) -> bool:
    """Should this request start with a todo list? auto: creation requests, several action verbs, or a long request.
    A Personal Agent (small model, read-only inspection and reports) needs a higher bar: three verbs or a request
    twice as long, so "check my disk and tell me what is big" is not turned into a planning exercise."""
    mode = plan_mode_setting(agent_cfg)
    if mode == "off" or category == "greeting":
        return False
    if mode == "always":
        return True
    q = (query or "").strip()
    if category == "creation":
        return True
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


def check_execution_receipt(items: list, item: int, actions: list) -> Optional[str]:
    """Verify that a step marked 'done' has an execution receipt or successful tool evidence.
    Prevents empty/hallucinated instant completions without executing any actions.
    """
    if not actions:
        target = next((i for i in items if i["ord"] == item), None)
        step_text = target.get("text", "") if target else f"step #{item}"
        if any(w in step_text.lower() for w in ("create", "write", "build", "implement", "test", "verify", "run", "fix", "edit", "add")):
            return (f"execution receipt missing for step #{item} ('{step_text[:60]}'): no tool actions "
                    "have been performed yet. Execute the required file writes or tests before marking done.")
    return None


def check_syntax_before_done(actions: list) -> Optional[str]:
    """Return an error string if any file written in `actions` has a syntax error.

    Called by check_update() before allowing a step to be marked 'done'.
    Delegates to patch_verifier.syntax_check_actions so that write_file calls
    producing a SyntaxError or IndentationError are caught before the plan moves on.

    Returns None when all written files parse cleanly (or no Python/JS files were written).
    """
    try:
        from .patch_verifier import syntax_check_actions
        failures = syntax_check_actions(actions)
        if failures:
            first = failures[0]
            return first.as_receipt_error()
    except Exception:
        pass   # never block the plan on an unexpected verifier failure
    return None


def check_update(items: list, item: int, status: str, actions: Optional[list] = None):
    """Error text when the status change breaks the one-step-at-a-time rule or lacks verification receipt, else None."""
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
        if actions is not None:
            err = check_execution_receipt(items, item, actions)
            if err:
                return err
            err = check_syntax_before_done(actions)
            if err:
                return err
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

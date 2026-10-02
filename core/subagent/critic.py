import difflib
import re
from pathlib import Path
from typing import Optional

from ..pan import mask_pans
from .runner import run_subagent


def parse_reviewer_verdict(reviewer_text: str) -> tuple[bool, str]:
    """Parse a reviewer's response into (approved: bool, feedback: str).

    Looks for an explicit 'VERDICT: APPROVED' or 'VERDICT: REJECTED - <reason>' line.
    Falls back to semantic keyword detection if no explicit VERDICT prefix exists.

    FAILS CLOSED when nothing is parseable. This used to end with
    `return True, "No critical defects flagged by reviewer"`, which meant any reviewer output
    that matched none of the four defect phrases and none of the approval keywords was
    APPROVED - a 500-word critique that never typed "VERDICT:" and never said the word
    "regression" sailed through the gate. For a verification gate, "could not tell" is not
    "fine": the correct outcome is a rejection carrying the reason, so the loop retries with a
    stricter reviewer prompt instead of accepting code nobody checked.
    """
    text = (reviewer_text or "").strip()
    if not text:
        return False, "Reviewer returned an empty response"

    # Match lines like 'VERDICT: APPROVED' or 'VERDICT: REJECTED - reason'
    match = re.search(
        r"(?im)^\s*VERDICT\s*:\s*(APPROVED|PASS|REJECTED|FAIL)\b(?:\s*[-–—:]\s*(.*))?$",
        text,
    )
    if match:
        v_type = match.group(1).upper()
        reason = (match.group(2) or "").strip()
        if v_type in ("APPROVED", "PASS"):
            return True, reason or ("Approved" if v_type == "APPROVED" else "Pass")
        return False, reason or ("Rejected" if v_type == "REJECTED" else "Fail")

    # Fallback: search anywhere in text for VERDICT: <type>
    match_any = re.search(
        r"\bVERDICT\s*:\s*(APPROVED|PASS|REJECTED|FAIL)\b(?:\s*[-–—:]\s*([^\n\r]+))?",
        text,
        re.IGNORECASE,
    )
    if match_any:
        v_type = match_any.group(1).upper()
        reason = (match_any.group(2) or "").strip()
        if v_type in ("APPROVED", "PASS"):
            return True, reason or ("Approved" if v_type == "APPROVED" else "Pass")
        return False, reason or ("Rejected" if v_type == "REJECTED" else "Fail")

    upper = text.upper()
    # Check for approval keywords without rejection keywords. The "VERDICT: X" members are
    # redundant - they are substrings of "APPROVED"/"REJECTED" - but they are kept because
    # they document the intended vocabulary and cost nothing.
    has_approval = any(w in upper for w in ("APPROVED", "VERDICT: APPROVED", "LOOKS GOOD TO ME", "LGTM"))
    has_rejection = any(w in upper for w in ("REJECTED", "VERDICT: REJECTED", "CHANGES REQUIRED", "DISAPPROVED", "FAIL"))

    # Precedence is rejection > explicit defect term > approval.
    #
    # Rejection first: a reviewer that says both has raised a concern, and resolving that
    # ambiguity towards "ship it" is the wrong direction for a gate.
    #
    # Defect terms are checked BEFORE approval because they are the stronger signal. This
    # ordering was previously approval-first, which made the defect heuristic unreachable
    # whenever the review also contained the word "APPROVED" - so "Mostly APPROVED, but this
    # is a regression" passed the gate.
    if has_rejection:
        return False, "Reviewer requested changes or rejected the diff"

    lower = text.lower()
    defects = ("syntax error", "vulnerability", "regression", "broken test", "fatal defect")
    named = [d for d in defects if d in lower]
    if named:
        return False, f"Reviewer identified critical defects in code ({', '.join(named)})"

    if has_approval:
        return True, "Reviewer approved changes"

    # Unparseable and silent: fail closed.
    return False, ("Reviewer gave no APPROVED/REJECTED verdict and no recognised defect term, "
                   "so the review cannot be trusted; rejected for retry")


def get_workspace_changes_diff(baseline_changes: Optional[dict] = None) -> tuple[list[str], str]:
    """Inspect workspace changes recorded in session state and produce a unified diff.

    Returns:
        (modified_files_list, unified_diff_text)
    """
    try:
        from ..agent_tools.workspace import _ws_changes
        from ..request_context import get_current_user_id

        uid = get_current_user_id()
        current = _ws_changes.get(uid) or {}
    except Exception:
        current = {}

    baseline = baseline_changes or {}
    touched_paths = []
    diff_hunks = []

    for path, rec in current.items():
        before_text = baseline.get(path, rec.get("before"))
        after_text = rec.get("after")

        # If file was modified or created in this window
        if path not in baseline or after_text != baseline.get(path):
            touched_paths.append(path)
            p_name = Path(path).name
            b_lines = (before_text or "").splitlines()
            a_lines = (after_text or "").splitlines()
            hunk = list(
                difflib.unified_diff(
                    b_lines,
                    a_lines,
                    fromfile=f"a/{p_name}",
                    tofile=f"b/{p_name}",
                    lineterm="",
                )
            )
            if hunk:
                diff_hunks.append("\n".join(hunk[:150]))

    if not touched_paths:
        return [], "(no workspace files modified)"

    diff_text = "\n\n".join(diff_hunks)
    if len(diff_text) > 8000:
        diff_text = diff_text[:8000] + "\n... [diff truncated for length]"

    return touched_paths, diff_text or "(no text diff generated)"


def _critic_envelope_header(iterations: int, max_iterations: int, verdict: str, status: str) -> str:
    return f"[critic-actor · iterations={iterations}/{max_iterations} · verdict={verdict} · status={status}]"


async def run_critic_actor_cycle(
    task: str,
    max_iterations: int = 2,
    coder_lane: Optional[str] = None,
    reviewer_lane: Optional[str] = None,
    coder_tools: Optional[list] = None,
    reviewer_tools: Optional[list] = None,
) -> dict:
    """Orchestrate an automated Coder-Reviewer critique loop.

    1. Dispatches Coder sub-agent to implement task.
    2. Collects git/workspace diff.
    3. Dispatches Reviewer sub-agent to critique changes.
    4. If reviewer approves (VERDICT: APPROVED), returns success.
    5. If reviewer rejects (VERDICT: REJECTED), re-dispatches Coder with feedback
       up to `max_iterations` times.
    """
    task = (task or "").strip()
    if not task:
        err = "error: task is required"
        return {
            "status": "error",
            "iterations": 0,
            "verdict": "ERROR",
            "summary": err,
            "reviewer_feedback": "",
            "modified_files": [],
            "diff_summary": "",
            "history": [],
            "output": err,
        }

    max_iter = max(1, min(int(max_iterations or 2), 3))
    history = []
    last_coder_res = ""
    last_reviewer_res = ""
    last_feedback = ""
    approved = False
    all_modified = set()
    cycle_diff = ""

    for iter_idx in range(1, max_iter + 1):
        # Snapshot current workspace changes before coder edits
        try:
            from ..agent_tools.workspace import _ws_changes
            from ..request_context import get_current_user_id

            uid = get_current_user_id()
            baseline_changes = {k: rec.get("after") for k, rec in (_ws_changes.get(uid) or {}).items()}
        except Exception:
            baseline_changes = {}

        # 1. Dispatch Coder
        if iter_idx == 1:
            coder_prompt = task
        else:
            coder_prompt = (
                f"Task:\n{task}\n\n"
                f"[CRITIC REVIEW FEEDBACK - Iteration {iter_idx - 1}]\n"
                f"The reviewer inspected your previous implementation and rejected it with the following defects:\n"
                f"{last_feedback}\n\n"
                "Please fix these specific issues in the code and verify the implementation."
            )

        coder_res = await run_subagent(
            task=coder_prompt,
            role="coder",
            lane_override=coder_lane,
            tool_allowlist=coder_tools,
        )
        last_coder_res = coder_res

        # Check for catastrophic early failures
        if coder_res.startswith("error:") or "(sub-agent got no response" in coder_res:
            fail_status = "no_response" if "got no response" in coder_res else "error"
            header = _critic_envelope_header(iter_idx, max_iter, "ERROR", fail_status)
            out = f"{header}\n{coder_res}"
            return {
                "status": fail_status,
                "iterations": iter_idx,
                "verdict": "ERROR",
                "summary": coder_res,
                "reviewer_feedback": "",
                "modified_files": sorted(list(all_modified)),
                "diff_summary": "",
                "history": history,
                "output": out,
            }

        # 2. Extract diff & touched files
        touched, cycle_diff = get_workspace_changes_diff(baseline_changes)
        all_modified.update(touched)

        # 3. Dispatch Reviewer
        mod_names = ", ".join(Path(p).name for p in touched) if touched else "(no files modified)"
        reviewer_prompt = (
            f"You are the Critic Reviewer inspecting code changes for the following task:\n"
            f"Task:\n{task}\n\n"
            f"Coder Summary:\n{coder_res}\n\n"
            f"Modified Files: {mod_names}\n\n"
            f"Diff of Changes:\n{cycle_diff}\n\n"
            "Review these changes for correctness, edge cases, code quality, and security.\n"
            "End your review with a clear verdict line:\n"
            "VERDICT: APPROVED\n"
            "or\n"
            "VERDICT: REJECTED - <specific defects to fix>"
        )

        reviewer_res = await run_subagent(
            task=reviewer_prompt,
            role="reviewer",
            lane_override=reviewer_lane,
            tool_allowlist=reviewer_tools,
        )
        last_reviewer_res = reviewer_res

        approved, feedback = parse_reviewer_verdict(reviewer_res)
        last_feedback = feedback

        history.append({
            "iteration": iter_idx,
            "coder_result": coder_res,
            "reviewer_result": reviewer_res,
            "approved": approved,
            "feedback": feedback,
            "modified_files": list(touched),
        })

        if approved:
            break

    status = "success" if approved else "critic_rejected"
    verdict = "APPROVED" if approved else "REJECTED"
    header = _critic_envelope_header(len(history), max_iter, verdict, status)

    files_list_str = ", ".join(Path(p).name for p in all_modified) if all_modified else "none"

    body = [
        f"=== Coder Outcome (Iteration {len(history)}/{max_iter}) ===",
        last_coder_res.strip(),
        f"\n=== Reviewer Critique ({verdict}) ===",
        last_reviewer_res.strip(),
        f"\n=== Summary of Changes ===",
        f"Files modified: {files_list_str}",
    ]
    raw_out = f"{header}\n" + "\n".join(body)
    safe_out, _ = mask_pans(raw_out)

    return {
        "status": status,
        "iterations": len(history),
        "verdict": verdict,
        "summary": last_coder_res,
        "reviewer_feedback": last_feedback,
        "modified_files": sorted(list(all_modified)),
        "diff_summary": cycle_diff,
        "history": history,
        "output": safe_out,
    }


async def tool_spawn_reviewed_coder(args: dict) -> str:
    """Agent tool implementation for spawn_reviewed_coder."""
    task = str(args.get("task") or "").strip()
    if not task:
        return "error: task is required"

    max_iter = args.get("max_iterations") or 2
    try:
        max_iter = int(max_iter)
    except Exception:
        max_iter = 2

    res = await run_critic_actor_cycle(
        task=task,
        max_iterations=max_iter,
        coder_lane=args.get("coder_lane"),
        reviewer_lane=args.get("reviewer_lane"),
        coder_tools=args.get("coder_tools"),
        reviewer_tools=args.get("reviewer_tools"),
    )
    return res["output"]

import asyncio
import json
import sys
import time
from pathlib import Path
from fastapi import Depends, Request
from fastapi.responses import StreamingResponse, JSONResponse
from core.auth import Principal
from core.deps import get_current_user
from core.audit import audit_log
from core import input_guard
from core import context_budget
from core import lane_health
from core.small_model import classifier
from core import step_outcome
from core.subagent.runner import subagent_result_verdict as _subagent_verdict
from core import tool_surface
from core import prompt_scope
from core.project_context import load_project_instructions, prompt_block as project_prompt_block
from core.knowledge_access import allowed_source_ids_for, kb_local_only, set_kb_cloud_blocked
from core.db import (
    db_record_request,
    db_get_project_allow_patterns,
    db_get_plan_items,
    db_owned_project_id,
    db_session_owner,
)
from core import auth_db
from core.small_model import (
    APP_CONFIG,
    small_models,
    router_route,
    router_available,
    router_engine_name,
)
from core.state import state
from core import cloud
from core import output_guard
from core.sse import sse
from core.agent_tools.limits import agent_limit, effective_max_tokens, executor_effort_ceiling
from core.agent_tools.memory_tools import MEMORY_TOOL_NAMES
from core import agent_memory, working_memory
from core.agent_tools import (
    FILE_WRITE_TOOLS,
    active_workspace,
    get_active_project,
    require_device_workspace,
    WorkspaceAccessDenied,
    set_plan_context,
    tool_update_plan_item,
)
from core import companion_bridge
from core.registry import registry
from core.grammar import envelope_examples
from core.skills import skills_prompt_fragment
from core.agent_library import agent_library_prompt_fragment
from core import router_policy, route_log
from core.plugins import plugins_prompt_fragment, fire_hook
from core.shell_tools import command_allowed, mark_approved, mark_code_approved, shell_cfg
from core import device_approval
from core.request_context import set_device_approved
from core.agent_loop.narration import _is_narration
from core.agent_loop.executor_view import build_executor_view
from core.agent_loop.finish import apply_finish, without_finish
from core.agent_loop import plan_guard, script_nudge
from core.agent_loop import loop_guard as _lg
from core.tool_args import shell_command
from core.agent_loop.tool_output import cap_tool_result, cap_from_config
from core.agent_loop.clearing import clear_old_results
from core.agent_loop import (
    run_tool,
    AGENT_SYSTEM_PROMPT,
    all_tools,
    is_degeneration_or_loop,
    sanitize_user_facing_content,
    validate_and_repair_tool_args,
    fast_sandbox_check,
    validate_and_finalize_response,
    is_passive_refusal,
    PASSIVE_REFUSAL_NOTE,
    safe_parse_and_repair_args,
    MAX_CUTOFF_RETRIES,
    CUT_CALL_NOTICE,
    CUT_TEXT_NOTICE,
    was_cut_off,
    _extract_text_tool_calls,
    compact_messages,
)
from core.agent_loop.policy import (
    LaneInputs,
    StopContext,
    decide_lane,
    describe_stop as _describe_stop,
    parallel_spawn_eligible,
    stop_headline,
    stop_note,
)
from core.agent_loop.stream import (
    DrainResult,
    StreamSpec,
    account_llm_result,
    drain_llm_stream,
)
from core.monitor import (
    _monitor_state,
    monitor_begin,
    monitor_end,
    parse_cache_tokens,
)
from .. import common
from core import reasoning as reasoning_mod
from ..common import _llm_chat_stream
from ..common.sampling_extra import sampler_extra
from core.verifier import sse_events as answer_check_events

from .base import _executor_device_label, _main_device_label, router
from .stream import run_agent_stream
from .constants import (
    AGENT_MAX_STEPS,
    ESC_STREAK_LIMIT,
    EXECUTOR_TEST_TOOLS,
    LOOP_STOP_STREAK,
    MAX_PLAN_NUDGES,
    MAX_PASSIVITY_NUDGES,
    NEAR_REPEAT_LIMIT,
    AGENT_RESUME_PREFIX,
    NEAR_REPEAT_WINDOW,
    near_repeat_limit,
    PLAN_MODE_PROMPT,
    PLAN_MODE_TOOLS,
    _run_timeout_s,
)
from .setup import SetupError, setup_run
from .guards import (
    _executor_grammar,
    _guard_flush_events,
    _strip_download_markers,
    _with_diff,
)
from .models import AgentRequest
from .permissions import _await_permission, _perm_pending, _permission_stream, can_save_pattern, keepalive

# appended to a declined command: the model otherwise retries the same thing through run_python
_NO_WORKAROUND = (" The user said no to this command. Do not run the same thing another way (run_python, another "
                  "shell, a different spelling). Use a simpler read-only command, or report what you could not collect.")


# _describe_stop lives in core.agent_loop.policy (imported above) so the stop-summary
# text is unit-tested without importing this route module.


def _classify_step_error(e: Exception) -> tuple[str, str]:
    """(message for the user, outcome kind) for a failure inside a step.

    The client must never see the raw exception: httpx transport errors carry local file
    paths and driver messages, and this stream goes to a browser. Keep the wording to what
    the user can act on and let the operator find the detail in the server log."""
    import httpx
    if isinstance(e, httpx.TimeoutException):
        return ("⚠️ The model stopped responding and the request timed out. "
                "The run has ended — try again, or switch to a smaller model in "
                "Settings → Models & Jobs if this happens often."), "timeout"
    if isinstance(e, (httpx.ConnectError, httpx.ConnectTimeout, httpx.RemoteProtocolError)):
        return ("⚠️ Lost the connection to the local model server. The run has ended — "
                "check that it is still loaded in Settings → Models & Jobs, then try again."), "error"
    if isinstance(e, OSError):
        return ("⚠️ A local operation failed and the run has ended. "
                "See the server log for the detail."), "error"
    return (f"⚠️ Agent loop error: {type(e).__name__}. See the server log for the detail."), "error"


def _error_class(e: BaseException | None) -> str | None:
    """route_runs.detail code for a crashed run: the exception TYPE, never the message.

    outcome='error' is 15% of recorded runs (median step 1) and used to carry no
    cause at all, which made the largest early-exit population unmeasurable. Type
    names are code-like; messages carry local paths, so the message never goes in.
    Bounded length: custom exception names can be arbitrarily long.
    """
    if e is None:
        return None
    name = type(e).__name__[:40] or "Exception"
    return f"err:{name}"


_SYNTH_NOTE_CODES = {
    "synthesized response from successful tool actions": "synth:tool_actions",
    "synthesized web search results": "synth:web_results",
    "synthesized execution output": "synth:execution",
    "extracted answer from model reasoning": "synth:reasoning_answer",
    "extracted full model reasoning": "synth:reasoning_full",
    "fallback confirmation": "synth:fallback_confirmation",
    PASSIVE_REFUSAL_NOTE: "stall:passive_refusal",
}


def _synth_detail(note: str | None) -> str | None:
    """route_runs.detail code for why a run ended synthesized (or stalled).

    validate_and_finalize_response already names the cause (core/agent_loop/
    sandbox.py) and run.py streams it to the client, but it never reached the
    database - so all 16 recorded synthesized runs were one indistinguishable
    bucket. Fixed vocabulary, looked up by value: the note itself is never
    copied into the code, so a future note carrying user text cannot leak into
    retained storage. "validated" (a real answer) records nothing, so the
    interesting rows stay visible.
    """
    if not note or note == "validated":
        return None
    return _SYNTH_NOTE_CODES.get(note, "synth:other")


async def _get_workspace_tree_summary() -> str:
    """Compact directory overview to ground the model during directive nudging."""
    try:
        from core.agent_tools.file_ops import tool_list_files
        res = await tool_list_files({"pattern": "*"})
        if res and not str(res).startswith("error:"):
            lines = [l.strip() for l in str(res).split("\n") if l.strip()][:30]
            if lines and lines != ["(no files matched)"]:
                return "\n".join(lines)
    except Exception:
        pass
    return "(workspace directory is ready for new files)"


def _tool_failure_code(name: str, result) -> str | None:
    """The route_events.tool_err code for a failed tool call.

    A sub-agent reports failure in its envelope, not with an "error:" prefix, so
    the text classifier cannot see it: without this, every exhausted child would
    be recorded as one anonymous failure - the same blind spot the plan's
    spawn_agent diagnosis ran into.
    """
    import re as _re
    if isinstance(result, str):
        statuses = set(_re.findall(r"status=([a-z_]+)", result)) if _subagent_verdict(name, result) is False else set()
        if statuses:
            return "subagent:" + ",".join(sorted(statuses))
    return route_log.classify_tool_result(result)


@router.post("/agent/run")
async def agent_run(req: AgentRequest, request: Request, user: Principal = Depends(get_current_user)):
    if req.permission_mode == "plan":
        req.plan = True      # the Plan mode of the dropdown is the same read-only run as the /plan switch
    try:
        ctx = await setup_run(req, request, user)
    except SetupError as e:
        return JSONResponse(e.payload, status_code=e.status_code)
    # The loop itself (lane stepping, tool dispatch, escalation, termination,
    # SSE events) lives in routes/agent/stream.py as run_agent_stream() - extracted
    # verbatim from this function on 2026-10-06 so the 1,500-line body stops
    # strangling the route module and can be covered independently.
    return await run_agent_stream(req, user, ctx)

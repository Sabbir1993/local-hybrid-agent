"""Pure orchestration policy: which lane runs this step, and what each stop means.

No I/O, no imports from routes/ or core modules with side effects (only loop_guard, which
is pure). The route computes the inputs, calls decide_lane(), and acts on the result.
Everything here is unit-testable without a server, a model, or a database.

Why this exists as data, not nested ternaries in the route: routes/agent/run.py used to
carry a six-deep lane ternary plus a six-deep reason ternary whose precedence orders did
not even match each other, and two hand-synchronized stop_reason->text dicts that had to
be edited in lockstep. All three are transcribed here verbatim - including the precedence
quirks - so the route shrinks and the policy gains tests.
"""

from dataclasses import dataclass
from enum import Enum

from .loop_guard import describe as _describe_guard


class Lane(str, Enum):
    """Which model lane runs a step. str-valued so existing `== "main"` comparisons,
    JSON payloads, and SSE events keep working unchanged."""
    MAIN = "main"
    EXECUTOR = "executor"


class StopReason(str, Enum):
    """How a run ended. Same reason: str-valued, drop-in for the string literals."""
    MAX_STEPS = "max_steps"
    TIMEOUT = "timeout"
    BUDGET = "budget"
    LOOP = "loop"
    LOOP_NEAR_REPEAT = "loop_near_repeat"
    NO_PROGRESS = "no_progress"


@dataclass(frozen=True)
class LaneInputs:
    """Everything the lane decision depends on, computed by the route before the call."""
    use_executor: bool = True
    executor_stuck: bool = False
    main_first: bool = False
    main_first_why: str = ""
    esc_sustained: bool = False
    esc_streak_reason: str = ""
    forced_main: bool = False
    force_main_why: str = ""
    plan_first: bool = False


def decide_lane(inp: LaneInputs) -> tuple:
    """(Lane, reason) for this step. Verbatim transcription of the route's ternaries,
    precedence quirks included: the reason chain checks plan_first before forced_main
    while the lane chain checks main_first before esc_sustained before forced_main.
    Do not "fix" the ordering here - any change alters which lane runs."""
    if (not inp.use_executor or inp.executor_stuck or inp.main_first
            or inp.esc_sustained or inp.forced_main or inp.plan_first):
        lane = Lane.MAIN
    else:
        lane = Lane.EXECUTOR
    if not inp.use_executor:
        reason = "no_executor"
    elif inp.executor_stuck:
        reason = "repeat_streak"
    elif inp.plan_first and not inp.forced_main and not inp.main_first and not inp.esc_sustained:
        reason = "plan_first"
    elif inp.forced_main:
        reason = inp.force_main_why
    elif inp.main_first:
        reason = inp.main_first_why
    elif inp.esc_sustained:
        reason = f"escalated_{inp.esc_streak_reason}"
    else:
        reason = "executor_default"
    return lane, reason


def describe_stop(detail: str) -> str:
    """'identical_result:run_python:3' -> a sentence (the guard's own wording)."""
    try:
        rule, tool, count = (detail or "").split(":")
        return _describe_guard(rule, tool, int(count))
    except ValueError:
        return "the run repeated itself"


@dataclass(frozen=True)
class StopContext:
    """Numbers the stop summary interpolates. All plain data, no handles."""
    steps: int = 0
    steps_run: int = 0
    run_limit_s: int = 0
    run_token_budget: int = 0
    run_prompt_tokens: int = 0
    loop_detail: str = ""
    elapsed_s: int = 0


def stop_note(reason: str, ctx: StopContext) -> str:
    """The short `note` for the stopped-done event (plan progress appended by the route)."""
    notes = {
        StopReason.MAX_STEPS.value: f"max steps reached ({ctx.steps})",
        StopReason.TIMEOUT.value: f"wall-clock limit reached ({ctx.run_limit_s}s)",
        StopReason.BUDGET.value: f"token budget reached ({ctx.run_token_budget:,} new tokens)",
        StopReason.LOOP.value: "stopped: repeating the same tool calls",
        StopReason.LOOP_NEAR_REPEAT.value: f"stopped: `{ctx.loop_detail}` called repeatedly without progress",
        StopReason.NO_PROGRESS.value: "stopped: no progress - " + describe_stop(ctx.loop_detail),
    }
    return notes.get(reason, "stopped")


def stop_headline(reason: str, ctx: StopContext) -> str:
    """The bold `why` line of the stopped summary."""
    headlines = {
        StopReason.MAX_STEPS.value: f"Reached the step limit ({ctx.steps} steps)",
        StopReason.TIMEOUT.value: f"Ran for {ctx.elapsed_s // 60} min (wall-clock limit)",
        StopReason.BUDGET.value: f"Used the run's token budget ({ctx.run_prompt_tokens:,} new tokens)",
        StopReason.LOOP.value: "Kept repeating the same tool calls",
        StopReason.LOOP_NEAR_REPEAT.value: f"Called `{ctx.loop_detail}` repeatedly without making progress",
        StopReason.NO_PROGRESS.value: "Stopped making progress: " + describe_stop(ctx.loop_detail),
    }
    return headlines.get(reason, "Stopped")


def parallel_spawn_eligible(name: str, plan_restricted: bool, plan_mode_tools) -> bool:
    """May this call take the parallel spawn_agent fan-out in routes/agent/run.py?

    The fan-out only runs fast_sandbox_check before dispatching to run_tool;
    the sequential gauntlet's plan-mode (`req.plan`) and plan-first
    (`plan_gate`) blocks live after it. A call either gate would stop must not
    take the fast path -- it falls through to the sequential loop, which
    reports the plan-mode error instead of executing. `plan_mode_tools` is
    passed in (routes/agent/constants.py PLAN_MODE_TOOLS) so this module keeps
    its no-routes-imports rule.
    """
    if plan_restricted and name not in (plan_mode_tools or ()):
        return False
    return True

from core.small_model import APP_CONFIG


AGENT_MAX_STEPS = 200


# identical-tool-call rounds after which a run is stopped (the executor escalates at 2)
LOOP_STOP_STREAK = 3


# times a run may be sent back to work when it answers with plan steps still pending,
# or with a bare announcement of work it never started (core/agent_loop/narration.py)
MAX_PLAN_NUDGES = 3
MAX_PASSIVITY_NUDGES = 2


# consecutive executor steps that trip the SAME escalation reason before the run
# commits to the main model. One forgiven failure keeps a single tutorialized or
# degenerate reply from silently ending orchestration for the whole run.
ESC_STREAK_LIMIT = 2


# Wall-clock ceiling for one agent run. The step cap alone does not protect the
# GPU: a model can make slow, novel, legitimate progress (or the loop detector can
# miss a near-repeat) and hold the card - and the open SSE request - indefinitely.
# 0 disables the ceiling. Overridable via config/app.json -> agent.run_timeout_s.
DEFAULT_RUN_TIMEOUT_S = 1800


# The progress-aware guard (core/agent_loop/loop_guard.py) is the loop detector. This name-count
# rule is only a high backstop for a run that thrashes without ever repeating a (call, result)
# pair: one tool this many times inside the window, whatever its arguments returned.
NEAR_REPEAT_LIMIT = 25


NEAR_REPEAT_WINDOW = 30


# Tools that are driven step by step with different arguments each time (a shell session, a
# browser or device test) get the whole window before the backstop applies.
NEAR_REPEAT_ITERATIVE_PREFIXES = ("run_shell", "browser_", "mobile_")
NEAR_REPEAT_ITERATIVE_LIMIT = 30


# The Continue button sends this prompt (static/js/agent-acts.js). A run that starts with it is a
# resume: it stays on the main model, because the previous run already showed the helper model
# could not carry this task.
AGENT_RESUME_PREFIX = "Continue the previous task"


def near_repeat_limit(tool: str) -> int:
    return NEAR_REPEAT_ITERATIVE_LIMIT if tool.startswith(NEAR_REPEAT_ITERATIVE_PREFIXES) else NEAR_REPEAT_LIMIT


def _run_timeout_s() -> int:
    try:
        v = int(APP_CONFIG.get("agent", {}).get("run_timeout_s", DEFAULT_RUN_TIMEOUT_S))
        return max(0, v)
    except (TypeError, ValueError):
        return DEFAULT_RUN_TIMEOUT_S


# Tools allowed in plan mode: read/explore only — nothing that mutates disk.
# create_plan/get_plan ARE allowed: the deliverable of plan mode is the tracked plan itself.
PLAN_MODE_TOOLS = {"list_files", "read_file", "grep", "search_memory", "list_skills", "read_skill",
                   "analyze_image", "web_fetch", "web_search", "create_plan", "get_plan",
                   # looking at the running app / device changes nothing in the project
                   "browser_navigate", "browser_snapshot", "browser_console",
                   "mobile_devices", "mobile_ui", "mobile_logs"}


# look-at-what-you-built loop on the executor lane too (registered only when
# capabilities.browser / capabilities.mobile are on)
EXECUTOR_TEST_TOOLS = ("browser_navigate", "browser_snapshot", "browser_click", "browser_type",
                       "browser_console", "browser_screenshot",
                       "browser_wait", "browser_press", "browser_select", "browser_eval", "browser_close",
                       "mobile_devices", "mobile_install", "mobile_launch", "mobile_ui", "mobile_tap",
                       "mobile_type", "mobile_screenshot", "mobile_logs")


# Tools safe to dispatch CONCURRENTLY in one step: they read independent state
# and never mutate disk/DB/context or need an interactive permission card.
# Deliberately an allow-list, so every new tool is sequential until reviewed.
# Missing on purpose: write tools, run_python/run_shell (arbitrary code +
# per-call permission cards), create_plan/update_plan_item (ordered state),
# finish (terminal), spawn_agent (handled by its own fan-out below), media
# generation (GPU/expensive), browser/mobile (stateful sessions, self-ordered).
PARALLEL_READ_TOOLS = frozenset({
    "read_file", "read_file_chunk", "grep", "list_files", "list_diff",
    "get_plan", "search_memory", "search_knowledge_base",
    "memory_read", "list_skills", "read_skill",
    "web_search", "web_fetch", "analyze_image", "doc_inspect",
})


PLAN_MODE_PROMPT = """

PLAN MODE ACTIVE — READ-ONLY.
You must NOT create, edit, write, or revert any files, and must not run code that
changes anything. Your job is to investigate, then produce an implementation plan.
1. Explore the workspace with read-only tools (list_files, read_file, grep, web_*) as needed.
2. Then call create_plan ONCE with your final ordered steps (the items array) so the plan is tracked and displayed to the user.
3. Then output the same plan as a clear numbered list: files to create/modify (exact paths), the change in each, and the execution order.
4. End with: 'Say "proceed" (or switch off Plan mode) to execute this plan.'
Never attempt file modifications in plan mode; mutating tools are unavailable."""


MAX_UPLOAD_BYTES = 50 * 1024 * 1024


MAX_UPLOAD_FILES = 20

from core.small_model import APP_CONFIG


AGENT_MAX_STEPS = 200


# identical-tool-call rounds after which a run is stopped (the executor escalates at 2)
LOOP_STOP_STREAK = 3


# times a run may be sent back to work when it answers with plan steps still pending,
# or with a bare announcement of work it never started (core/agent_loop/narration.py)
MAX_PLAN_NUDGES = 3


# consecutive executor steps that trip the SAME escalation reason before the run
# commits to the main model. One forgiven failure keeps a single tutorialized or
# degenerate reply from silently ending orchestration for the whole run.
ESC_STREAK_LIMIT = 2


# Wall-clock ceiling for one agent run. The step cap alone does not protect the
# GPU: a model can make slow, novel, legitimate progress (or the loop detector can
# miss a near-repeat) and hold the card - and the open SSE request - indefinitely.
# 0 disables the ceiling. Overridable via config/app.json -> agent.run_timeout_s.
DEFAULT_RUN_TIMEOUT_S = 1800


# same tool called this many times inside NEAR_REPEAT_WINDOW steps, with
# differing arguments, is treated as a thrash rather than progress
NEAR_REPEAT_LIMIT = 6


NEAR_REPEAT_WINDOW = 12


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
                       "mobile_devices", "mobile_install", "mobile_launch", "mobile_ui", "mobile_tap",
                       "mobile_type", "mobile_screenshot", "mobile_logs")


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

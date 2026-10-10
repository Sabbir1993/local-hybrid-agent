"""Planning / research without acting: the loop that no other guard sees.

A small model told to build something can keep re-creating its plan and looking things up (web_fetch, web_search,
reads) and never write a file or run a command. Each of those steps is different from the last, so the identical-
result guard (loop_guard.py) does not fire until much later, after real budget is gone.

`ActionGuard` counts consecutive steps made only of passive tools. At NUDGE_AT it says what to do now; at BAN_AT
the passive tools are withheld for BAN_STEPS steps so the model has to act; at STOP_AT the run stops with the usual
"no progress" summary.
"""

PASSIVE_TOOLS = frozenset({
    "create_plan", "get_plan", "web_fetch", "web_search", "read_file", "read_file_chunk", "grep", "list_files",
    "project_overview", "search_memory", "search_knowledge_base", "list_skills", "read_skill", "analyze_image",
    "doc_inspect",
})
NUDGE_AT, BAN_AT, STOP_AT = 3, 5, 8
BAN_STEPS = 2
RULE = "no_action"


class ActionGuard:
    def __init__(self):
        self.streak = 0
        self.ban_until = -1

    def banned(self, step: int) -> bool:
        return step < self.ban_until

    def record_step(self, tool_names: list, step: int) -> str:
        """Feed the tool names a finished step used. Returns "", "nudge", "ban" or "stop"."""
        if not tool_names or not all(n in PASSIVE_TOOLS for n in tool_names):
            self.streak = 0              # something was written, run, edited or delegated: that is progress
            return ""
        self.streak += 1
        if self.streak >= STOP_AT:
            return "stop"
        if self.streak == BAN_AT:
            self.ban_until = step + 1 + BAN_STEPS
            return "ban"
        if self.streak == NUDGE_AT:
            return "nudge"
        return ""


def nudge_message(streak: int, current_step_text: str = "") -> str:
    cur = f" The step you are on: {current_step_text}." if current_step_text else ""
    return (f"[act now] The last {streak} steps were only planning and looking things up; nothing was written or run."
            f"{cur} Do it now with a real action: write_file / edit_file for a file, or run_shell for a command "
            "(for a new project, run the standard scaffold command). Do not look anything up first - use the usual "
            "defaults and fix errors when they show up. Do not call create_plan again.")


def ban_message(steps: int = BAN_STEPS) -> str:
    return (f"[act now] Still no action. For the next {steps} steps the planning and lookup tools are withheld: "
            "call write_file, edit_file or run_shell (or finish with a clear statement of what blocks you).")

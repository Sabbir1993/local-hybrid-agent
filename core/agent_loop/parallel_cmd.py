"""`/parallel <task>`: the user asks for the task to be split across parallel sub-agents.

Before this, /parallel only pasted a sentence into the box and hoped the model would call spawn_parallel_agents; local
models almost never did. Now the command is a contract: the run's first step goes to the main model with
spawn_parallel_agents as the only tool and a tool call required, so the fan-out always happens. Later steps run as
usual, so the agent can read the combined results and answer (or fix what the sub-agents found).

Code mode only: the Personal Agent (Agent mode) blocks the spawn tools (core/request_context.PERSONAL_BLOCKED_TOOLS),
so there the command is just removed and the task runs normally.
"""
import re

_RX = re.compile(r"^\s*/parallel\b[ \t]*\n?", re.IGNORECASE)
TOOL = "spawn_parallel_agents"
MAX_FORCED_STEPS = 2          # a model that still does not call it after this many required steps is let go


def parse(text) -> tuple[bool, str]:
    """(asked for /parallel, the task without the command)."""
    if not isinstance(text, str):
        return False, ""
    m = _RX.match(text)
    if not m:
        return False, text
    return True, text[m.end():].strip()


def instruction(task: str) -> str:
    """The user's message as the model sees it on a /parallel run."""
    return ("[parallel] The user asked for this task to be split across parallel sub-agents. Call "
            "spawn_parallel_agents now with 2-4 agents. Give each agent ONE independent part with complete, "
            "self-contained instructions: the exact files, folders or questions it covers, and what to report back. "
            "Parts must not overlap and must not edit the same file. Use role='reviewer' or 'planner' for read-only "
            "parts and role='coder' only for parts that change files. When the results come back, check them and "
            "give the user one combined answer.\n\nTask: " + (task or "(no task given - ask the user what to split)"))


def wants_forced_step(active: bool, actions_taken: list, forced_steps: int) -> bool:
    """Should this step offer only spawn_parallel_agents and require a call?"""
    if not active or forced_steps >= MAX_FORCED_STEPS:
        return False
    return not any((a or {}).get("name") == TOOL for a in actions_taken or [])

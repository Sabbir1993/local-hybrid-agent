"""`/parallel <task>`: the user asks for the task to be split across parallel sub-agents.

Before this, /parallel only pasted a sentence into the box and hoped the model would call spawn_parallel_agents; local
models almost never did. Now the command is a contract: the run's first step goes to the main model with
spawn_parallel_agents as the only tool and a tool call required, so the fan-out always happens. Later steps run as
usual, so the agent can read the combined results and answer (or fix what the sub-agents found).

In Agent mode (the Personal Agent) it works only when the admin switch agent.personal_subagents is on
(core/request_context.personal_blocked_tools); otherwise, or when the agent's tool list leaves out 'Spawn Sub-Agent',
the command is removed, the task runs normally and the user is told why (unavailable_reason).
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


def unavailable_reason(personal: bool, hidden_tools, custom_agent_tools) -> str:
    """'' when this run may fan out, else a short reason for the user (the task then runs normally)."""
    if TOOL in (hidden_tools or ()):
        if personal:
            return ("Parallel sub-agents are off for Agent mode, so this task runs as one agent. An admin can turn them "
                    "on in Settings > Agent limits > 'Sub-agents in Agent mode'.")
        return "Parallel sub-agents are not available for this run, so this task runs as one agent."
    if custom_agent_tools and TOOL not in custom_agent_tools:
        return ("This agent's tool list does not include 'Spawn Sub-Agent', so this task runs as one agent. "
                "Edit the agent and tick it to use /parallel.")
    return ""


def wants_forced_step(active: bool, actions_taken: list, forced_steps: int) -> bool:
    """Should this step offer only spawn_parallel_agents and require a call?"""
    if not active or forced_steps >= MAX_FORCED_STEPS:
        return False
    return not any((a or {}).get("name") == TOOL for a in actions_taken or [])

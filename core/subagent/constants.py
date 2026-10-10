MAX_SUBAGENT_STEPS = 15
DEFAULT_SUBAGENT_STEPS = 8
# run_python: sub-agents have no SSE stream to raise the approval modal on
# media tools: cost money / long GPU jobs - only the main agent may use them
DENIED_TOOLS = {"run_shell", "run_tests", "git_commit", "git_branch", "run_python", "spawn_agent", "spawn_parallel_agents", "spawn_reviewed_coder", "generate_image", "generate_video"}

SUBAGENT_SYSTEM_PROMPT = """You are a focused sub-agent delegated a single, self-contained task \
by a parent AI coding agent. Workspace: {workspace}

You have no access to the parent's conversation -- work only from the task below.
Use the tools available to you to complete it, then give a concise final answer
summarizing what you found or changed. Do not ask clarifying questions; make
reasonable assumptions and state them if needed."""

SPAWN_AGENT_SCHEMA = {
    "type": "function",
    "function": {
        "name": "spawn_agent",
        "description": (
            "Delegate a self-contained sub-task to a short-lived specialized child agent. "
            "Use for focused work that benefits from an isolated context (e.g. 'research X and "
            "summarize', 'review this diff', 'write tests for this module') rather than doing it "
            "inline. The child has its own tool access and step budget and returns one synthesized "
            "result -- it cannot see or continue this conversation, and cannot itself delegate further."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "task": {
                    "type": "string",
                    "description": "Full, self-contained instructions for the sub-agent -- it has no access to this conversation's history.",
                },
                "role": {
                    "type": "string",
                    "description": (
                        "Optional named role controlling prompt/tools/max_steps: planner, coder, reviewer, "
                        "an Agent Library profile name, a custom-agent slug, or a skill name. "
                        "Skill names work as roles (e.g. 'code-review'). "
                        "Omit for a generic sub-agent on the executor lane."
                    ),
                },
                "lane": {
                    "type": "string",
                    "description": "Explicit model (lane) override, e.g. main or executor (ignored if role is set).",
                },
                "tools": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Optional explicit tool-name allowlist, further intersected with the role/default set. run_shell and spawn_agent are always excluded regardless of this list.",
                },
                "max_steps": {
                    "type": "integer",
                    "description": f"Step budget for the child, default from role or {DEFAULT_SUBAGENT_STEPS}, hard-capped at {MAX_SUBAGENT_STEPS}.",
                },
            },
            "required": ["task"],
        },
    },
}

SPAWN_PARALLEL_AGENTS_SCHEMA = {
    "type": "function",
    "function": {
        "name": "spawn_parallel_agents",
        "description": (
            "Dispatch 2 or more independent sub-agents to run concurrently in parallel. "
            "Use this when investigating multiple separate modules, test files, "
            "code paths, or research topics simultaneously to save time. "
            "All sub-agents run at the same time and return their combined findings."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "agents": {
                    "type": "array",
                    "description": "List of sub-agent specifications to execute concurrently in parallel.",
                    "items": {
                        "type": "object",
                        "properties": {
                            "task": {
                                "type": "string",
                                "description": "Full, self-contained instructions for this sub-agent.",
                            },
                            "role": {
                                "type": "string",
                                "description": "Optional role name: planner, coder, reviewer, explorer, skill name, or custom-agent slug.",
                            },
                            "lane": {
                                "type": "string",
                                "description": "Optional model (lane) override, e.g. main or executor.",
                            },
                            "tools": {
                                "type": "array",
                                "items": {"type": "string"},
                                "description": "Optional tool allowlist for this sub-agent.",
                            },
                            "max_steps": {
                                "type": "integer",
                                "description": f"Step budget (default {DEFAULT_SUBAGENT_STEPS}, max {MAX_SUBAGENT_STEPS}).",
                            },
                        },
                        "required": ["task"],
                    },
                },
            },
            "required": ["agents"],
        },
    },
}

BLACKBOARD_POST_SCHEMA = {
    "type": "function",
    "function": {
        "name": "blackboard_post",
        "description": "Post a verified discovery, key fact, or schema to the session blackboard for sibling subagents and the parent to read.",
        "parameters": {
            "type": "object",
            "properties": {
                "key": {"type": "string", "description": "Short topic key, e.g. 'auth_db_schema' or 'endpoint_path'"},
                "value": {"type": "string", "description": "The verified finding or content"},
            },
            "required": ["key", "value"],
        },
    },
}

BLACKBOARD_READ_SCHEMA = {
    "type": "function",
    "function": {
        "name": "blackboard_read",
        "description": "Read verified findings posted by other sub-agents or tasks from the session blackboard.",
        "parameters": {
            "type": "object",
            "properties": {
                "key": {"type": "string", "description": "Optional specific key to read. If omitted, returns all findings on the blackboard."},
            },
        },
    },
}

SPAWN_REVIEWED_CODER_SCHEMA = {
    "type": "function",
    "function": {
        "name": "spawn_reviewed_coder",
        "description": (
            "Execute a coding task with an automated Critic-Actor verification loop. "
            "A coder sub-agent writes/edits code and tests, then a reviewer sub-agent "
            "critiques the diff and either approves (VERDICT: APPROVED) or rejects with "
            "actionable feedback (VERDICT: REJECTED). If rejected, the coder is re-dispatched "
            "with feedback until approved or max iterations reached."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "task": {
                    "type": "string",
                    "description": "Full, self-contained coding instructions for the coder sub-agent.",
                },
                "max_iterations": {
                    "type": "integer",
                    "description": "Maximum number of critique/fix cycles (default 2, min 1, max 3).",
                },
                "coder_lane": {
                    "type": "string",
                    "description": "Optional model lane override for the coder (e.g. 'executor' or 'main').",
                },
                "reviewer_lane": {
                    "type": "string",
                    "description": "Optional model lane override for the reviewer (e.g. 'main').",
                },
            },
            "required": ["task"],
        },
    },
}


"""`finish` - the explicit end-of-run signal.

Plain text with no tool call is ambiguous: it can be the answer or a model that
stopped mid-thought. A model that ends with finish(answer) says which. The agent loop
(core/agent_loop/finish.py) turns the call into an ordinary final reply, so every check
that applies to a final reply still applies; this function only exists so the tool
resolves anywhere else the registry is used (sub-agents, MCP bridges).
"""

FINISH_SCHEMA = {
    "type": "function",
    "function": {
        "name": "finish",
        "description": ("Call once, as your last action, when the task is complete. Put the full answer "
                        "for the user in `answer`: what you found or did and the result. Do not call it "
                        "before the work is done."),
        "parameters": {
            "type": "object",
            "properties": {"answer": {"type": "string",
                                      "description": "The complete final answer shown to the user."}},
            "required": ["answer"],
        },
    },
}


def tool_finish(args: dict) -> str:
    return str((args or {}).get("answer") or "").strip()

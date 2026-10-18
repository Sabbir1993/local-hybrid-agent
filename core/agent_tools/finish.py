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

COMPLETE_TASK_SCHEMA = {
    "type": "function",
    "function": {
        "name": "complete_task",
        "description": (
            "Call once when your coding or workspace task is finished and verified. "
            "Provides a structured summary of what was implemented, the files modified, "
            "and verification results."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "summary": {
                    "type": "string",
                    "description": "Clear explanation of what was implemented or accomplished.",
                },
                "files_modified": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "List of relative paths created or modified in the workspace.",
                },
                "verification": {
                    "type": "string",
                    "description": "Verification steps, tests executed, or confirmation of correctness.",
                },
            },
            "required": ["summary"],
        },
    },
}


def tool_finish(args: dict) -> str:
    return str((args or {}).get("answer") or "").strip()


def tool_complete_task(args: dict) -> str:
    args = args or {}
    summary = str(args.get("summary") or args.get("answer") or "").strip()
    files = args.get("files_modified") or []
    verif = str(args.get("verification") or "").strip()
    parts = []
    if summary:
        parts.append(summary)
    if isinstance(files, list) and files:
        parts.append("\n**Modified Files:**\n" + "\n".join(f"- `{f}`" for f in files))
    if verif:
        parts.append(f"\n**Verification:**\n{verif}")
    return "\n\n".join(parts).strip() if parts else summary


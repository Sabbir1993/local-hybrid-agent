"""Sub-agent delegation: the `spawn_agent` tool + an isolated nested agent-loop runner.

A sub-agent is a new, isolated instance of the same kind of step loop `routes/agent.py`
runs for the top-level conversation: own message history, own tool subset, own step
budget, pinned to a lane or named role (core.roles). It is invoked synchronously
(awaited) from inside the parent's tool-call handling via run_tool("spawn_agent", ...)
-- no new SSE stream, no new process. The parent sees exactly one tool_call/tool_result
pair; internally the child may take several steps.

Deliberate scope cuts (see PROJECT_KNOWLEDGE.md / AGENT_GAP_ANALYSIS.md for the fuller
concurrency picture this stops short of):
  - Nesting is capped at depth 1: a sub-agent cannot itself call spawn_agent.
  - run_shell is unavailable to sub-agents: the shell permission-modal flow lives in
    routes/agent.py's SSE loop (_perm_pending/_await_permission), which this synchronous
    nested loop has no channel to reach. Never allow-list it around this, even via the
    `tools` arg -- that would mean unapproved shell exec.
  - _active_project is defensively restored if a sub-agent ever changes it, but
    _ws_changes is deliberately left untouched: sub-agent edits are the same
    user-visible task in the same workspace, so they must show up in the parent's
    list_diff/revert, not be hidden or reverted separately.

All cross-module imports below are deferred into function bodies on purpose: this
module is wired into core.agent_tools.TOOL_IMPLS/AGENT_TOOLS at the bottom of that
file, and core.agent_tools <-> core.agent_loop already import each other at module
scope, so importing them here at module scope too would risk a real circular import
depending on which module happens to be imported first.
"""

import contextlib
import json
import time
from typing import Optional

MAX_SUBAGENT_STEPS = 15
DEFAULT_SUBAGENT_STEPS = 8
DENIED_TOOLS = {"run_shell", "spawn_agent"}

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
                    "enum": ["planner", "coder", "reviewer"],
                    "description": "Optional named role controlling lane/prompt/tools/max_steps. Omit for a generic sub-agent on the executor lane.",
                },
                "lane": {
                    "type": "string",
                    "enum": ["main", "executor"],
                    "description": "Explicit lane override (ignored if role is set).",
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


@contextlib.asynccontextmanager
async def _subagent_scope():
    """Defensive guard: if a sub-agent's tool run ever mutates the active project
    global, restore it on exit so the parent's workspace pointer survives. Does NOT
    isolate _ws_changes -- see module docstring."""
    from .agent_tools import get_active_project, set_active_project
    before = get_active_project()
    try:
        yield
    finally:
        if get_active_project() != before:
            set_active_project(before)


async def run_subagent(task: str, role: Optional[str] = None, lane_override: Optional[str] = None,
                        tool_allowlist: Optional[list] = None, max_steps: Optional[int] = None) -> str:
    from .roles import resolve_role
    from .registry import registry
    from .agent_tools import active_workspace
    from .agent_loop import (
        run_tool,
        safe_parse_and_repair_args,
        validate_and_repair_tool_args,
        sanitize_user_facing_content,
        compact_messages,
    )
    from .small_model import small_models
    from .state import state
    from .monitor import monitor_begin, monitor_end, _monitor_state, parse_cache_tokens
    from .db import db_record_request
    from routes.common import _llm_chat_stream

    task = (task or "").strip()
    if not task:
        return "error: spawn_agent requires a non-empty task"

    role_cfg = resolve_role(role)
    lane = lane_override or role_cfg.get("lane") or "executor"
    if lane not in ("main", "executor"):
        lane = "executor"
    steps = min(MAX_SUBAGENT_STEPS, max(1, int(max_steps or role_cfg.get("max_steps") or DEFAULT_SUBAGENT_STEPS)))

    all_schemas = [t for t in registry.schemas() if t.get("function", {}).get("name") not in DENIED_TOOLS]
    names_ok = set(role_cfg.get("tools") or [t["function"]["name"] for t in all_schemas])
    if tool_allowlist:
        names_ok &= set(tool_allowlist)
    names_ok -= DENIED_TOOLS
    tools_for_subagent = [t for t in all_schemas if t["function"]["name"] in names_ok]

    sys_prompt = SUBAGENT_SYSTEM_PROMPT.format(workspace=str(active_workspace()))
    if role_cfg.get("system_prompt"):
        sys_prompt += "\n\n" + role_cfg["system_prompt"]

    msgs = [{"role": "system", "content": sys_prompt}, {"role": "user", "content": task}]

    async with _subagent_scope():
        if lane == "main":
            client = state.client
            if client is None:
                return "error: main lane is not available for a sub-agent run (model not loaded)"
        else:
            ex_inst = small_models.instances["executor"]
            if not ex_inst.available:
                return "error: executor lane is not available for a sub-agent run"
            await ex_inst.ensure_loaded()
            client = ex_inst.client

        model_name = f"subagent:{role or 'generic'}:{lane}"
        final_content = ""
        for _ in range(steps):
            ex_ctx = 16384
            msgs[:] = compact_messages(msgs, int(ex_ctx * 0.7))

            rid = monitor_begin("agent/subagent", True, json.dumps({"messages": msgs}).encode(),
                                 model=model_name, source="local")
            res = None
            try:
                async for ev, val in _llm_chat_stream(client, msgs, tools_for_subagent, 0.3, -1, rid=rid):
                    if ev == "result":
                        res = val
            finally:
                req_mon = _monitor_state["active"].get(rid)
                dt = (time.time() - req_mon["start"]) if req_mon else None
                u = (res or {}).get("usage") or {}
                tim = (res or {}).get("timings") or {}
                pcached, ccached = parse_cache_tokens(u, tim)
                ptoks = u.get("prompt_tokens") or (sum(len(m.get("content") or "") for m in msgs) // 4)
                ctoks = u.get("completion_tokens") or (req_mon.get("gen_tokens") if req_mon else 0)
                tps = (ctoks / dt) if (ctoks and dt and dt > 0) else None
                monitor_end(rid, 200, prompt_tokens=ptoks, completion_tokens=ctoks, tps=tps, duration=dt,
                            model=model_name, prompt_cached=pcached, completion_cached=ccached, source="local")
                db_record_request("agent/subagent", model_name, ptoks, ctoks, tps, dt, None, True, 200,
                                   prompt_cached_tokens=pcached, completion_cached_tokens=ccached,
                                   is_orchestrator=True, source="local")

            if res is None:
                final_content = "(sub-agent got no response from the model)"
                break
            content = res.get("content", "")
            tool_calls = res.get("tool_calls", [])
            if not tool_calls:
                final_content = content
                break

            history_content = sanitize_user_facing_content(content)
            clean_tcs = []
            for tc in tool_calls:
                fn = tc.get("function", {})
                name = fn.get("name", "?")
                a = safe_parse_and_repair_args(fn.get("arguments") or {}, name, task)
                a, _err = validate_and_repair_tool_args(name, a, task)
                clean_tcs.append({"id": tc.get("id") or f"sub_{name}", "type": "function",
                                   "function": {"name": name, "arguments": json.dumps(a)}})
            msgs.append({"role": "assistant", "content": history_content, "tool_calls": clean_tcs})

            for tc in clean_tcs:
                name = tc["function"]["name"]
                try:
                    a = json.loads(tc["function"]["arguments"])
                except Exception:
                    a = {}
                if name in DENIED_TOOLS:
                    result = f"error: '{name}' is unavailable to sub-agents"
                else:
                    result = await run_tool(name, a)
                msgs.append({"role": "tool", "tool_call_id": tc["id"], "content": result})
        else:
            final_content = final_content or "(sub-agent reached its step limit without a final answer)"

    header = f"[sub-agent" + (f" · role={role}" if role else "") + f" · {len(msgs) - 2} msgs · lane={lane}]"
    return f"{header}\n{final_content.strip()}"


async def tool_spawn_agent(args: dict) -> str:
    task = str(args.get("task") or "").strip()
    if not task:
        return "error: task is required"
    return await run_subagent(
        task=task,
        role=args.get("role"),
        lane_override=args.get("lane"),
        tool_allowlist=args.get("tools"),
        max_steps=args.get("max_steps"),
    )

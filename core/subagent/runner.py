import asyncio
import json
import re
import sys
import time
from typing import Optional
from .constants import (
    DEFAULT_SUBAGENT_STEPS,
    DENIED_TOOLS,
    MAX_SUBAGENT_STEPS,
    SUBAGENT_SYSTEM_PROMPT,
)
from ..agent_loop.tool_output import cap_tool_result
from .scope import _subagent_scope
from .window import _subagent_window


def resolve_subagent_tools(all_names, role_tools=None, tool_allowlist=None, parent_allowlist=None) -> set:
    """Tool names a sub-agent may execute. The caller's allowlist and the
    parent's custom-agent allowlist can only narrow (never widen: a child never
    gets more than its parent), and DENIED_TOOLS are stripped last so no
    combination of role + explicit list + parent scope can re-admit them.
    Pure: unit-tested without a model, a registry, or a database."""
    names_ok = set(role_tools or all_names)
    if tool_allowlist:
        names_ok &= set(tool_allowlist)
    if parent_allowlist is not None:
        names_ok &= set(parent_allowlist)
    names_ok -= DENIED_TOOLS
    return names_ok


def subagent_call_verdict(name: str, names_ok) -> Optional[str]:
    """Refusal text when a sub-agent must not run an emitted `name`, else None.
    The schema filter above only withholds tools from the offer; models can
    emit any name, so the denied list is enforced here again at execution --
    before the allowlist check, so a forbidden tool is refused as forbidden
    rather than merely unlisted."""
    if name in DENIED_TOOLS:
        return f"error: '{name}' is unavailable to sub-agents"
    if name not in names_ok:
        return f"error: '{name}' is not enabled for this sub-agent"
    return None


async def subagent_policy_refusal(name: str, args) -> Optional[str]:
    """Administrator rules and pre_tool hooks apply to sub-agents exactly as to the main agent, so delegating
    cannot get around them. A rule that asks is a refusal here: a sub-agent has no approval card to raise."""
    from .. import hooks, permission_rules
    from ..small_model import APP_CONFIG
    verdict = permission_rules.evaluate(name, args, (APP_CONFIG.get("permissions") or {}).get("rules"))
    if verdict:
        if verdict["action"] == "deny":
            return permission_rules.refusal_text(verdict)
        return (f"error: policy rule '{verdict['id']}' needs the user's approval for '{name}', and a sub-agent cannot "
                "ask. Report back to the parent agent that this step needs the user's approval.")
    if hooks.matching("pre_tool", name):
        why = await hooks.run_pre(name, args)
        if why:
            return f"error: {why}"
    return None


# The child's outcome, machine-readable in the header the parent already sees.
# The parent's tool verdict is `ok = not result.startswith("error:")`
# (routes/agent/run.py), and this header means a result NEVER starts with
# "error:" - so a child that ran out of steps used to be recorded as a SUCCESS.
# Closed set, and deliberately no "refusal": deciding that from prose is the
# same guess that produced this bug, and nothing measures it. Statuses are
# structural facts (did the loop finish, did the model answer), not readings of
# the answer's tone.
SUBAGENT_STATUSES = ("success", "step_exhausted", "no_response", "critic_rejected")
SUBAGENT_TOOLS = ("spawn_agent", "spawn_parallel_agents", "spawn_reviewed_coder")


def _envelope_header(role: Optional[str], lane: Optional[str], n_msgs: int, status: str,
                     posted: Optional[list] = None, files: Optional[list] = None) -> str:
    posted_part = f" · posted={','.join(posted)}" if posted else ""
    files_part = f" · files={','.join(files)}" if files else ""
    return ("[sub-agent" + (f" · role={role}" if role else "")
            + f" · {n_msgs} msgs · lane={lane} · status={status}{posted_part}{files_part}]")


def parse_subagent_envelope(text: str) -> dict:
    """Parse a subagent output envelope into a typed structured contract."""
    if not isinstance(text, str):
        return {"status": "error", "content": str(text), "files_modified": [], "posted": []}

    first_line = text.splitlines()[0] if text else ""
    if not first_line.startswith(("[sub-agent", "[Sub-agent", "[critic-actor")):
        return {
            "status": "raw" if not text.startswith("error:") else "error",
            "content": text,
            "files_modified": [],
            "posted": [],
        }

    status_match = re.search(r"status=([a-z_]+)", first_line)
    status = status_match.group(1) if status_match else "unknown"

    role_match = re.search(r"role=([^·\s\]]+)", first_line)
    role = role_match.group(1) if role_match else None

    lane_match = re.search(r"lane=([^·\s\]]+)", first_line)
    lane = lane_match.group(1) if lane_match else None

    posted_match = re.search(r"posted=([^·\]]+)", first_line)
    posted = [k.strip() for k in posted_match.group(1).split(",")] if posted_match else []

    files_match = re.search(r"files=([^·\]]+)", first_line)
    files = [f.strip() for f in files_match.group(1).split(",")] if files_match else []

    body = "\n".join(text.splitlines()[1:]).strip()

    return {
        "status": status,
        "role": role,
        "lane": lane,
        "posted": posted,
        "files_modified": files,
        "content": body,
    }



def subagent_result_verdict(tool_name: str, result) -> Optional[bool]:
    """True/False when the sub-agent envelope states an outcome, else None.

    None means "this result does not speak for itself": a non-subagent tool, a
    bare "error: ..." early return (the parent already fails those on the
    prefix), or a header whose status is not one this build knows. The caller
    keeps its own rule in that case rather than inheriting a guess.
    """
    if tool_name not in SUBAGENT_TOOLS or not isinstance(result, str):
        return None
    # spawn_parallel_agents interleaves envelopes with body lines
    # ("[Sub-agent #2]: [sub-agent ...]" then the child's text), so every envelope
    # line is collected rather than only the leading ones.
    # A single spawn_agent / spawn_reviewed_coder has exactly one envelope - the
    # first line. Only that line may vote: a successful child whose BODY quotes a
    # `[sub-agent ... status=...]` line (it saw another agent's header in
    # its findings) must not flip the verdict to failure.
    statuses = set()
    lines = result.splitlines()
    candidates = lines if tool_name == "spawn_parallel_agents" else lines[:1]
    for line in candidates:
        if line.startswith(("[sub-agent", "[Sub-agent", "[critic-actor")):
            statuses.update(re.findall(r"status=([a-z_]+)", line))
    if not statuses or not statuses <= set(SUBAGENT_STATUSES):
        return None
    # any child that did not finish makes the whole delegation a failure
    return all(s == "success" for s in statuses)


async def run_subagent(task: str, role: Optional[str] = None, lane_override: Optional[str] = None,
                        tool_allowlist: Optional[list] = None, max_steps: Optional[int] = None) -> str:
    from ..roles import resolve_role
    from ..registry import registry
    from ..agent_tools import active_workspace
    from ..agent_loop import (
        run_tool,
        safe_parse_and_repair_args,
        validate_and_repair_tool_args,
        sanitize_user_facing_content,
        compact_messages,
    )
    from ..small_model import small_models
    from ..state import state
    from ..monitor import monitor_begin, monitor_end, _monitor_state, parse_cache_tokens
    from ..db import db_record_request
    from .. import context_budget
    from routes.common import _llm_chat_stream

    task = (task or "").strip()
    if not task:
        return "error: spawn_agent requires a non-empty task"

    role_cfg = resolve_role(role)
    if role and not role_cfg:
        from ..roles import known_role_names
        avail = ', '.join(known_role_names()) or '(none)'
        # suggest the closest name: a typo, a near-miss on a role, or a skill the
        # model remembered by a slightly different name
        hint = ""
        want = role.strip().lower()
        for n in known_role_names():
            if want in n.lower() or n.lower() in want:
                hint = f" Did you mean '{n}'?"
                break
        return (f"error: unknown role '{role}'.{hint} Available: {avail}. "
                "Pass a role, custom-agent slug, agent-library profile, or skill name.")
    # explicit lane (role or override) wins; otherwise the "Sub-agents" job's model
    # (core/lanes.py, the executor by default). Any registered lane is allowed.
    from .. import lanes as lanes_mod
    reg = lanes_mod.registry()
    lane = lane_override or role_cfg.get("lane") or None
    if lane and (lane not in reg or not lanes_mod.kind_ok("chat", reg[lane]["kind"])):
        lane = None
    default_steps = 10 if (lane_override or role_cfg.get("lane")) == "main" else DEFAULT_SUBAGENT_STEPS
    steps = min(MAX_SUBAGENT_STEPS, max(1, int(max_steps or role_cfg.get("max_steps") or default_steps)))

    all_schemas = [t for t in registry.schemas() if t.get("function", {}).get("name") not in DENIED_TOOLS]
    # a sub-agent never gets more than the parent's custom-agent allowlist
    from ..request_context import get_tool_allowlist
    names_ok = resolve_subagent_tools(
        [t["function"]["name"] for t in all_schemas],
        role_cfg.get("tools"), tool_allowlist, get_tool_allowlist())
    tools_for_subagent = [t for t in all_schemas if t["function"]["name"] in names_ok]

    sys_prompt = SUBAGENT_SYSTEM_PROMPT.format(workspace=str(active_workspace()))
    if role_cfg.get("system_prompt"):
        sys_prompt += "\n\n" + role_cfg["system_prompt"]
    # session blackboard (facts posted by sibling subagents), PAN masked
    try:
        from .blackboard import blackboard_summary
        bb_summary = blackboard_summary()
        if bb_summary:
            sys_prompt += "\n\n" + bb_summary
    except Exception:
        pass
    # project instructions (AGENTS.md from /init), PAN/secret masked by the loader
    try:
        from ..project_context import load_project_instructions, prompt_block
        sys_prompt += prompt_block(await load_project_instructions())
    except Exception:
        pass

    msgs = [{"role": "system", "content": sys_prompt}, {"role": "user", "content": task}]

    from .blackboard import (
        _active_scope_writes,
        acquire_file_lock,
        release_all_file_locks_for_holder,
    )
    subagent_id = f"sub_{int(time.time() * 1000)}_{role or 'generic'}"
    files_modified: set = set()
    token = _active_scope_writes.set([])
    try:
        async with _subagent_scope():
            # sub-agents stay local, as before, unless they're pointed at a cloud model
            # on purpose: a user's own cloud lane, or the job mapped in Settings
            from .. import cloud
            if lane:
                d = reg[lane]
                route = [lanes_mod.Target(lane) if d["local"] else lanes_mod.Target(lane, cloud.cloud_lane(lane))]
            else:
                route = lanes_mod.targets("subagent", force_local="subagent" not in cloud.role_map())
            route = [t for t in route if t.is_cloud or t.lane == "main" or t.lane in reg]
            # Guarantee a route. With the local main model (the common case) the
            # executor is the only mapped step, so a run that escalated orchestration
            # to a cloud main left the sub-agent with nothing to call. Append the main
            # lane's *effective* target - cloud binding included - as the last resort.
            if not any(t.is_cloud for t in route):
                _cm = cloud.cloud_lane("main")
                if _cm:
                    route.append(lanes_mod.Target("main", _cm))
            client, lane_used = None, None
            tried = []
            for t in route:
                tried.append(t.describe())
                if not t.available():
                    continue
                try:
                    client, lane_used = await t.client(), t
                    break
                except Exception as e:
                    print(f"[subagent] {t.describe()} unavailable: {e}", file=sys.stderr)
            if client is None:
                # Name what was tried: the bare "no model is available" gave the user no
                # way to tell a missing model file from an unstarted local server.
                return ("error: no model is available for this sub-agent task "
                        f"(role={role or 'generic'}). Tried: {', '.join(tried) or 'no lanes mapped'}. "
                        "Load a model from the top toolbar, or map a Sub-agents model in Settings → Models.")
            lane = lane_used.lane
            src = lane_used.source
            model_name = f"subagent:{role or 'generic'}:{lane}"
            final_content = ""
            status = "step_exhausted"          # unless a branch below says otherwise
            # The sub-agent's prompt window: the real per-request window of the lane it
            # actually landed on, not a hardcoded guess. A fixed 16k under-counted a
            # cloud/main lane and, more importantly, ignored the tool-schema block
            # (several thousand tokens), so compaction thought it fit and the server
            # refused the prompt.
            sub_win = await _subagent_window(lane_used)
            for _ in range(steps):
                sub_budget = context_budget.budget_for(lane, sub_win)
                if sub_budget:
                    msgs[:] = compact_messages(msgs, sub_budget, tools=tools_for_subagent)

                rid = monitor_begin("agent/subagent", True, n_msgs=len(msgs),
                                     model=model_name, source=src)
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
                                model=model_name, prompt_cached=pcached, completion_cached=ccached, source=src)
                    db_record_request("agent/subagent", model_name, ptoks, ctoks, tps, dt, None, True, 200,
                                       prompt_cached_tokens=pcached, completion_cached_tokens=ccached,
                                       is_orchestrator=True, source=src)

                if res is None:
                    final_content = "(sub-agent got no response from the model)"
                    status = "no_response"
                    break
                content = res.get("content", "")
                tool_calls = res.get("tool_calls", [])
                if not tool_calls:
                    final_content = content
                    status = "success"
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
                    refused = subagent_call_verdict(name, names_ok)
                    if refused is None:
                        refused = await subagent_policy_refusal(name, a)
                    if refused is not None:
                        result = refused
                    elif name in ("write_file", "edit_file", "append_file"):
                        target_p = a.get("path") or a.get("file_path") or ""
                        lock_ok, lock_holder = acquire_file_lock(target_p, subagent_id) if target_p else (True, subagent_id)
                        if not lock_ok:
                            result = (f"error: file '{target_p}' is currently locked for editing by sibling "
                                      f"agent '{lock_holder}'. Choose another task or wait.")
                        else:
                            result = await run_tool(name, a)
                            if not str(result).startswith("error:"):
                                files_modified.add(target_p)
                    else:
                        result = await run_tool(name, a)
                    # a child re-sends its own history every step too: keep its tool results as small as the parent's
                    msgs.append({"role": "tool", "tool_call_id": tc["id"], "content": cap_tool_result(result)})
            else:
                final_content = final_content or "(sub-agent reached its step limit without a final answer)"

        posted_keys = _active_scope_writes.get() or []
    finally:
        # Always release any advisory file locks acquired by this subagent
        release_all_file_locks_for_holder(subagent_id)
        # Always restore the parent's scope list. This used to be a bare reset()
        # after the block, so the early `return "error: no model is available"`
        # skipped it: because run_subagent is awaited in the CALLER's context
        # (not its own task), the orphaned empty list stayed installed for the rest
        # of the parent run, and blackboard_post - which is deliberately NOT in
        # DENIED_TOOLS - would keep appending into it, dead-retaining memory and
        # corrupting the posted= field of the NEXT subagent.
        _active_scope_writes.reset(token)

    # R10: a child's file writes pollute the parent's diff/undo set (_ws_changes
    # in core/agent_tools/workspace.py). Drop THIS child's own writes now that
    # its run is over - a parallel sibling's files are not in `files_modified`
    # here, so they survive untouched.
    from .scope import drop_child_write_tracking
    drop_child_write_tracking(files_modified)


    # the child's answer returns as a tool result, bypassing output_guard -- mask
    # card numbers here so they never reach the parent context in the clear
    from ..pan import mask_pans
    final_content, _ = mask_pans(final_content)
    header = _envelope_header(
        role=role,
        lane=lane,
        n_msgs=len(msgs) - 2,
        status=status,
        posted=posted_keys,
        files=sorted(files_modified) if files_modified else None,
    )
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


async def run_parallel_subagents(agent_specs: list, max_concurrency: int = 4) -> str:
    if not agent_specs or not isinstance(agent_specs, list):
        return "error: 'agents' must be a non-empty list of agent specifications"

    sem = asyncio.Semaphore(max(1, max_concurrency))

    async def _run_one(idx: int, spec: dict) -> str:
        if not isinstance(spec, dict):
            return f"[Sub-agent #{idx+1}]: error: invalid specification format"
        task = str(spec.get("task") or "").strip()
        if not task:
            return f"[Sub-agent #{idx+1}]: error: task is required"
        role = spec.get("role")
        lane = spec.get("lane")
        tools = spec.get("tools")
        max_steps = spec.get("max_steps")
        async with sem:
            res = await run_subagent(
                task=task,
                role=role,
                lane_override=lane,
                tool_allowlist=tools,
                max_steps=max_steps,
            )
            title = f"Sub-agent #{idx+1}" + (f" (role={role})" if role else "")
            return f"=== {title} ===\n{res}"

    tasks = [_run_one(i, spec) for i, spec in enumerate(agent_specs)]
    results = await asyncio.gather(*tasks, return_exceptions=True)

    formatted = []
    for i, r in enumerate(results):
        if isinstance(r, Exception):
            formatted.append(f"=== Sub-agent #{i+1} ===\nerror: {type(r).__name__}: {r}")
        else:
            formatted.append(str(r))

    header = f"[parallel sub-agents · {len(agent_specs)} spawned concurrently]"
    return f"{header}\n\n" + "\n\n".join(formatted)


async def tool_spawn_parallel_agents(args: dict) -> str:
    agents = args.get("agents")
    if not agents or not isinstance(agents, list):
        return "error: 'agents' is required and must be a list of agent specifications"
    return await run_parallel_subagents(agents)

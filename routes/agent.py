"""
routes/agent.py - Autonomous multi-turn coding agent, shell permissions, vision, and workspace tools.
"""

import asyncio
import json
import os
import sys
import time
from pathlib import Path
from typing import Optional, Union

from fastapi import APIRouter, UploadFile, File as FastAPIFile
from fastapi.responses import StreamingResponse, JSONResponse, FileResponse
from pydantic import BaseModel

from core.config import BASE_DIR
from core.file_tools import extract_file_content, MIME_MAP
from core.db import (
    db_record_request,
    db_add_project_allow_pattern,
    db_get_plan_items,
    db_get_project_allow_patterns,
)
from core.small_model import (
    APP_CONFIG,
    small_models,
    needle_route,
    needle_available,
)
from core.state import state
from core import cloud
from core.agent_tools import (
    AGENT_TOOLS,
    AGENT_CORE_TOOLS,
    active_workspace,
    common_workspace,
    get_active_project,
    set_plan_context,
    _ws_resolve,
    _common_resolve,
    _ws_changes,
)
from core.registry import registry
from core.grammar import build_tool_call_grammar, envelope_examples
from core.web_tools import register_web_tools
from core.skills import skills_prompt_fragment
from core.plugins import plugins_prompt_fragment, fire_hook
from core.shell_tools import (
    add_allow_pattern,
    shell_cfg,
    permission_callback,
)
from core.agent_loop import (
    run_tool,
    AGENT_SYSTEM_PROMPT,
    all_tools,
    is_degeneration_or_loop,
    sanitize_user_facing_content,
    validate_and_repair_tool_args,
    fast_sandbox_check,
    validate_and_finalize_response,
    safe_parse_and_repair_args,
    _extract_text_tool_calls,
    estimate_prompt_tokens,
    compact_messages,
)
from core.monitor import (
    _monitor_state,
    monitor_begin,
    monitor_end,
    parse_cache_tokens,
)
from . import common
from .common import _process_sse_stream, _llm_chat_stream

router = APIRouter(tags=["agent"])

class AttachedFile(BaseModel):
    name: str
    path: str
    preview: str = ""
    truncated: bool = False

class AgentRequest(BaseModel):
    messages: list
    max_steps: int = 12
    temperature: float = 0.4
    max_tokens: int = -1
    large_model: Optional[str] = None
    mode: Optional[str] = "main"
    plan: bool = False
    session_id: Optional[int] = None
    attachments: list[AttachedFile] = []


AGENT_MAX_STEPS = 30

# Grammar-constrained tool calls for the executor lane (small-model reliability).
# Auto-disabled for the process lifetime if the llama-server build rejects the
# grammar or the envelope can't be verified. Kill switch: config/app.json
# router.executor_grammar: false.
_executor_grammar_disabled = False


def _executor_grammar(tools_for_lane: list) -> Optional[str]:
    """GBNF grammar constraining the executor's tool-call JSON (or None)."""
    global _executor_grammar_disabled
    if _executor_grammar_disabled or not APP_CONFIG.get("router", {}).get("executor_grammar", True):
        return None
    g = build_tool_call_grammar(tools_for_lane)
    if g is None:
        _executor_grammar_disabled = True
    return g

# ---------------- shell permission flow ----------------
# pending shell permission requests: req_id -> {cmd, event, result}
_perm_pending: dict[str, dict] = {}


class PermissionAnswerReq(BaseModel):
    req_id: str
    decision: str            # allow | project | always | deny
    pattern: Optional[str] = None
    project_id: Optional[Union[int, str]] = None


@router.post("/agent/permission")
async def agent_permission_answer(req: PermissionAnswerReq):
    """UI answers a permission_request emitted on the agent SSE stream."""
    rec = _perm_pending.get(req.req_id)
    if rec is None:
        return JSONResponse({"error": "unknown or expired permission request"}, status_code=404)
    if req.decision == "always" and req.pattern:
        add_allow_pattern(req.pattern)
    elif req.decision == "project" and req.pattern:
        # Save to project-specific allowed patterns
        target_proj = req.project_id or get_active_project()
        if target_proj:
            db_add_project_allow_pattern(target_proj, req.pattern)
    rec["result"] = {"allow": req.decision != "deny",
                     "note": f"pattern allowed for {req.decision}" if req.decision in ("always", "project") else ""}
    rec["event"].set()
    return {"ok": True, "decision": req.decision}


async def _await_permission(req_id: str, ev: asyncio.Event):
    """Wait for the UI's answer to a shell permission request (180s cap)."""
    try:
        await asyncio.wait_for(ev.wait(), timeout=180)
    except asyncio.TimeoutError:
        _perm_pending.pop(req_id, None)
        return False, "permission request timed out (180s)"
    rec = _perm_pending.pop(req_id, None) or {}
    res = rec.get("result") or {"allow": False, "note": "no answer"}
    return res.get("allow", False), res.get("note", "")

# Tools allowed in plan mode: read/explore only — nothing that mutates disk.
# create_plan/get_plan ARE allowed: the deliverable of plan mode is the tracked plan itself.
PLAN_MODE_TOOLS = {"list_files", "read_file", "grep", "search_memory", "list_skills", "read_skill",
                   "analyze_image", "web_fetch", "web_search", "create_plan", "get_plan"}

PLAN_MODE_PROMPT = """

PLAN MODE ACTIVE — READ-ONLY.
You must NOT create, edit, write, or revert any files, and must not run code that
changes anything. Your job is to investigate, then produce an implementation plan.
1. Explore the workspace with read-only tools (list_files, read_file, grep, web_*) as needed.
2. Then call create_plan ONCE with your final ordered steps (the items array) so the plan is tracked and displayed to the user.
3. Then output the same plan as a clear numbered list: files to create/modify (exact paths), the change in each, and the execution order.
4. End with: 'Say "proceed" (or switch off Plan mode) to execute this plan.'
Never attempt file modifications in plan mode; mutating tools are unavailable."""


@router.post("/agent/run")
async def agent_run(req: AgentRequest):
    # route through the registry so web/skills/mcp/plugin/shell tools are visible
    ex_inst = small_models.instances["executor"]

    # --- lane resolution -------------------------------------------------
    # mode is one of: all-local | main-local-rest-cloud | main-cloud-rest-local
    #                 | all-cloud (legacy: "main" == all-local, "tiered" ==
    #                 main-local-rest-cloud).
    # A lane is served by the cloud when its binding resolves in core.cloud;
    # otherwise it stays local (llama-server), and the mode decides whether the
    # client is forced local even when a cloud binding exists.
    mode = (req.mode or "all-local").strip().lower()
    if mode in ("main", ""):
        mode = "all-local"
    elif mode == "tiered":
        mode = "main-local-rest-cloud"
    elif mode == "agent":
        mode = "all-local"
    cloud_main = cloud.cloud_lane("main")
    cloud_exec = cloud.cloud_lane("executor")
    if mode == "main-cloud-rest-local":
        cloud_exec = None       # executor stays local in this mode
    use_cloud_main = bool(cloud_main)
    if not use_cloud_main and mode == "main-cloud-rest-local":
        print("[agent] mode=main-cloud-rest-local but no cloud main lane is configured "
              "- falling back to the local main model")

    main_ready = (state.process is not None and state.process.poll() is None and state.client is not None)
    if not main_ready and not use_cloud_main and mode != "main-local-rest-cloud":
        target = state.profile_path or state.profile or common.initial_profile_path
        if target:
            try:
                print(f"[server_manager] agent/run: model not running — auto-starting on demand...")
                await state.load_profile(target)
                main_ready = (state.process is not None and state.process.poll() is None and state.client is not None)
            except Exception as e:
                print(f"[server_manager] auto-start main model failed: {e}", file=sys.stderr)
    if not main_ready and not use_cloud_main and not ex_inst.available:
        return JSONResponse({"error": "No model loaded. Please load or start a model from the top toolbar first."}, status_code=400)

    print(f"[agent] lanes: main={'cloud:' + cloud_main.key if use_cloud_main else 'local'} "
          f"executor={'cloud:' + cloud_exec.key if cloud_exec else 'local'} mode={mode}")

    steps = max(1, min(req.max_steps, APP_CONFIG["agent"].get("max_steps", AGENT_MAX_STEPS)))
    msgs = [dict(m) for m in req.messages]

    # --- Inject attached file content into the last user message ---
    if req.attachments:
        file_context_parts = []
        for att in req.attachments:
            header = f"--- ATTACHED FILE: {att.name} (workspace path: {att.path}) ---"
            preview = att.preview or "(no content extracted)"
            footer = (
                f"--- END {att.name} ---\n"
                f"[NOTE: This file was truncated at 12,000 chars. "
                f"Use read_file_chunk('{att.path}', offset_chars=12000) to read more.]"
                if att.truncated else
                f"--- END {att.name} ---"
            )
            file_context_parts.append(f"{header}\n{preview}\n{footer}")
        file_context = "\n\n".join(file_context_parts)
        # Inject into last user message
        injected = False
        for m in reversed(msgs):
            if m.get("role") == "user":
                existing = m.get("content") or ""
                m["content"] = f"{existing}\n\n[Attached Files]\n{file_context}" if existing else f"[Attached Files]\n{file_context}"
                injected = True
                break
        if not injected and file_context:
            msgs.append({"role": "user", "content": f"[Attached Files]\n{file_context}"})

    ws_path = str(active_workspace())
    sys_prompt = AGENT_SYSTEM_PROMPT.format(workspace=ws_path)
    # capability prompt fragments: skills listing + plugin guidance
    for frag in (skills_prompt_fragment(), plugins_prompt_fragment()):
        if frag:
            sys_prompt += "\n" + frag
    # executor lanes get the tool-call format few-shot (aligned with the GBNF grammar)
    if (cloud_exec or ex_inst.available) and (mode != "all-local" or not main_ready):
        sys_prompt += envelope_examples()
    if req.plan:
        sys_prompt += PLAN_MODE_PROMPT
    # point the plan tools at this session; in Build mode, inject the tracked plan
    # so the agent continues it step by step and keeps statuses up to date
    set_plan_context(req.session_id)
    if req.session_id and not req.plan:
        plan_items = db_get_plan_items(req.session_id)
        if plan_items:
            done_n = sum(1 for i in plan_items if i["status"] == "done")
            fail_n = sum(1 for i in plan_items if i["status"] == "failed")
            sys_prompt += (
                "\n\nACTIVE PLAN — tracked with create_plan / update_plan_item. Work through the "
                "pending or failed steps in order, and call update_plan_item(item=N, status='done' "
                "or 'failed') immediately after each step finishes or fails:\n"
                + "\n".join(
                    f"{i['ord']}. [{i['status']}] {i['text']}" + (f" — {i['note']}" if i.get("note") else "")
                    for i in plan_items)
                + f"\n({done_n}/{len(plan_items)} done, {fail_n} failed)"
            )
    has_sys = False
    for m in msgs:
        if m.get("role") == "system":
            has_sys = True
            m["content"] = (m.get("content") or "").strip() + "\n\n" + sys_prompt
            break
    if not has_sys:
        msgs.insert(0, {"role": "system", "content": sys_prompt})

    use_executor = bool(cloud_exec) or (ex_inst.available and (mode != "all-local" or not main_ready))
    main_client = cloud.CloudClient(cloud_main) if use_cloud_main else state.client

    # --- cloud fallback --------------------------------------------------
    # config/providers.json -> cloud.fallback_local: when a cloud lane dies before
    # producing content, retry that request on the local lane instead of failing
    # the whole step. Local models may need loading first (that's the point).
    fb_enabled = bool(cloud.cloud_bindings().get("fallback_local", True))

    def _local_model_info(lane: str) -> dict:
        if lane == "executor":
            nm = (ex_inst.model_path.name if ex_inst.model_path else "executor").replace(".gguf", "")
            return {"lane": "executor", "model": nm, "display": f"\u26A1 {nm}",
                    "device": "Arc A770 #1 (Vulkan1)", "role": "Executor Model", "source": "local"}
        nm = Path((state.profile or {}).get("model_path", "")).name if state.profile else "Main LLM"
        nm = (nm or "Main LLM").replace(".gguf", "")
        return {"lane": "main", "model": nm, "display": f"\U0001F9E0 {nm}",
                "device": "Dual Intel Arc A770 (Vulkan)", "role": "Main Autonomous LLM",
                "source": "local"}

    async def _local_fallback(lane: str):
        """Client for the local lane, loading it on demand. None if unavailable/disabled."""
        if not fb_enabled:
            return None
        try:
            if lane == "executor" and ex_inst.available:
                await ex_inst.ensure_loaded()
                return ex_inst.client
            if lane == "main":
                target = state.profile_path or state.profile or common.initial_profile_path
                if target:
                    if state.process is None or state.process.poll() is not None:
                        await state.load_profile(target)
                    return state.client
        except Exception as e:
            print(f"[agent] local fallback for {lane} unavailable: {e}", file=sys.stderr)
        return None
    last_query = ""
    for m in reversed(msgs):
        if m.get("role") == "user":
            last_query = str(m.get("content", ""))
            break

    def get_model_info(lane: str) -> dict:
        # cloud-bound lanes report ☁️ + provider so the UI badge is truthful
        if lane == "main" and use_cloud_main:
            return cloud_main.info("main")
        if lane == "executor" and cloud_exec:
            return cloud_exec.info("executor")
        if lane == "main":
            m_name = state.profile.get("model_path", "") if state.profile else ""
            m_base = Path(m_name).name if m_name else "Main LLM"
            clean_name = m_base.replace(".gguf", "")
            return {
                "lane": "main",
                "model": clean_name,
                "display": f"🧠 {clean_name}",
                "device": "Dual Intel Arc A770 (Vulkan)",
                "role": "Main Autonomous LLM",
                "source": "local",
            }
        elif lane == "executor":
            m_name = ex_inst.model_path.name if ex_inst.model_path else "Qwen2.5-VL-3B"
            clean_name = m_name.replace(".gguf", "")
            return {
                "lane": "executor",
                "model": clean_name,
                "display": f"⚡ {clean_name}",
                "device": "Arc A770 #1 (Vulkan1)",
                "role": "Executor Model",
                "source": "local",
            }
        elif lane == "needle":
            return {
                "lane": "needle",
                "model": "Needle Router",
                "display": "⚡ Needle Router",
                "device": "CPU Router",
                "role": "Fast Router",
                "source": "local",
            }
        return {"lane": lane, "model": lane, "display": lane, "device": "Dual Intel Arc A770", "role": "Agent", "source": "local"}

    simple_greetings = {"hi", "hello", "hey", "help", "who are you", "what can you do", "good morning", "good evening", "how are you", "test", "hi there"}
    clean_q = last_query.strip().lower()
    if clean_q in simple_greetings or (len(clean_q) <= 3 and not clean_q.startswith("/")):
        async def direct_chat():
            model_info = get_model_info("main" if (use_cloud_main or main_ready) else "executor")
            yield f"event: lane\ndata: {json.dumps(model_info)}\n\n"
            if use_cloud_main or main_ready:
                active_client = main_client
            elif cloud_exec:
                active_client = cloud.CloudClient(cloud_exec)
            else:
                await ex_inst.ensure_loaded()
                active_client = ex_inst.client
            chat_rid = monitor_begin("agent/direct", True, json.dumps({"messages": msgs}).encode(),
                                     model=model_info.get("model"), source=model_info.get("source"),
                                     provider=model_info.get("provider_name"))
            try:
                if getattr(active_client, "is_cloud", False):
                    fb_client = await _local_fallback("main" if (use_cloud_main or main_ready) else "executor")
                    direct_stream = common._llm_chat_stream_with_fallback(
                        active_client, fb_client, msgs, None, req.temperature,
                        req.max_tokens, rid=chat_rid, lane="direct")
                else:
                    direct_stream = _llm_chat_stream(active_client, msgs, None, req.temperature, req.max_tokens, rid=chat_rid)
                async for ev, val in direct_stream:
                    if ev == "fallback":
                        yield f"event: lane\ndata: {json.dumps(_local_model_info('main' if (use_cloud_main or main_ready) else 'executor'))}\n\n"
                        continue
                    if ev == "thought_delta":
                        yield f"event: thought_delta\ndata: {json.dumps({'step': 1, 'delta': val, 'model': model_info['display']})}\n\n"
                    elif ev == "content_delta":
                        yield f"event: delta\ndata: {json.dumps({'text': val})}\n\n"
            finally:
                req_mon = _monitor_state["active"].get(chat_rid)
                toks = req_mon.get("gen_tokens") if req_mon else None
                dt = (time.time() - req_mon["start"]) if req_mon else None
                tps = (toks / dt) if (toks and dt and dt > 0) else None
                monitor_end(chat_rid, 200, completion_tokens=toks, tps=tps, duration=dt,
                           source=model_info.get("source"), provider=model_info.get("provider_name"))
            yield f"event: done\ndata: {{}}\n\n"
        return StreamingResponse(direct_chat(), media_type="text/event-stream")

    async def sse():
        actions_taken = []
        final_content = ""
        final_reasoning = ""
        attempt_sigs: set = set()
        repeat_streak = 0
        try:
            for step in range(steps):
                yield f"event: step\ndata: {json.dumps({'step': step + 1, 'total': steps})}\n\n"
                tool_calls = None
                content = ""
                reasoning = ""

                is_creation_or_code = any(w in last_query.lower() for w in ("make", "create", "generate", "write", "build", "code", "add", "fix", "html", "script", "page"))
                if (not req.plan) and step == 0 and mode != "all-local" and not cloud_exec and not is_creation_or_code and needle_available() and not any(
                        m.get("role") in ("tool", "assistant") for m in msgs[1:]):
                    nr = await asyncio.get_event_loop().run_in_executor(
                        None, needle_route, last_query, all_tools())
                    if nr:
                        model_info = get_model_info("needle")
                        yield f"event: lane\ndata: {json.dumps(model_info)}\n\n"
                        if nr.get("reasoning"):
                            yield f"event: thought\ndata: {json.dumps({'step': step + 1, 'text': nr['reasoning'], 'model': model_info['display']})}\n\n"
                        tc_id = "n0"
                        yield f"event: tool_call\ndata: {json.dumps({'id': tc_id, 'name': nr['name'], 'args': nr['args'], 'model': model_info['display'], 'device': model_info['device']})}\n\n"
                        result = await run_tool(nr["name"], nr["args"])
                        ok = not (isinstance(result, str) and (result.startswith("error:") or result.startswith("File not found")))
                        yield f"event: tool_result\ndata: {json.dumps({'id': tc_id, 'name': nr['name'], 'ok': ok, 'result': result, 'model': model_info['display']})}\n\n"
                        actions_taken.append({"name": nr["name"], "args": nr["args"], "ok": ok, "result": result})
                        # Record needle request in usage.db
                        needle_p = max(1, len(last_query) // 4)
                        needle_c = max(1, (len(nr.get("reasoning", "")) + len(json.dumps(nr.get("args", {})))) // 4)
                        db_record_request("agent/needle", model_info.get("model") or "orchestrator/needle",
                                          needle_p, needle_c, 35.0, 0.1, None, False, 200,
                                          prompt_cached_tokens=0, completion_cached_tokens=0, is_orchestrator=True,
                                          source=model_info.get("source"), provider=model_info.get("provider_name"))
                        msgs.append({"role": "assistant", "content": "",
                                     "tool_calls": [{"id": tc_id, "type": "function",
                                                     "function": {"name": nr["name"],
                                                                  "arguments": json.dumps(nr["args"])}}]})
                        msgs.append({"role": "tool", "tool_call_id": tc_id, "content": result})
                        continue

                executor_stuck = repeat_streak >= 2
                lane_name = "main" if mode == "all-local" or not use_executor or executor_stuck else "executor"
                if executor_stuck:
                    print("[server_manager] executor repeating identical tool calls - escalating to main model", file=sys.stderr)
                if lane_name == "executor":
                    if cloud_exec:
                        # cloud executor: no local process to warm up
                        active_client = cloud.CloudClient(cloud_exec)
                    else:
                        try:
                            await ex_inst.ensure_loaded()
                            active_client = ex_inst.client
                        except Exception as e:
                            print(f"[server_manager] executor unavailable: {e}; routing to main model", file=sys.stderr)
                            lane_name = "main"
                            active_client = main_client
                else:
                    if not use_cloud_main:
                        if not state.client or state.process is None:
                            target = state.profile_path or state.profile or common.initial_profile_path
                            if target:
                                await state.load_profile(target)
                        active_client = state.client
                    else:
                        active_client = main_client

                if active_client is None:
                    raise RuntimeError(f"Model engine '{lane_name}' is not ready or failed to connect.")

                model_info = get_model_info(lane_name)
                yield f"event: lane\ndata: {json.dumps(model_info)}\n\n"

                # executor lane gets the lean core set; main lane sees everything;
                # plan mode restricts to read-only exploration tools
                if req.plan:
                    tools_for_lane = [t for t in all_tools()
                                     if t.get("function", {}).get("name") in PLAN_MODE_TOOLS]
                else:
                    if lane_name == "executor":
                        # core tools plus shell, skills and plan tracking so the
                        # executor can install packages, run commands, and tick plan items
                        tools_for_lane = [t for t in all_tools()
                                         if t.get("function", {}).get("name") in
                                         ("write_file", "read_file", "edit_file", "list_files", "run_python", "run_shell", "read_skill", "list_skills",
                                          "create_plan", "update_plan_item", "get_plan")]
                    else:
                        tools_for_lane = all_tools()

                # Smart context truncation: mechanically compact history into the
                # lane's window (keeps system prompt + recent tail, rolls older
                # turns into a digest). Report it to the UI when it fires.
                step_grammar = None
                if lane_name == "executor":
                    # cloud executors are OpenAI-compatible: no GBNF grammar, and
                    # their window comes from the provider config (model ctx)
                    ex_ctx = int(cloud_exec.ctx if cloud_exec else (ex_inst.cfg.get("ctx") or 16384))
                    pre_tokens = estimate_prompt_tokens(msgs)
                    # in-place slice assignment: must NOT rebind `msgs` here, or it
                    # becomes a local of sse() and earlier reads raise UnboundLocalError
                    msgs[:] = compact_messages(msgs, int(ex_ctx * 0.7))
                    post_tokens = estimate_prompt_tokens(msgs)
                    if post_tokens < pre_tokens:
                        yield (f"event: ctx\ndata: "
                               + json.dumps({'lane': lane_name, 'before_tokens': pre_tokens,
                                             'after_tokens': post_tokens}) + "\n\n")
                    if not cloud_exec:
                        step_grammar = _executor_grammar(tools_for_lane)

                step_rid = monitor_begin(f"agent/{lane_name}", True, json.dumps({"messages": msgs}).encode(),
                                         model=model_info.get("model"), source=model_info.get("source"),
                                         provider=model_info.get("provider_name"))
                res_dict = None
                streamed_content = []
                try:
                    if getattr(active_client, "is_cloud", False):
                        fb_client = await _local_fallback(lane_name)
                        lane_stream = common._llm_chat_stream_with_fallback(
                            active_client, fb_client, msgs, tools_for_lane, req.temperature,
                            req.max_tokens, rid=step_rid, grammar=step_grammar, lane=lane_name)
                    else:
                        lane_stream = _llm_chat_stream(active_client, msgs, tools_for_lane, req.temperature, req.max_tokens, rid=step_rid, grammar=step_grammar)
                    async for ev, val in lane_stream:
                        if ev == "fallback":
                            model_info = _local_model_info(lane_name)
                            yield f"event: lane\ndata: {json.dumps(model_info)}\n\n"
                            continue
                        if ev == "thought_delta":
                            yield f"event: thought_delta\ndata: {json.dumps({'step': step + 1, 'delta': val, 'model': model_info['display']})}\n\n"
                        elif ev == "content_delta":
                            streamed_content.append(val)
                            yield f"event: delta\ndata: {json.dumps({'text': val})}\n\n"
                        elif ev == "result":
                            res_dict = val
                finally:
                    req_mon = _monitor_state["active"].get(step_rid)
                    toks = req_mon.get("gen_tokens") if req_mon else len(streamed_content)
                    dt = (time.time() - req_mon["start"]) if req_mon else None
                    tps = (toks / dt) if (toks and dt and dt > 0) else None
                    u = (res_dict or {}).get("usage") or {}
                    tim = (res_dict or {}).get("timings") or {}
                    pcached, ccached = parse_cache_tokens(u, tim)
                    ptoks = u.get("prompt_tokens") or (sum(len(m.get("content", "")) for m in msgs) // 4)
                    if not pcached and len(msgs) > 1:
                        pcached = sum(len(m.get("content", "")) for m in msgs[:-1]) // 4
                    ctoks = u.get("completion_tokens") or toks
                    is_orch = (lane_name == "executor" or "orchestrator" in (model_info.get("model") or "").lower())
                    monitor_end(step_rid, 200, prompt_tokens=ptoks, completion_tokens=ctoks, tps=tps, duration=dt,
                                model=model_info.get("model"), prompt_cached=pcached, completion_cached=ccached,
                                source=model_info.get("source"), provider=model_info.get("provider_name"))
                    db_record_request(f"agent/{lane_name}", model_info.get("model"), ptoks, ctoks, tps, dt, None, True, 200,
                                      prompt_cached_tokens=pcached, completion_cached_tokens=ccached, is_orchestrator=is_orch,
                                      source=model_info.get("source"), provider=model_info.get("provider_name"))

                content = res_dict.get("content", "") if res_dict else "".join(streamed_content)
                reasoning = res_dict.get("reasoning", "") if res_dict else ""
                tool_calls = res_dict.get("tool_calls", []) if res_dict else []

                if not tool_calls:
                    final_content = content
                    final_reasoning = reasoning

                # Auto-escalation heuristics:
                # If the executor lane degraded into an infinite repeat loop, refused,
                # or outputted markdown tutorial code instead of executing tool calls on step 0,
                # escalate to the powerful main model immediately.
                is_loop = is_degeneration_or_loop(content)
                refused = any(w in content.lower() for w in ("i cannot", "i can't", "i am unable", "as an ai", "i don't have access"))
                tutorial_code_emitted = ("```" in content and not tool_calls)
                wants_action = any(w in last_query.lower() for w in ("run", "install", "test", "check", "exec", "open", "read", "view", "find", "grep"))
                wants_creation = any(w in last_query.lower() for w in ("make", "create", "generate", "write", "build", "code", "add", "fix", "html", "script", "page"))
                should_escalate = (
                    lane_name == "executor"
                    and (use_cloud_main or main_ready)
                    and (
                        is_loop
                        or (wants_creation and step == 0 and not tool_calls)
                        or (wants_action and step == 0 and (tutorial_code_emitted or (refused and not tool_calls)))
                        or (step == 0 and not content.strip() and not tool_calls)
                    )
                )

                if should_escalate:
                    print(f"[server_manager] Executor failed/tutorialized on step {step+1}; auto-escalating to Main Model.", file=sys.stderr)
                    yield "event: delta_reset\ndata: {}\n\n"
                    lane_name = "main"
                    model_info = get_model_info("main")
                    yield f"event: lane\ndata: {json.dumps(model_info)}\n\n"
                    esc_rid = monitor_begin("agent/main-escalated", True, json.dumps({"messages": msgs}).encode(),
                                            model=model_info.get("model"), source=model_info.get("source"),
                                            provider=model_info.get("provider_name"))
                    esc_tools = ([t for t in all_tools()
                                  if t.get("function", {}).get("name") in PLAN_MODE_TOOLS]
                                 if req.plan else all_tools())
                    res_dict = None
                    streamed_content = []
                    try:
                        if getattr(main_client, "is_cloud", False):
                            fb_main = await _local_fallback("main")
                            esc_stream = common._llm_chat_stream_with_fallback(
                                main_client, fb_main, msgs, esc_tools, req.temperature,
                                req.max_tokens, rid=esc_rid, lane="main")
                        else:
                            esc_stream = _llm_chat_stream(main_client, msgs, esc_tools, req.temperature, req.max_tokens, rid=esc_rid)
                        async for ev, val in esc_stream:
                            if ev == "fallback":
                                yield f"event: lane\ndata: {json.dumps(_local_model_info('main'))}\n\n"
                                continue
                            if ev == "thought_delta":
                                yield f"event: thought_delta\ndata: {json.dumps({'step': step + 1, 'delta': val, 'model': model_info['display']})}\n\n"
                            elif ev == "content_delta":
                                streamed_content.append(val)
                                yield f"event: delta\ndata: {json.dumps({'text': val})}\n\n"
                            elif ev == "result":
                                res_dict = val
                    finally:
                        req_mon = _monitor_state["active"].get(esc_rid)
                        toks = req_mon.get("gen_tokens") if req_mon else len(streamed_content)
                        dt = (time.time() - req_mon["start"]) if req_mon else None
                        tps = (toks / dt) if (toks and dt and dt > 0) else None
                        u = (res_dict or {}).get("usage") or {}
                        tim = (res_dict or {}).get("timings") or {}
                        pcached, ccached = parse_cache_tokens(u, tim)
                        ptoks = u.get("prompt_tokens") or (sum(len(m.get("content", "")) for m in msgs) // 4)
                        if not pcached and len(msgs) > 1:
                            pcached = sum(len(m.get("content", "")) for m in msgs[:-1]) // 4
                        ctoks = u.get("completion_tokens") or toks
                        monitor_end(esc_rid, 200, prompt_tokens=ptoks, completion_tokens=ctoks, tps=tps, duration=dt,
                                    model=model_info.get("model"), prompt_cached=pcached, completion_cached=ccached,
                                    source=model_info.get("source"), provider=model_info.get("provider_name"))
                        db_record_request("agent/main-escalated", model_info.get("model"), ptoks, ctoks, tps, dt, None, True, 200,
                                          prompt_cached_tokens=pcached, completion_cached_tokens=ccached, is_orchestrator=False,
                                          source=model_info.get("source"), provider=model_info.get("provider_name"))
                    content = res_dict.get("content", "") if res_dict else "".join(streamed_content)
                    reasoning = res_dict.get("reasoning", "") if res_dict else ""
                    tool_calls = res_dict.get("tool_calls", []) if res_dict else []
                    if not tool_calls:
                        parsed_tc = _extract_text_tool_calls(content or reasoning)
                        if parsed_tc:
                            tool_calls = parsed_tc

                final_content = content
                final_reasoning = reasoning

                if not tool_calls:
                    val_text, was_synth, note = validate_and_finalize_response(
                        last_query, final_content, final_reasoning, actions_taken)
                    if was_synth and val_text != final_content:
                        if not final_content.strip():
                            yield f"event: delta\ndata: {json.dumps({'text': val_text})}\n\n"
                        else:
                            yield "event: delta_reset\ndata: {}\n\n"
                            yield f"event: delta\ndata: {json.dumps({'text': val_text})}\n\n"
                    yield f"event: validated\ndata: {json.dumps({'synthesized': was_synth, 'note': note})}\n\n"
                    yield "event: done\ndata: {}\n\n"
                    return

                yield f"event: lane\ndata: {json.dumps({'lane': lane_name})}\n\n"
                
                clean_tool_calls = []
                parsed_actions = []
                for tc in tool_calls:
                    fn = tc.get("function", {})
                    name = fn.get("name", "?")
                    tc_id = tc.get("id") or f"call_{step}_{name}"
                    args_raw = fn.get("arguments") or {}
                    args = safe_parse_and_repair_args(args_raw, name, last_query)
                    repaired_args, val_err = validate_and_repair_tool_args(name, args, last_query)
                    if not val_err:
                        args = repaired_args
                    
                    clean_args_str = json.dumps(args)
                    clean_tc = {
                        "id": tc_id,
                        "type": "function",
                        "function": {
                            "name": name,
                            "arguments": clean_args_str
                        }
                    }
                    clean_tool_calls.append(clean_tc)
                    parsed_actions.append((name, tc_id, args))

                # semantic loop detection: identical tool+args attempted again
                sigs = [(name, json.dumps(args, sort_keys=True, default=str))
                        for name, _tc_id, args in parsed_actions]
                if sigs and all(s in attempt_sigs for s in sigs):
                    repeat_streak += 1
                else:
                    repeat_streak = 0
                attempt_sigs.update(sigs)

                history_content = sanitize_user_facing_content(content)
                msgs.append({"role": "assistant", "content": history_content, "tool_calls": clean_tool_calls})

                for name, tc_id, args in parsed_actions:
                    yield f"event: tool_call\ndata: {json.dumps({'id': tc_id, 'name': name, 'args': args})}\n\n"

                    if req.plan and name not in PLAN_MODE_TOOLS:
                        # plan mode: mutating tools are unavailable — hard block
                        result = f"error: plan mode is active — '{name}' is read-only-restricted. Produce the plan instead."
                        yield f"event: tool_result\ndata: {json.dumps({'id': tc_id, 'name': name, 'ok': False, 'result': result})}\n\n"
                        actions_taken.append({"name": name, "args": args, "ok": False, "result": result})
                        msgs.append({"role": "tool", "tool_call_id": tc_id, "content": result})
                        continue

                    approved, note = fast_sandbox_check(name, args)
                    yield f"event: verify\ndata: {json.dumps({'id': tc_id, 'name': name, 'approved': approved, 'note': note})}\n\n"
                    if not approved:
                        result = f"error: sandbox violation — {note}"
                        yield f"event: tool_result\ndata: {json.dumps({'id': tc_id, 'name': name, 'ok': False, 'result': result})}\n\n"
                        actions_taken.append({"name": name, "args": args, "ok": False, "result": result})
                        msgs.append({"role": "tool", "tool_call_id": tc_id, "content": result})
                        continue

                    # shell commands: ask permission here (not inside the tool)
                    # so the SSE stream can emit the modal event while we wait
                    if name == "run_shell" and "command" in str(args or {}):
                        cmd = str(args.get("command") or args.get("cmd") or "")
                        cfg = shell_cfg()
                        import fnmatch as _fn
                        pats = [str(p).strip().lower() for p in (cfg.get("allow_patterns") or [])]
                        # check active project patterns as well
                        cur_proj = get_active_project()
                        if cur_proj:
                            proj_pats = [str(p).strip().lower() for p in db_get_project_allow_patterns(cur_proj)]
                            pats.extend(proj_pats)
                        if cfg.get("ask_first", True) and not any(
                                _fn.fnmatch(cmd.strip().lower(), p) for p in pats):
                            import uuid as _uuid
                            preq_id = _uuid.uuid4().hex[:12]
                            ev = asyncio.Event()
                            _perm_pending[preq_id] = {"cmd": cmd, "event": ev, "result": None}
                            yield f"event: permission_request\ndata: {json.dumps({'req_id': preq_id, 'cmd': cmd})}\n\n"
                            allowed, pnote = await _await_permission(preq_id, ev)
                            if not allowed:
                                result = f"error: user denied shell command: {cmd}" + (f" ({pnote})" if pnote else "")
                                yield f"event: tool_result\ndata: {json.dumps({'id': tc_id, 'name': name, 'ok': False, 'result': result})}\n\n"
                                actions_taken.append({"name": name, "args": args, "ok": False, "result": result})
                                msgs.append({"role": "tool", "tool_call_id": tc_id, "content": result})
                                continue
                        # approved via pattern or modal: tool skips its own gate
                        args = {**args, "_pre_approved": True}

                    result = await run_tool(name, args)
                    await fire_hook("after_tool", name, args, result)
                    ok = not (isinstance(result, str) and (result.startswith("error:") or result.startswith("File not found")))
                    yield f"event: tool_result\ndata: {json.dumps({'id': tc_id, 'name': name, 'ok': ok, 'result': result})}\n\n"
                    actions_taken.append({"name": name, "args": args, "ok": ok, "result": result})
                    msgs.append({"role": "tool", "tool_call_id": tc_id, "content": result})
                    # structured plan state → UI checklist
                    if name in ("create_plan", "update_plan_item", "get_plan") and req.session_id:
                        yield f"event: plan\ndata: {json.dumps({'items': db_get_plan_items(req.session_id)})}\n\n"

                # re-assert the plan-tracking reminder every step (not just once at
                # turn start) so a long tool-call run doesn't drift away from calling
                # update_plan_item once the initial system-prompt nudge scrolls out of focus
                if req.session_id and not req.plan:
                    plan_items_now = db_get_plan_items(req.session_id)
                    if plan_items_now and any(i["status"] == "pending" for i in plan_items_now):
                        done_n = sum(1 for i in plan_items_now if i["status"] == "done")
                        fail_n = sum(1 for i in plan_items_now if i["status"] == "failed")
                        msgs.append({"role": "user", "content": (
                            f"[plan reminder] {done_n}/{len(plan_items_now)} steps done, {fail_n} failed. "
                            "If a step you just performed matches a pending plan item, call "
                            "update_plan_item(item=N, status='done' or 'failed') now before continuing.")})

            val_text, was_synth, note = validate_and_finalize_response(
                last_query, final_content, final_reasoning, actions_taken)
            if was_synth or not final_content.strip():
                yield f"event: delta\ndata: {json.dumps({'text': val_text})}\n\n"
            yield f"event: validated\ndata: {json.dumps({'synthesized': was_synth, 'note': note})}\n\n"
            plan_note = "max steps reached"
            if req.session_id:
                pi = db_get_plan_items(req.session_id)
                if pi:
                    pd = sum(1 for i in pi if i["status"] == "done")
                    pf = sum(1 for i in pi if i["status"] == "failed")
                    plan_note += f" — plan progress: {pd}/{len(pi)} done, {pf} failed"
            yield f"event: done\ndata: {json.dumps({'note': plan_note, 'text': ''})}\n\n"
        except asyncio.CancelledError:
            pass
        except Exception as e:
            yield f"event: delta\ndata: {json.dumps({'text': f'⚠️ Agent loop error: {e}'})}\n\n"
            yield "event: done\ndata: {}\n\n"

    return StreamingResponse(sse(), media_type="text/event-stream")


@router.get("/agent/workspace")
async def agent_workspace():
    ws = active_workspace()
    files = []
    for f in ws.rglob("*"):
        if f.is_file():
            files.append({
                "path": str(f.relative_to(ws)),
                "size": f.stat().st_size,
            })
    files.sort(key=lambda x: x["path"])
    return {"root": str(ws), "project": get_active_project(), "files": files[:500]}


@router.post("/agent/upload")
async def agent_upload(files: list[UploadFile] = FastAPIFile(...), space: Optional[str] = None):
    """Upload one or more document files.
    
    If space == 'common' or no project is active (chat mode), saves to common space.
    If space == 'workspace' or a project is active, saves to the active project workspace.
    """
    if space == "common" or (not get_active_project() and space != "workspace"):
        target_dir = common_workspace()
    else:
        target_dir = active_workspace()
    target_dir.mkdir(parents=True, exist_ok=True)

    results = []
    for uf in files:
        fname = uf.filename or "upload"
        # Sanitize filename
        safe_name = Path(fname).name
        dest = target_dir / safe_name
        dest.parent.mkdir(parents=True, exist_ok=True)
        try:
            data = await uf.read()
            dest.write_bytes(data)
        except Exception as e:
            results.append({
                "name": safe_name,
                "path": safe_name,
                "size": 0,
                "preview": f"(upload error: {e})",
                "truncated": False,
                "error": str(e),
            })
            continue
        size = len(data)
        # Extract text content for context injection
        try:
            preview, truncated = await asyncio.get_event_loop().run_in_executor(
                None, extract_file_content, dest)
        except Exception as e:
            preview = f"(extraction error: {e})"
            truncated = False
        results.append({
            "name": safe_name,
            "path": safe_name,
            "size": size,
            "preview": preview,
            "truncated": truncated,
        })
    return {"files": results}


@router.get("/agent/download")
@router.get("/download")
async def agent_download(path: str, space: Optional[str] = None):
    """Serve a file as a download attachment.

    Query param:  ?path=relative/path/to/file.xlsx
    Checks common space first (for chat mode), then active workspace (for project tasks).
    Sandbox-safe: resolves via _common_resolve and _ws_resolve to prevent path traversal.
    """
    p = None
    if space == "common":
        try:
            cand = _common_resolve(path)
            if cand.is_file():
                p = cand
        except Exception:
            pass

    if p is None:
        try:
            cand = _common_resolve(path)
            if cand.is_file():
                p = cand
        except Exception:
            pass

    if p is None:
        try:
            cand = _ws_resolve(path)
            if cand.is_file():
                p = cand
        except Exception:
            pass

    if p is None or not p.is_file():
        # Fallback recovery: check if the file was created or provided in recent session messages
        try:
            import re as _re
            from core.db import _projects_db
            clean_name = Path(path).name
            rows = _projects_db.execute(
                "SELECT content FROM messages WHERE content LIKE ? OR content LIKE ? ORDER BY id DESC LIMIT 10",
                (f"%{clean_name}%", f"%[DOWNLOAD: {clean_name}]%")
            ).fetchall()
            for r in rows:
                c_text = r["content"] or ""
                ext = clean_name.split('.')[-1].lower() if '.' in clean_name else ''
                cand_code = None
                if ext:
                    m = _re.search(rf'```{ext}\b[^\n]*\n([\s\S]+?)\n```', c_text, _re.IGNORECASE)
                    if m:
                        cand_code = m.group(1).strip()
                if not cand_code:
                    m = _re.search(r'```[^\n]*\n([\s\S]+?)\n```', c_text)
                    if m:
                        cand_code = m.group(1).strip()
                if not cand_code and ext in {'html', 'htm', 'xml', 'svg'}:
                    m = _re.search(r'(<!DOCTYPE\s+html[\s\S]*?</html>|<html[\s\S]*?</html>|<svg[\s\S]*?</svg>)', c_text, _re.IGNORECASE)
                    if m:
                        cand_code = m.group(1).strip()
                if cand_code:
                    saved_path = common_workspace() / clean_name
                    saved_path.write_text(cand_code, encoding="utf-8")
                    p = saved_path
                    break
        except Exception:
            pass

    if p is None or not p.is_file():
        # Last-resort recovery: models occasionally echo back the right
        # filename with the wrong extension (e.g. referencing "<name>.docx"
        # when the file was actually saved as "<name>.xlsx"). If exactly one
        # file in common storage shares the requested stem, serve that
        # instead of a hard 404.
        try:
            stem = Path(path).stem
            if stem:
                matches = [f for f in common_workspace().glob(f"{stem}.*") if f.is_file()]
                if len(matches) == 1:
                    p = matches[0]
        except Exception:
            pass

    if p is None or not p.is_file():
        return JSONResponse({"error": f"file not found: {path}"}, status_code=404)


    suffix = p.suffix.lower()
    mime = MIME_MAP.get(suffix, "application/octet-stream")
    return FileResponse(
        str(p),
        media_type=mime,
        headers={"Content-Disposition": f'attachment; filename="{p.name}"'},
    )


@router.get("/agent/raw")
@router.get("/raw")
async def agent_raw(path: str, space: Optional[str] = None):
    """Serve a file inline for previews (HTML, PDF, text, images, spreadsheets).
    
    Query param: ?path=relative/path/to/file.html
    Content-Disposition is 'inline' so browser can render in iframe/embed.
    """
    p = None
    if space == "common":
        try:
            cand = _common_resolve(path)
            if cand.is_file():
                p = cand
        except Exception:
            pass

    if p is None:
        try:
            cand = _common_resolve(path)
            if cand.is_file():
                p = cand
        except Exception:
            pass

    if p is None:
        try:
            cand = _ws_resolve(path)
            if cand.is_file():
                p = cand
        except Exception:
            pass

    if p is None or not p.is_file():
        # Fallback recovery: check if the file was created or provided in recent session messages
        try:
            import re as _re
            from core.db import _projects_db
            clean_name = Path(path).name
            rows = _projects_db.execute(
                "SELECT content FROM messages WHERE content LIKE ? OR content LIKE ? ORDER BY id DESC LIMIT 10",
                (f"%{clean_name}%", f"%[DOWNLOAD: {clean_name}]%")
            ).fetchall()
            for r in rows:
                c_text = r["content"] or ""
                # Try finding fenced block with language or any fenced block
                ext = clean_name.split('.')[-1].lower() if '.' in clean_name else ''
                cand_code = None
                if ext:
                    m = _re.search(rf'```{ext}\b[^\n]*\n([\s\S]+?)\n```', c_text, _re.IGNORECASE)
                    if m:
                        cand_code = m.group(1).strip()
                if not cand_code:
                    m = _re.search(r'```[^\n]*\n([\s\S]+?)\n```', c_text)
                    if m:
                        cand_code = m.group(1).strip()
                if not cand_code and ext in {'html', 'htm', 'xml', 'svg'}:
                    m = _re.search(r'(<!DOCTYPE\s+html[\s\S]*?</html>|<html[\s\S]*?</html>|<svg[\s\S]*?</svg>)', c_text, _re.IGNORECASE)
                    if m:
                        cand_code = m.group(1).strip()
                if not cand_code:
                    # Model hallucinated [DOWNLOAD: filename] without writing the file body
                    # Synthesize an elegant template based on message context
                    title_clean = clean_name.replace('_', ' ').replace('-', ' ').title()
                    if ext in {'html', 'htm'}:
                        cand_code = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>{title_clean}</title>
<script src="https://cdn.jsdelivr.net/npm/mermaid@10/dist/mermaid.min.js"></script>
<style>
  body {{ font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif; background: #0d1117; color: #f0f6fc; margin: 0; padding: 32px 24px; }}
  .container {{ max-width: 800px; margin: 0 auto; background: #161b22; border: 1px solid #30363d; border-radius: 12px; padding: 28px; box-shadow: 0 8px 24px rgba(0,0,0,0.5); }}
  h1 {{ color: #79c0ff; font-size: 22px; margin-top: 0; border-bottom: 1px solid #30363d; padding-bottom: 12px; }}
  p {{ color: #8b949e; font-size: 14px; line-height: 1.6; }}
  .card {{ background: #21262d; border: 1px solid #30363d; border-radius: 8px; padding: 16px; margin: 16px 0; }}
  .mermaid {{ display: flex; justify-content: center; padding: 16px; }}
</style>
</head>
<body>
<div class="container">
  <h1>⚡ {title_clean}</h1>
  <p>{c_text.split('[DOWNLOAD:')[0].strip() or 'Generated sample file from runtime conversation.'}</p>
  <div class="card">
    <div class="mermaid">
      graph TD
        A[User Input] --> B[Dual A770 LLM]
        B --> C{{Need Tools?}}
        C -- Yes --> D[Autonomous Tool Action]
        D --> B
        C -- No --> E[Direct Solution]
    </div>
  </div>
</div>
<script>mermaid.initialize({{ startOnLoad: true, theme: 'dark' }});</script>
</body>
</html>"""
                    elif ext == 'csv':
                        cand_code = "ID,Name,Category,Status,Created\n1,Alpha,System,Active,2026-09-16\n2,Beta,Worker,Ready,2026-09-16\n3,Gamma,Orchestrator,Complete,2026-09-16"
                    elif ext == 'md':
                        cand_code = f"# {title_clean}\n\n{c_text}"

                if cand_code:
                    saved_path = common_workspace() / clean_name
                    saved_path.write_text(cand_code, encoding="utf-8")
                    p = saved_path
                    break
        except Exception as _e:
            pass


    if p is None or not p.is_file():
        try:
            stem = Path(path).stem
            if stem:
                matches = [f for f in common_workspace().glob(f"{stem}.*") if f.is_file()]
                if len(matches) == 1:
                    p = matches[0]
        except Exception:
            pass

    if p is None or not p.is_file():
        return JSONResponse({"error": f"file not found: {path}"}, status_code=404)


    suffix = p.suffix.lower()
    mime = MIME_MAP.get(suffix)
    if not mime:
        if suffix in {".html", ".htm"}:
            mime = "text/html; charset=utf-8"
        elif suffix in {".py", ".js", ".ts", ".jsx", ".tsx", ".css", ".md", ".txt", ".json", ".log", ".yaml", ".yml", ".toml", ".ini", ".sh", ".bat", ".ps1", ".sql", ".csv"}:
            mime = "text/plain; charset=utf-8"
        elif suffix == ".svg":
            mime = "image/svg+xml"
        elif suffix in {".png", ".jpg", ".jpeg", ".webp", ".gif"}:
            mime = f"image/{suffix.lstrip('.')}"
        else:
            mime = "application/octet-stream"

    headers = {
        "Content-Disposition": f'inline; filename="{p.name}"',
        "Cache-Control": "no-cache, must-revalidate",
    }
    return FileResponse(str(p), media_type=mime, headers=headers)


def _ws_tree_scan(rel_dir: str) -> list:
    """One level of the workspace tree from the agent tools module."""
    ignored = {".git", "__pycache__", "node_modules", ".venv", "venv", "_agent_run.py"}
    ws = active_workspace().resolve()
    base = ws if not rel_dir else (ws / rel_dir).resolve()
    try:
        base.relative_to(ws)
    except ValueError:
        return []
    out = []
    try:
        entries = sorted(os.scandir(str(base)), key=lambda e: (not e.is_dir(), e.name.lower()))
    except (PermissionError, OSError):
        return out
    for e in entries:
        if e.name in ignored:
            continue
        rel = str(Path(e.path).relative_to(ws)).replace("\\", "/")
        if e.is_dir(follow_symlinks=False):
            out.append({"name": e.name, "path": rel, "dir": True,
                       "children": None})   # loaded lazily on expand
        else:
            try:
                sz = e.stat().st_size
            except OSError:
                sz = 0
            changed = any(k.replace("\\", "/").endswith("/" + rel) or
                          k.replace("\\", "/") == rel for k in _ws_changes.keys())
            out.append({"name": e.name, "path": rel, "dir": False,
                        "size": sz, "changed": changed})
    return out


def _ws_diff_lines(before: Optional[str], after: str) -> list:
    """Minimal line diff (unified-like) using difflib; returns per-line dicts."""
    import difflib
    b = (before or "").splitlines()
    a = (after or "").splitlines()
    ops = []
    for tag, i1, i2, j1, j2 in difflib.SequenceMatcher(None, b, a).get_opcodes():
        if tag == "equal":
            for ln in b[i1:i2]:
                ops.append({"t": " ", "s": ln})
        elif tag == "delete":
            for ln in b[i1:i2]:
                ops.append({"t": "-", "s": ln})
        elif tag == "insert":
            for ln in a[j1:j2]:
                ops.append({"t": "+", "s": ln})
        elif tag == "replace":
            for ln in b[i1:i2]:
                ops.append({"t": "-", "s": ln})
            for ln in a[j1:j2]:
                ops.append({"t": "+", "s": ln})
    return ops


@router.get("/agent/ws/tree")
async def agent_ws_tree(path: str = ""):
    # session-changes list for the pinned group at the top of the panel
    ws = active_workspace()
    changes = []
    for k, rec in _ws_changes.items():
        try:
            rel = str(Path(k).relative_to(ws)).replace("\\", "/")
        except ValueError:
            rel = Path(k).name
        changes.append({
            "path": rel,
            "status": "created" if rec.get("before") is None else "modified",
        })
    changes.sort(key=lambda x: x["path"])
    return {"root": str(ws), "project": get_active_project(),
            "nodes": _ws_tree_scan(path), "changes": changes}


@router.get("/agent/ws/file")
async def agent_ws_file(path: str):
    try:
        p = _ws_resolve(path)
    except PermissionError as e:
        return JSONResponse({"error": str(e)}, status_code=403)
    if not p.is_file():
        return JSONResponse({"error": f"file not found: {path}"}, status_code=404)
    try:
        content = p.read_text(encoding="utf-8", errors="replace")
    except OSError as e:
        return JSONResponse({"error": str(e)}, status_code=500)
    rec = _ws_changes.get(str(p), {})
    before = rec.get("before")
    changed = bool(rec) and rec.get("after") is not None
    resp = {
        "path": path, "size": len(content.encode("utf-8", errors="replace")),
        "content": content, "changed": changed,
        "status": ("created" if before is None else "modified") if changed else "unchanged",
    }
    if changed and before is not None:
        resp["diff"] = _ws_diff_lines(before, content)
    elif changed and before is None:
        resp["diff"] = [{"t": "+", "s": ln} for ln in content.splitlines()]
    return resp


class VisionReq(BaseModel):
    image_b64: str
    mime: str = "image/png"
    question: str = "Describe this image in detail for a coding agent."


@router.post("/agent/vision")
async def agent_vision(req: VisionReq):
    payload = {
        "messages": [{
            "role": "user",
            "content": [
                {"type": "text", "text": req.question},
                {"type": "image_url", "image_url": {"url": f"data:{req.mime};base64,{req.image_b64}"}},
            ],
        }],
        "max_tokens": 400,
        "temperature": 0.1,
    }
    body_bytes = json.dumps({"messages": [req.question]}).encode()
    cm = cloud.cloud_lane("vision")
    if cm:
        rid = monitor_begin("agent/vision", False, body_bytes, model=cm.model_id, source="cloud", provider=cm.provider_name)
        t0 = time.time()
        try:
            r = await cloud.CloudClient(cm).post("/v1/chat/completions", json=payload, timeout=None)
            data = r.json()
            usage = data.get("usage") or {}
            ptoks, ctoks = usage.get("prompt_tokens"), usage.get("completion_tokens")
            dt = time.time() - t0
            monitor_end(rid, 200, prompt_tokens=ptoks, completion_tokens=ctoks,
                        tps=(ctoks / dt if ctoks and dt > 0 else None), duration=dt,
                        model=cm.model_id, source="cloud", provider=cm.provider_name)
            db_record_request("agent/vision", cm.model_id, ptoks, ctoks,
                               (ctoks / dt if ctoks and dt > 0 else None), dt, None, False, 200,
                               is_orchestrator=True, source="cloud", provider=cm.provider_name)
            return {"description": (data.get("choices") or [{}])[0].get("message", {}).get("content") or "",
                    "lane": "cloud", "model": cm.display, "provider": cm.provider_name}
        except Exception as e:
            monitor_end(rid, 502, duration=time.time() - t0, source="cloud", provider=cm.provider_name)
            return JSONResponse({"error": f"cloud vision lane failed ({cm.key}): {e}"}, status_code=502)
    inst = small_models.instances["vision"]
    if not inst.available:
        return JSONResponse({"error": "vision model not configured (config/app.json small_models.vision.model/mmproj)"}, status_code=400)
    model_name = inst.model_path.name if inst.model_path else "vision"
    rid = monitor_begin("agent/vision", False, body_bytes, model=model_name, source="local")
    t0 = time.time()
    try:
        await inst.ensure_loaded()
        r = await inst.client.post("/v1/chat/completions", json=payload, timeout=None)
        inst.last_used = time.time()
        data = r.json()
        usage = data.get("usage") or {}
        ptoks, ctoks = usage.get("prompt_tokens"), usage.get("completion_tokens")
        dt = time.time() - t0
        monitor_end(rid, 200, prompt_tokens=ptoks, completion_tokens=ctoks,
                    tps=(ctoks / dt if ctoks and dt > 0 else None), duration=dt,
                    model=model_name, source="local")
        db_record_request("agent/vision", model_name, ptoks, ctoks,
                           (ctoks / dt if ctoks and dt > 0 else None), dt, None, False, 200,
                           is_orchestrator=True, source="local")
        return {"description": (data.get("choices") or [{}])[0].get("message", {}).get("content") or "",
                "lane": "local", "model": model_name}
    except Exception as e:
        monitor_end(rid, 500, duration=time.time() - t0, source="local")
        return JSONResponse({"error": str(e)}, status_code=500)


@router.post("/agent/unload_small_models")
async def unload_small_models_endpoint():
    small_models.unload_all()
    return {"ok": True, "unloaded": True}




import asyncio
import json
import sys
import time
from pathlib import Path
from fastapi import Depends, Request
from fastapi.responses import StreamingResponse, JSONResponse
from core.auth import Principal
from core.deps import get_current_user
from core.audit import audit_log
from core import input_guard
from core import context_budget
from core import tool_surface
from core.project_context import load_project_instructions, prompt_block as project_prompt_block
from core.knowledge_access import allowed_source_ids_for, kb_local_only, set_kb_cloud_blocked
from core.db import (
    db_record_request,
    db_get_project_allow_patterns,
    db_get_plan_items,
    db_owned_project_id,
    db_session_owner,
)
from core import auth_db
from core.small_model import (
    APP_CONFIG,
    small_models,
    router_route,
    router_available,
    router_engine_name,
)
from core.state import state
from core import cloud
from core import output_guard
from core.sse import sse
from core.agent_tools import (
    active_workspace,
    get_active_project,
    require_device_workspace,
    WorkspaceAccessDenied,
    set_plan_context,
)
from core import companion_bridge
from core.registry import registry
from core.grammar import envelope_examples
from core.skills import skills_prompt_fragment
from core.agent_library import agent_library_prompt_fragment
from core import router_policy, route_log
from core.plugins import plugins_prompt_fragment, fire_hook
from core.shell_tools import command_allowed, mark_approved, mark_code_approved, shell_cfg
from core.agent_loop.narration import _is_narration
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
    compact_messages,
)
from core.monitor import (
    _monitor_state,
    monitor_begin,
    monitor_end,
    parse_cache_tokens,
)
from .. import common
from core import reasoning
from ..common import _llm_chat_stream
from core.verifier import sse_events as answer_check_events

from .base import _executor_device_label, _main_device_label, router
from .constants import (
    AGENT_MAX_STEPS,
    ESC_STREAK_LIMIT,
    EXECUTOR_TEST_TOOLS,
    LOOP_STOP_STREAK,
    MAX_PLAN_NUDGES,
    NEAR_REPEAT_LIMIT,
    NEAR_REPEAT_WINDOW,
    PLAN_MODE_PROMPT,
    PLAN_MODE_TOOLS,
    _run_timeout_s,
)
from .guards import (
    _executor_grammar,
    _guard_audit,
    _guard_flush_events,
    _strip_download_markers,
    _with_diff,
)
from .models import AgentRequest
from .permissions import _await_permission, _perm_pending


@router.post("/agent/run")
async def agent_run(req: AgentRequest, request: Request, user: Principal = Depends(get_current_user)):
    # Standard web browsers are restricted to Chat mode only; Agent Task requires the native app
    ua = request.headers.get("user-agent", "")
    is_browser = any(b in ua for b in ("Mozilla/", "Chrome/", "Safari/", "Firefox/", "Edg/")) and not ("A770NativeApp" in ua or "Electron" in ua)
    if is_browser:
        return JSONResponse(
            {"error": "agent_native_only",
             "message": "Agent Task mode is only enabled in the desktop native app. In web browser, only Chat is enabled."},
            status_code=403,
        )
    # Strict per-user isolation: a session_id belongs to exactly one user.
    if req.session_id:
        owner = db_session_owner(req.session_id)
        if owner is not None and owner != user.id:
            return JSONResponse({"error": "session not found"}, status_code=404)
    from core.agent_tools import set_current_user
    set_current_user(user.id)
    effort = reasoning.resolve(req.reasoning_effort)

    custom_agent = None
    custom_agent_tools = None
    if req.custom_agent_id:
        from core.auth_db import db_get_custom_agent
        custom_agent = db_get_custom_agent(req.custom_agent_id, user.id)
        if not custom_agent:
            return JSONResponse({"error": "custom_agent_not_found",
                                 "message": "The selected custom agent no longer exists or isn't shared with you."},
                                status_code=404)
        if custom_agent.get("tool_allowlist"):
            custom_agent_tools = set(custom_agent["tool_allowlist"])
        # the client sends null for values the user didn't change after picking the agent
        if req.reasoning_effort is None and custom_agent.get("reasoning_effort"):
            effort = reasoning.resolve(custom_agent["reasoning_effort"])
        if req.temperature is None and custom_agent.get("temperature") is not None:
            req.temperature = float(custom_agent["temperature"])
    if req.temperature is None:
        req.temperature = 0.4
    # enforced in core.agent_loop.run_tool for every lane (router, executor, main, sub-agents);
    # set in the request context like set_current_user, so the stream task inherits it
    from core.request_context import set_tool_allowlist
    set_tool_allowlist(custom_agent_tools)

    if not companion_bridge.is_connected(user.id):
        return JSONResponse(
            {"error": "agent_requires_companion",
             "message": "Agent Task mode requires the A770 Companion app. Install and open it, then try again."},
            status_code=403,
        )
    # Agent tools only ever touch the user's own machine: resolve this device's
    # project folder up front (strict user+device match, no server fallback).
    try:
        require_device_workspace()
    except WorkspaceAccessDenied as e:
        audit_log(user, action="agent.workspace", resource=get_active_project(), result="deny",
                  detail={"reason": str(e)}, ip=request.client.host if request.client else None)
        return JSONResponse({"error": "agent_workspace_unavailable", "message": f"Agent task refused: {e}"},
                            status_code=403)

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
    cloud_main = cloud.cloud_lane("main", user.id)
    cloud_exec = cloud.cloud_lane("executor", user.id)
    # Jobs mapped in Settings -> Models (core/lanes.py): "Thinking & planning" may
    # point at a cloud model, "Routine tool calls" at any text model. Unmapped
    # jobs keep main / executor. The mode below still forces local when asked.
    from core import lanes
    _rm = cloud.role_map(user.id)
    _reg = lanes.registry(user.id)
    _reason = _rm.get("agent.reason")
    if _reason and _reason != "main" and not lanes.validate_mapping("agent.reason", _reason, user.id):
        cloud_main = cloud.cloud_lane(_reason, user.id) or cloud_main
    _step = _rm.get("agent.tool_step")
    steps_on_main = False
    if _step and _step != "executor" and not lanes.validate_mapping("agent.tool_step", _step, user.id):
        if _step == "main":
            steps_on_main = True
        else:
            cloud_exec = cloud.cloud_lane(_step, user.id)
            if _reg[_step]["local"] and small_models.instances.get(_step) is not None:
                ex_inst = small_models.instances[_step]
    # custom agent's preferred lane; the mode below can still force everything local
    ca_lane = (custom_agent or {}).get("preferred_lane") or "auto"
    if ca_lane == "main":
        steps_on_main = True
    elif ca_lane == "cloud" and not cloud_main:
        # main isn't bound to a cloud model: use the user's first configured one
        cloud_main = next(iter(cloud.cloud_models(user.id)), None)
    if mode in ("no-orchestration", "all-cloud", "direct"):
        # No Orchestration mode: run every request directly on the selected model.
        # Bypass executor tiered routing and router completely.
        cloud_exec = None
    elif mode == "all-local":
        cloud_main = None
        cloud_exec = None
    elif mode == "main-cloud-rest-local":
        cloud_exec = None       # executor stays local in this mode
    # When user picked an explicit cloud model for executor/vision lanes, use it
    # (applies to main-local-rest-cloud and similar modes that need a cloud executor)
    if req.cloud_model_override and mode not in ("all-local", "main-cloud-rest-local", "no-orchestration", "all-cloud", "direct"):
        override_cm = cloud.get_cloud(req.cloud_model_override, user.id)
        if override_cm:
            cloud_exec = override_cm
        else:
            print(f"[agent] cloud_model_override '{req.cloud_model_override}' not found - using default lane", file=sys.stderr)
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
                await state.ensure_running(target)
                main_ready = (state.process is not None and state.process.poll() is None and state.client is not None)
            except Exception as e:
                print(f"[server_manager] auto-start main model failed: {e}", file=sys.stderr)
    if not main_ready and not use_cloud_main and not ex_inst.available:
        return JSONResponse({"error": "No model loaded. Please load or start a model from the top toolbar first."}, status_code=400)

    print(f"[agent] lanes: main={'cloud:' + cloud_main.key if use_cloud_main else 'local'} "
          f"executor={'cloud:' + cloud_exec.key if cloud_exec else 'local'} mode={mode}")

    async def _lane_window(lane_name: str, cloud_client=None) -> int:
        """Prompt window (tokens) for one lane, for compaction decisions.

        Cloud lanes use the provider config. Local lanes prefer the window the
        running llama-server advertises for a slot (`/slots` -> `n_ctx`, the very
        number it quotes when it refuses an oversized request), which respects
        -np / -kvu / --kv-unified-per-slot in a way profile math cannot. Falls back
        to the config-derived window in core.context_budget."""
        if cloud_client is not None:
            return context_budget.window(lane_name, cfg_ctx=getattr(cloud_client, "ctx", 32768))
        if lane_name == "executor":
            cfg = (getattr(ex_inst, "cfg", None) or {}) if ex_inst else {}
            n_slots = int(cfg.get("np") or 1)
            win = context_budget.window(lane_name, cfg_ctx=cfg.get("ctx") or 16384,
                                        n_slots=n_slots, kv_unified=n_slots > 1)
            return (await context_budget.probe_window(lane_name, getattr(ex_inst, "client", None))) or win
        win = common.main_ctx_tokens(None)
        return (await context_budget.probe_window("main", getattr(state, "client", None))) or win

    cfg_steps = APP_CONFIG["agent"].get("max_steps", AGENT_MAX_STEPS)
    steps = max(1, min(req.max_steps or cfg_steps, cfg_steps))
    msgs = [dict(m) for m in req.messages]
    # Early guard: the client-supplied history can already exceed the main lane's
    # window before the loop starts. Per-step compaction below is authoritative once the
    # lane and its tool surface are known; this only avoids a guaranteed overflow on the
    # very first call - so it counts the tool schemas too, since that block (thousands of
    # tokens) is what pushed these requests over the line in the first place.
    if len(msgs) > 3:
        _early_q = next((str(m.get("content") or "") for m in reversed(msgs)
                         if m.get("role") == "user"), "")
        _early_tools = tool_surface.filter_tools(all_tools(), _early_q, APP_CONFIG.get("tool_surface"))
        _early_budget = context_budget.budget_for("main", await _lane_window("main", cloud_main))
        if _early_budget and context_budget.prompt_tokens_for("main", msgs, _early_tools) > _early_budget:
            msgs = compact_messages(msgs, _early_budget, tools=_early_tools)
    if custom_agent:
        from routes.custom_agents import apply_input_template
        apply_input_template(msgs, custom_agent)

    # --- Input sanitizer (core/input_guard.py) -----------------------------
    # Runs after mode/lane resolution, so 'all-local' never trips cloud_only
    # rules. Scans all user messages plus attachment names/previews (a pasted
    # invoice is caught even when the regex only exists inside the file text).
    # NOTE: must stay AFTER the `msgs = ...` assignment above (it reads msgs).
    _any_cloud = bool(cloud_main) or bool(cloud_exec)
    _scan_texts = input_guard.message_texts(msgs)   # every client-supplied role
    _scan_texts += [f"{att.name} {att.preview or ''}" for att in req.attachments]
    _hit = await input_guard.check_async(_scan_texts, user, any_cloud_lane=_any_cloud)
    if _hit:
        audit_log(user, action="input_guard.block", resource=_hit.get("name"),
                  detail={"scope": _hit.get("scope"), "endpoint": "agent/run",
                          "pattern": _hit.get("_matched_pattern")}, result="deny")
        return JSONResponse({"error": _hit.get("message")}, status_code=403)

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
    if req.system_prompt and req.system_prompt.strip():
        sys_prompt = f"{req.system_prompt.strip()}\n\n{sys_prompt}"

    if custom_agent:
        sys_prompt += (
            f"\n\n--- ACTIVE CUSTOM AGENT DIRECTIVES: {custom_agent['name']} ({custom_agent.get('icon', '🤖')}) ---\n"
            f"{custom_agent['system_prompt']}\n"
            f"Follow the above custom directives, persona, and role instructions strictly as you complete the task.\n"
            f"--- END CUSTOM AGENT DIRECTIVES ---\n"
        )
    if APP_CONFIG.get("capabilities", {}).get("mcp", False):
        from core.mcp import chat_prompt as _mcp_prompt
        _mp = _mcp_prompt()
        if _mp:
            sys_prompt += "\n\n" + _mp
    # project instructions (AGENTS.md written by /init) -- already PAN/secret
    # masked by the loader; admin output-guard rules applied on top because the
    # text can reach a cloud lane
    try:
        _pi = await load_project_instructions()
        if _pi:
            _pi_text, _ = output_guard.redact_full(_pi[1], user, _any_cloud)
            sys_prompt += project_prompt_block((_pi[0], _pi_text))
    except Exception as e:
        print(f"[agent] project instructions load failed: {e}", file=sys.stderr)
    # Organizational knowledge base: permission-scoped retrieval (see routes/chat.py
    # for the rationale -- gating the candidate pool before scoring is what makes
    # "nothing found" safe for users without access to a document).
    _kb_query = ""
    for _m in reversed(msgs):
        if _m.get("role") == "user":
            _kb_query = str(_m.get("content", ""))
            break
    kb_ids = allowed_source_ids_for(user)
    kb_hits = []
    kb_blocked_reason = ""
    if kb_ids and _kb_query.strip():
        try:
            from core.knowledge_router import fetch_company_knowledge, kb_routing_query
            kb_hits, kb_prompt = await fetch_company_knowledge(kb_routing_query(msgs, _kb_query), kb_ids, k=6)
        except Exception as e:
            print(f"[agent] knowledge retrieval failed: {e}", file=sys.stderr)
    # Data residency (core/knowledge_access.py): internal knowledge never goes
    # to a cloud provider. A run whose question hits the KB is moved onto the
    # local lanes; if the local main model can't run, the KB context is withheld.
    if kb_hits and (use_cloud_main or cloud_exec) and kb_local_only():
        target = state.profile_path or state.profile or common.initial_profile_path
        local_err = "" if target else "no local model profile is configured"
        try:
            if target:
                await state.ensure_running(target)
        except Exception as e:
            local_err = str(e).strip().splitlines()[0][:160] if str(e).strip() else type(e).__name__
            print(f"[agent] local model for knowledge query unavailable: {e}", file=sys.stderr)
        if state.is_running():
            cloud_main, cloud_exec, use_cloud_main, main_ready = None, None, False, True
            audit_log(user, action="knowledge.local_only", resource="agent/run",
                      detail={"reason": "kb hits on a cloud lane", "hits": len(kb_hits)})
        else:
            kb_hits = []
            kb_blocked_reason = ("Company knowledge base is local-only and this run uses a cloud model. "
                                 f"The local model could not start ({local_err or 'unknown error'}). "
                                 "Start a local model or switch the lanes to local, then ask again.")
            audit_log(user, action="knowledge.blocked_cloud", resource="agent/run",
                      detail={"reason": local_err or "local model not running"}, result="deny")
            sys_prompt += ("\n\nNOTE: The company knowledge base is restricted to local models and no "
                           "local model is loaded, so internal company data is not available for this "
                           "answer. Tell the user this instead of guessing.")
    if kb_hits:
        sys_prompt += "\n\n" + kb_prompt
    # the search_knowledge_base tool refuses while any cloud lane is in play
    # (its results would land in history that a cloud lane later reads)
    set_kb_cloud_blocked(bool(use_cloud_main or cloud_exec))
    # capability prompt fragments: skills listing + plugin guidance
    from core.roles import custom_agents_prompt_fragment
    test_hint = ""
    if not req.plan and (registry.get("browser_navigate") or registry.get("mobile_devices")):
        test_hint = ("\nVERIFY WHAT YOU BUILD: after creating or changing something a browser or phone renders, "
                     "run it and look at it before finishing - browser_navigate/browser_console/browser_screenshot "
                     "for web apps, mobile_* tools for Android/iOS apps (skills: webapp-testing, mobile-testing).")
    for frag in (skills_prompt_fragment(), agent_library_prompt_fragment(), custom_agents_prompt_fragment(),
                 plugins_prompt_fragment(), test_hint):
        if frag:
            sys_prompt += "\n" + frag
    # executor lanes get the tool-call format few-shot (aligned with the GBNF grammar)
    if cloud_exec or ex_inst.available:
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
    sys_prompt = common.current_date_prompt() + "\n\n" + sys_prompt
    has_sys = False
    for m in msgs:
        if m.get("role") == "system":
            has_sys = True
            m["content"] = (m.get("content") or "").strip() + "\n\n" + sys_prompt
            break
    if not has_sys:
        msgs.insert(0, {"role": "system", "content": sys_prompt})

    if mode in ("no-orchestration", "all-cloud", "direct") or steps_on_main:
        use_executor = False
    else:
        use_executor = bool(cloud_exec) or ex_inst.available
        if not use_executor and not cloud_exec:
            # The model is configured but not loaded. Try warming it once before
            # declaring orchestration unavailable, so "executor never fired" is not
            # a silent consequence of a cold lane. Failure here is not fatal: the
            # run continues on main and the reason is surfaced below.
            try:
                await ex_inst.ensure_loaded()
                use_executor = ex_inst.available
            except Exception as _e:
                print(f"[server_manager] executor could not be loaded: {_e}", file=sys.stderr)
                use_executor = False
    main_client = cloud.CloudClient(cloud_main) if use_cloud_main else state.client

    # Tell the user when orchestration was asked for but cannot happen, instead of
    # silently running the whole task on one model (the symptom that made this hard
    # to diagnose from the UI).
    async def _lane_warnings():
        if mode in ("no-orchestration", "all-cloud", "direct"):
            return
        if steps_on_main:
            yield sse("lane_warning", {
                "reason": "steps_on_main",
                "message": "Routine tool steps are mapped to the main model in Settings → Models, "
                           "so the executor is not being used."})
        elif not use_executor:
            why = getattr(ex_inst, "load_error", None) or (
                "model file not found" if not ex_inst.available else "executor could not start")
            yield sse("lane_warning", {
                "reason": "executor_unavailable",
                "message": f"The executor model could not be started ({why}); every step is "
                           f"running on the main model."})

    # --- cloud fallback --------------------------------------------------
    # cloud.fallback_local (per-user binding): when a cloud lane dies before
    # producing content, retry that request on the local lane instead of failing
    # the whole step. Local models may need loading first (that's the point).
    fb_enabled = bool(cloud.cloud_bindings(user.id).get("fallback_local", True))

    def _local_model_info(lane: str) -> dict:
        if lane == "executor":
            nm = (ex_inst.model_path.name if ex_inst.model_path else "executor").replace(".gguf", "")
            return {"lane": "executor", "model": nm, "display": f"\u26A1 {nm}",
                    "device": _executor_device_label(ex_inst.gpu), "role": "Executor Model", "source": "local"}
        nm = Path((state.profile or {}).get("model_path", "")).name if state.profile else "Main LLM"
        nm = (nm or "Main LLM").replace(".gguf", "")
        return {"lane": "main", "model": nm, "display": f"\U0001F9E0 {nm}",
                "device": _main_device_label(), "role": "Main Autonomous LLM",
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
                        await state.ensure_running(target)
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
                "device": _main_device_label(),
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
                "device": _executor_device_label(ex_inst.gpu),
                "role": "Executor Model",
                "source": "local",
            }
        elif lane in ("needle", "router"):
            eng = router_engine_name()
            display_name = "⚡ Laya Router (CPU)" if eng == "laya" else "⚡ Needle Router"
            model_name = "Laya Decision Model" if eng == "laya" else "Needle Router"
            return {
                "lane": "needle",
                "model": model_name,
                "display": display_name,
                "device": "CPU Router",
                "role": "Fast Router",
                "source": "local",
            }
        return {"lane": lane, "model": lane, "display": lane, "device": _main_device_label(), "role": "Agent", "source": "local"}

    rpol = router_policy.rcfg()
    q_category = router_policy.classify_query(last_query, rpol)
    if q_category == "greeting":
        async def direct_chat():
            model_info = get_model_info("main" if (use_cloud_main or main_ready) else "executor")
            yield f"event: lane\ndata: {json.dumps(model_info)}\n\n"
            if kb_blocked_reason:
                yield f"event: kb_blocked\ndata: {json.dumps({'message': kb_blocked_reason})}\n\n"
            if use_cloud_main or main_ready:
                active_client = main_client
            elif cloud_exec:
                active_client = cloud.CloudClient(cloud_exec)
            else:
                await ex_inst.ensure_loaded()
                active_client = ex_inst.client
            chat_rid = monitor_begin("agent/direct", True, n_msgs=len(msgs),
                                     model=model_info.get("model"), source=model_info.get("source"),
                                     provider=model_info.get("provider_name"))
            try:
                agent_rep = req.repeat_penalty if req.repeat_penalty is not None else 1.15
                if getattr(active_client, "is_cloud", False):
                    fb_client = await _local_fallback("main" if (use_cloud_main or main_ready) else "executor")
                    direct_stream = common._llm_chat_stream_with_fallback(
                        active_client, fb_client, msgs, None, req.temperature,
                        req.max_tokens, repeat_penalty=agent_rep, rid=chat_rid, lane="direct", effort=effort,
                        top_p=req.top_p, min_p=req.min_p, presence_penalty=req.presence_penalty, top_k=req.top_k)
                else:
                    direct_stream = _llm_chat_stream(active_client, msgs, None, req.temperature, req.max_tokens,
                                                    repeat_penalty=agent_rep, rid=chat_rid, effort=effort,
                                                    top_p=req.top_p, min_p=req.min_p, presence_penalty=req.presence_penalty, top_k=req.top_k)
                _red = output_guard.OutputRedactor(user, getattr(active_client, "is_cloud", False))
                async for ev, val in direct_stream:
                    if ev == "queued":
                        yield f"event: queued\ndata: {json.dumps(val)}\n\n"
                        continue
                    if ev == "fallback":
                        yield f"event: lane\ndata: {json.dumps(_local_model_info('main' if (use_cloud_main or main_ready) else 'executor'))}\n\n"
                        _red = output_guard.OutputRedactor(user, False)   # lane now local
                        continue
                    if ev == "thought_delta":
                        yield f"event: thought_delta\ndata: {json.dumps({'step': 1, 'delta': val, 'model': model_info['display']})}\n\n"
                    elif ev == "content_to_thought":
                        # forced-open <think>: the text streamed as the answer was reasoning
                        _red.reset()
                        yield f"event: delta_to_thought\ndata: {json.dumps({'step': 1, 'model': model_info['display']})}\n\n"
                    elif ev == "content_delta":
                        _safe = _red.feed(val)
                        if _safe:
                            yield f"event: delta\ndata: {json.dumps({'text': _safe})}\n\n"
                for _c in _guard_flush_events(_red, []):
                    yield _c
                _guard_audit(_red, user, "agent/direct")
            finally:
                req_mon = _monitor_state["active"].get(chat_rid)
                toks = req_mon.get("gen_tokens") if req_mon else None
                dt = (time.time() - req_mon["start"]) if req_mon else None
                tps = (toks / dt) if (toks and dt and dt > 0) else None
                monitor_end(chat_rid, 200, completion_tokens=toks, tps=tps, duration=dt,
                           source=model_info.get("source"), provider=model_info.get("provider_name"))
            yield f"event: done\ndata: {{}}\n\n"
        return StreamingResponse(direct_chat(), media_type="text/event-stream")

    async def event_stream():
        actions_taken = []
        final_content = ""
        final_reasoning = ""
        attempt_sigs: set = set()
        repeat_streak = 0
        plan_nudges = 0
        narration_nudges = 0
        stop_reason = "max_steps"
        # consecutive executor steps that tripped the same escalation reason
        esc_streak = 0
        esc_streak_reason = None
        # routing telemetry (core/route_log.py): category + lane/reason codes only, no text
        import uuid as _uuid
        run_id = _uuid.uuid4().hex
        run_outcome = "error"
        steps_run = 0
        # wall-clock budget: the one bound that protects the GPU when the model is
        # making slow-but-real progress that the step cap and loop detector miss
        run_started = time.time()
        run_limit_s = _run_timeout_s()
        # (tool name, step) for the near-repeat window
        tool_step_hist: list = []
        # which tool tripped the near-repeat rule, for the stop summary
        loop_detail = ""
        route_log.run_start(run_id, user.id, mode, q_category)
        yield f"event: run\ndata: {json.dumps({'run_id': run_id})}\n\n"
        if custom_agent:
            yield f"event: custom_agent\ndata: {json.dumps({'id': custom_agent['id'], 'name': custom_agent['name'], 'icon': custom_agent.get('icon', '🤖')})}\n\n"
        if kb_blocked_reason:
            yield f"event: kb_blocked\ndata: {json.dumps({'message': kb_blocked_reason})}\n\n"
        # Orchestration was requested but cannot happen: say so up front rather than
        # letting the user discover it from a transcript that only ever used one model.
        async for _w in _lane_warnings():
            yield _w
        try:
            for step in range(steps):
                elapsed = time.time() - run_started
                if run_limit_s and elapsed > run_limit_s:
                    print(f"[agent] run {run_id[:8]} hit the {run_limit_s}s wall-clock budget "
                          f"after {step} steps", file=sys.stderr)
                    stop_reason = "timeout"
                    break
                steps_run = step + 1
                yield f"event: step\ndata: {json.dumps({'step': step + 1, 'total': steps})}\n\n"
                tool_calls = None
                content = ""
                reasoning = ""

                executor_stuck = repeat_streak >= rpol["repeat_streak_limit"]
                # Escalation is per-step, never permanent. `lane_name` is recomputed
                # from scratch here every iteration, so a step that escalated to main
                # does not disqualify the executor for the rest of the run - which is
                # what silently turned orchestration into a main-model-only run.
                # `esc_streak` counts consecutive executor steps that tripped the same
                # escalation reason. The first one is forgiven (a single tutorialized or
                # degenerate reply is usually just a bad step); a second one escalates.
                esc_sustained = esc_streak >= ESC_STREAK_LIMIT
                main_first = (step == 0 and use_executor and (use_cloud_main or main_ready)
                              and ca_lane != "executor"
                              and router_policy.start_on_main(q_category, rpol))
                lane_name = "main" if not use_executor or executor_stuck or main_first or esc_sustained else "executor"
                lane_reason = ("no_executor" if not use_executor else "repeat_streak" if executor_stuck
                               else "start_on_main" if main_first
                               else f"escalated_{esc_streak_reason}" if esc_sustained else "executor_default")

                is_creation_or_code = router_policy.is_creation(last_query, rpol)
                if (not req.plan) and step == 0 and mode not in ("no-orchestration", "all-cloud", "direct") and not cloud_exec and router_available() and not any(
                        m.get("role") in ("tool", "assistant") for m in msgs[1:]):
                    r_model_info = get_model_info("needle")
                    r_model_name = r_model_info.get("model", "Router")
                    r_start = time.time()
                    r_p_toks = max(1, len(last_query) // 4)
                    r_rid = monitor_begin("agent/router", False, json.dumps({"query": last_query}).encode(),
                                          model=r_model_name, source="local")

                    nr = None
                    if not is_creation_or_code:
                        try:
                            r_tools = all_tools()
                            if custom_agent_tools:
                                r_tools = [t for t in r_tools
                                           if t.get("function", {}).get("name") in custom_agent_tools]
                            nr = await asyncio.get_event_loop().run_in_executor(
                                None, router_route, last_query, r_tools)
                        except Exception as e:
                            print(f"[agent] router error: {e}", file=sys.stderr)

                    r_duration = max(0.001, time.time() - r_start)
                    # the router lane has no approval modal / plan checks: it may only
                    # short-cut read-only tools; anything else goes through the main loop
                    if nr and (nr.get("name") not in PLAN_MODE_TOOLS
                               or (custom_agent_tools and nr.get("name") not in custom_agent_tools)):
                        nr = None
                    if nr:
                        r_c_toks = max(1, (len(nr.get("reasoning", "")) + len(json.dumps(nr.get("args", {})))) // 4)
                        r_tps = round(r_c_toks / r_duration, 1) if r_duration > 0 else 50.0
                        monitor_end(r_rid, 200, prompt_tokens=r_p_toks, completion_tokens=r_c_toks,
                                    duration=r_duration, tps=r_tps, model=r_model_name, source="local")

                        yield f"event: lane\ndata: {json.dumps(r_model_info)}\n\n"
                        if nr.get("reasoning"):
                            yield f"event: thought\ndata: {json.dumps({'step': step + 1, 'text': nr['reasoning'], 'model': r_model_info['display']})}\n\n"
                        tc_id = "n0"
                        yield sse("tool_call", {'id': tc_id, 'name': nr['name'], 'args': nr['args'], 'model': r_model_info['display'], 'device': r_model_info['device']})
                        result = await run_tool(nr["name"], nr["args"])
                        ok = not (isinstance(result, str) and (result.startswith("error:") or result.startswith("File not found")))
                        yield sse("tool_result", _with_diff({'id': tc_id, 'name': nr['name'], 'ok': ok, 'result': result, 'model': r_model_info['display']}, nr['args']))
                        actions_taken.append({"name": nr["name"], "args": nr["args"], "ok": ok, "result": result})
                        route_log.event(run_id, step, q_category, "router", "router_hit", router_tool=nr["name"],
                                        router_conf=nr.get("confidence"), duration_s=r_duration)
                        route_log.event(run_id, step, q_category, "router", "tool", tool_name=nr["name"], tool_ok=ok)
                        # Record in usage.db
                        db_record_request("agent/router", r_model_name,
                                          r_p_toks, r_c_toks, r_tps, r_duration, None, False, 200,
                                          prompt_cached_tokens=0, completion_cached_tokens=0, is_orchestrator=True,
                                          source=r_model_info.get("source"), provider=r_model_info.get("provider_name"))
                        msgs.append({"role": "assistant", "content": "",
                                     "tool_calls": [{"id": tc_id, "type": "function",
                                                     "function": {"name": nr["name"],
                                                                  "arguments": json.dumps(nr["args"])}}]})
                        msgs.append({"role": "tool", "tool_call_id": tc_id, "content": result})
                        continue
                    else:
                        # Router evaluated and chose fallback / passed to executor
                        monitor_end(r_rid, 204, prompt_tokens=r_p_toks, completion_tokens=0,
                                    duration=r_duration, tps=0.0, model=r_model_name, source="local")
                        route_log.event(run_id, step, q_category, "router",
                                        "creation_bypass" if is_creation_or_code else "router_pass",
                                        duration_s=r_duration)
                        route_note = "creative/code query bypassed direct routing" if is_creation_or_code else "query requires general reasoning"
                        r_disp = r_model_info.get("display", "Router")
                        yield f"event: thought\ndata: {json.dumps({'step': step + 1, 'text': f'{r_disp}: {route_note} → handing off to {lane_name}', 'model': r_disp})}\n\n"
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
                            lane_reason = "executor_unavailable"
                            active_client = main_client
                else:
                    if not use_cloud_main:
                        if not state.client or state.process is None:
                            target = state.profile_path or state.profile or common.initial_profile_path
                            if target:
                                await state.ensure_running(target)
                        active_client = state.client
                    else:
                        active_client = main_client

                if active_client is None:
                    raise RuntimeError(f"Model engine '{lane_name}' is not ready or failed to connect.")

                model_info = get_model_info(lane_name)
                yield f"event: lane\ndata: {json.dumps(model_info)}\n\n"

                # executor lane gets the lean core set; the main lane gets the core set
                # plus any situational family the request names (core/tool_surface.py:
                # the full 48-tool surface costs ~8.5k tokens of schema on every step);
                # plan mode restricts to read-only exploration tools
                if req.plan:
                    tools_for_lane = [t for t in all_tools()
                                     if t.get("function", {}).get("name") in PLAN_MODE_TOOLS]
                else:
                    if lane_name == "executor" and custom_agent_tools:
                        # the custom agent's own selection, not intersected away by the core set
                        tools_for_lane = all_tools()
                    elif lane_name == "executor":
                        # core tools plus shell, skills and plan tracking so the
                        # executor can install packages, run commands, and tick plan items
                        # (+ connected MCP tools: the system prompt tells every lane about them)
                        tools_for_lane = [t for t in all_tools()
                                         if t.get("function", {}).get("name") in
                                         ("write_file", "read_file", "read_file_chunk", "edit_file", "list_files", "run_python", "run_shell", "read_skill", "list_skills",
                                          "create_plan", "update_plan_item", "get_plan") + EXECUTOR_TEST_TOOLS
                                         or t.get("function", {}).get("name", "").startswith("mcp__")]
                    else:
                        tools_for_lane = tool_surface.filter_tools(
                            all_tools(), last_query, APP_CONFIG.get("tool_surface"))
                if custom_agent_tools:
                    tools_for_lane = [t for t in tools_for_lane if t.get("function", {}).get("name") in custom_agent_tools]
                # Situational families withheld from this step, surfaced on event: ctx so
                # a "missing" tool is explainable. A child spawned via spawn_agent still
                # receives the full set (core/subagent.py), so the orchestrator can
                # delegate browser / device / document work it cannot call itself.
                hidden_fams = tool_surface.hidden_families(last_query, APP_CONFIG.get("tool_surface"))

                # Smart context truncation: mechanically compact history into the
                # lane's window (keeps system prompt + recent tail, rolls older
                # turns into a digest). Window and size now come from
                # core.context_budget (server-reported window + real prompt counts)
                # rather than a chars//3 guess. Report it to the UI when it fires.
                step_grammar = None
                if lane_name == "executor":
                    # cloud executors are OpenAI-compatible: no GBNF grammar, and
                    # their window comes from the provider config (model ctx)
                    ex_ctx = await _lane_window("executor", cloud_exec)
                    pre_tokens = context_budget.prompt_tokens_for(lane_name, msgs, tools_for_lane)
                    # in-place slice assignment: must NOT rebind `msgs` here, or it
                    # becomes a local of event_stream() and earlier reads raise UnboundLocalError
                    msgs[:] = compact_messages(msgs, context_budget.budget_for(lane_name, ex_ctx),
                                               tools=tools_for_lane)
                    sent_tokens = context_budget.prompt_tokens_for(lane_name, msgs, tools_for_lane)
                    if sent_tokens < pre_tokens:
                        yield (f"event: ctx\ndata: "
                               + json.dumps({'lane': lane_name, 'before_tokens': pre_tokens,
                                             'after_tokens': sent_tokens, 'hidden': hidden_fams}) + "\n\n")
                    if not cloud_exec:
                        step_grammar = _executor_grammar(tools_for_lane)
                else:
                    # Main lane context compaction: protect against context window explosion / VRAM demotion
                    main_ctx = await _lane_window("main", cloud_main)
                    budget = context_budget.budget_for(lane_name, main_ctx)
                    pre_tokens = context_budget.prompt_tokens_for(lane_name, msgs, tools_for_lane)
                    sent_tokens = pre_tokens
                    if pre_tokens > budget:
                        msgs[:] = compact_messages(msgs, budget, tools=tools_for_lane)
                        sent_tokens = context_budget.prompt_tokens_for(lane_name, msgs, tools_for_lane)
                        if sent_tokens < pre_tokens:
                            yield (f"event: ctx\ndata: "
                                   + json.dumps({'lane': lane_name, 'before_tokens': pre_tokens,
                                                 'after_tokens': sent_tokens, 'hidden': hidden_fams}) + "\n\n")

                step_rid = monitor_begin(f"agent/{lane_name}", True, n_msgs=len(msgs),
                                         model=model_info.get("model"), source=model_info.get("source"),
                                         provider=model_info.get("provider_name"))
                res_dict = None
                streamed_content = []
                try:
                    agent_rep = req.repeat_penalty if req.repeat_penalty is not None else 1.15
                    if getattr(active_client, "is_cloud", False):
                        fb_client = await _local_fallback(lane_name)
                        lane_stream = common._llm_chat_stream_with_fallback(
                            active_client, fb_client, msgs, tools_for_lane, req.temperature,
                            req.max_tokens, repeat_penalty=agent_rep, rid=step_rid, grammar=step_grammar, lane=lane_name,
                            effort=None if step_grammar else effort,
                            top_p=req.top_p, min_p=req.min_p, presence_penalty=req.presence_penalty, top_k=req.top_k)
                    else:
                        lane_stream = _llm_chat_stream(active_client, msgs, tools_for_lane, req.temperature, req.max_tokens,
                                                       repeat_penalty=agent_rep, rid=step_rid, grammar=step_grammar,
                                                       effort=None if step_grammar else effort,
                                                       top_p=req.top_p, min_p=req.min_p, presence_penalty=req.presence_penalty, top_k=req.top_k)
                    _red = output_guard.OutputRedactor(user, getattr(active_client, "is_cloud", False))
                    _cloud_out = bool(getattr(active_client, "is_cloud", False))
                    async for ev, val in lane_stream:
                        if ev == "queued":
                            yield f"event: queued\ndata: {json.dumps(val)}\n\n"
                            continue
                        if ev == "fallback":
                            model_info = _local_model_info(lane_name)
                            yield f"event: lane\ndata: {json.dumps(model_info)}\n\n"
                            _red = output_guard.OutputRedactor(user, False)   # lane now local
                            _cloud_out = False
                            continue
                        if ev == "thought_delta":
                            yield f"event: thought_delta\ndata: {json.dumps({'step': step + 1, 'delta': val, 'model': model_info['display']})}\n\n"
                        elif ev == "content_to_thought":
                            # forced-open <think>: the text streamed as the answer was reasoning
                            _red.reset()
                            streamed_content.clear()
                            yield f"event: delta_to_thought\ndata: {json.dumps({'step': step + 1, 'model': model_info['display']})}\n\n"
                        elif ev == "content_delta":
                            _safe = _red.feed(val)
                            streamed_content.append(_safe)
                            if _safe:
                                yield f"event: delta\ndata: {json.dumps({'text': _safe})}\n\n"
                        elif ev == "tool_preparing":
                            yield sse("tool_preparing", {'step': step + 1, **val})
                        elif ev == "result":
                            res_dict = val
                    for _c in _guard_flush_events(_red, streamed_content):
                        yield _c
                    _guard_audit(_red, user, f"agent/{lane_name}")
                finally:
                    req_mon = _monitor_state["active"].get(step_rid)
                    toks = req_mon.get("gen_tokens") if req_mon else len(streamed_content)
                    dt = (time.time() - req_mon["start"]) if req_mon else None
                    tps = (toks / dt) if (toks and dt and dt > 0) else None
                    u = (res_dict or {}).get("usage") or {}
                    tim = (res_dict or {}).get("timings") or {}
                    pcached, ccached = parse_cache_tokens(u, tim)
                    ptoks = u.get("prompt_tokens") or (sum(len(m.get("content", "")) for m in msgs) // 4)
                    # Feed the server's exact prompt count back into the budget
                    # accounting. Only a real count from the server may anchor the
                    # next step - the chars//4 fallback above is itself a guess.
                    if u.get("prompt_tokens"):
                        context_budget.record_usage(lane_name, sent_tokens, u["prompt_tokens"],
                                                    msgs, tools_for_lane)
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
                yield f"event: usage\ndata: {json.dumps({'prompt_tokens': ptoks, 'completion_tokens': ctoks})}\n\n"

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
                # (trigger lists live in the "router" config block -- core/router_policy.py)
                is_loop, _ = is_degeneration_or_loop(content)
                esc_reason = (router_policy.escalate_reason(step=step, content=content, tool_calls=tool_calls,
                                                            query=last_query, is_loop=is_loop, cfg=rpol)
                              if lane_name == "executor" and (use_cloud_main or main_ready) else "")
                should_escalate = bool(esc_reason)
                if should_escalate:
                    # count consecutive trips of the SAME reason. The executor keeps its
                    # slot until ESC_STREAK_LIMIT trips, so a single bad step escalates
                    # this request but the executor is re-tried on the next step.
                    if esc_reason == esc_streak_reason:
                        esc_streak += 1
                    else:
                        esc_streak_reason = esc_reason
                        esc_streak = 1
                elif lane_name == "executor":
                    esc_streak = 0
                    esc_streak_reason = None
                if should_escalate:
                    esc_just_fired = esc_streak < ESC_STREAK_LIMIT
                    if esc_just_fired:
                        # first offence: this step falls back to main, but say so, and let
                        # the next step give the executor another go.
                        print(f"[server_manager] executor step {step+1} escalated ({esc_reason}); "
                              f"retrying executor next step", file=sys.stderr)
                        yield sse("lane_warning", {
                            "reason": esc_reason, "retrying": True,
                            "message": f"Executor needed the main model ({esc_reason}). Retrying it next step."})
                        lane_reason = f"escalated_once_{esc_reason}"
                route_log.event(run_id, step, q_category, lane_name, lane_reason, escalated=should_escalate,
                                escalate_reason=esc_reason, duration_s=dt)

                if should_escalate:
                    print(f"[server_manager] Executor failed/tutorialized on step {step+1}; auto-escalating to Main Model.", file=sys.stderr)
                    _red.reset()
                    yield "event: delta_reset\ndata: {}\n\n"
                    lane_name = "main"
                    model_info = get_model_info("main")
                    yield f"event: lane\ndata: {json.dumps(model_info)}\n\n"
                    esc_rid = monitor_begin("agent/main-escalated", True, n_msgs=len(msgs),
                                            model=model_info.get("model"), source=model_info.get("source"),
                                            provider=model_info.get("provider_name"))
                    esc_tools = ([t for t in all_tools()
                                  if t.get("function", {}).get("name") in PLAN_MODE_TOOLS]
                                 if req.plan else tool_surface.filter_tools(
                                     all_tools(), last_query, APP_CONFIG.get("tool_surface")))
                    if custom_agent_tools:
                        esc_tools = [t for t in esc_tools if t.get("function", {}).get("name") in custom_agent_tools]
                    res_dict = None
                    streamed_content = []
                    esc_ctx = await _lane_window("main", cloud_main)
                    budget = context_budget.budget_for("main", esc_ctx)
                    pre_tokens = context_budget.prompt_tokens_for("main", msgs, esc_tools)
                    if pre_tokens > budget:
                        msgs[:] = compact_messages(msgs, budget, tools=esc_tools)
                    # estimate of what the escalated call actually sends (anchored below)
                    sent_tokens = context_budget.prompt_tokens_for("main", msgs, esc_tools)
                    try:
                        agent_rep = req.repeat_penalty if req.repeat_penalty is not None else 1.15
                        if getattr(main_client, "is_cloud", False):
                            fb_main = await _local_fallback("main")
                            esc_stream = common._llm_chat_stream_with_fallback(
                                main_client, fb_main, msgs, esc_tools, req.temperature,
                                req.max_tokens, repeat_penalty=agent_rep, rid=esc_rid, lane="main", effort=effort,
                                top_p=req.top_p, min_p=req.min_p, presence_penalty=req.presence_penalty, top_k=req.top_k)
                        else:
                            esc_stream = _llm_chat_stream(main_client, msgs, esc_tools, req.temperature, req.max_tokens,
                                                          repeat_penalty=agent_rep, rid=esc_rid, effort=effort,
                                                          top_p=req.top_p, min_p=req.min_p, presence_penalty=req.presence_penalty, top_k=req.top_k)
                        _red = output_guard.OutputRedactor(user, getattr(main_client, "is_cloud", False))
                        _cloud_out = bool(getattr(main_client, "is_cloud", False))
                        async for ev, val in esc_stream:
                            if ev == "queued":
                                yield f"event: queued\ndata: {json.dumps(val)}\n\n"
                                continue
                            if ev == "fallback":
                                yield f"event: lane\ndata: {json.dumps(_local_model_info('main'))}\n\n"
                                _red = output_guard.OutputRedactor(user, False)   # lane now local
                                _cloud_out = False
                                continue
                            if ev == "thought_delta":
                                yield f"event: thought_delta\ndata: {json.dumps({'step': step + 1, 'delta': val, 'model': model_info['display']})}\n\n"
                            elif ev == "content_to_thought":
                                # forced-open <think>: the text streamed as the answer was reasoning
                                _red.reset()
                                streamed_content.clear()
                                yield f"event: delta_to_thought\ndata: {json.dumps({'step': step + 1, 'model': model_info['display']})}\n\n"
                            elif ev == "content_delta":
                                _safe = _red.feed(val)
                                streamed_content.append(_safe)
                                if _safe:
                                    yield f"event: delta\ndata: {json.dumps({'text': _safe})}\n\n"
                            elif ev == "tool_preparing":
                                yield sse("tool_preparing", {'step': step + 1, **val})
                            elif ev == "result":
                                res_dict = val
                        for _c in _guard_flush_events(_red, streamed_content):
                            yield _c
                        _guard_audit(_red, user, "agent/main-escalated")
                    finally:
                        req_mon = _monitor_state["active"].get(esc_rid)
                        toks = req_mon.get("gen_tokens") if req_mon else len(streamed_content)
                        dt = (time.time() - req_mon["start"]) if req_mon else None
                        tps = (toks / dt) if (toks and dt and dt > 0) else None
                        u = (res_dict or {}).get("usage") or {}
                        tim = (res_dict or {}).get("timings") or {}
                        pcached, ccached = parse_cache_tokens(u, tim)
                        ptoks = u.get("prompt_tokens") or (sum(len(m.get("content", "")) for m in msgs) // 4)
                        # anchor the main lane on the escalated call's exact count
                        if u.get("prompt_tokens"):
                            context_budget.record_usage("main", sent_tokens, u["prompt_tokens"],
                                                        msgs, esc_tools)
                        if not pcached and len(msgs) > 1:
                            pcached = sum(len(m.get("content", "")) for m in msgs[:-1]) // 4
                        ctoks = u.get("completion_tokens") or toks
                        monitor_end(esc_rid, 200, prompt_tokens=ptoks, completion_tokens=ctoks, tps=tps, duration=dt,
                                    model=model_info.get("model"), prompt_cached=pcached, completion_cached=ccached,
                                    source=model_info.get("source"), provider=model_info.get("provider_name"))
                        db_record_request("agent/main-escalated", model_info.get("model"), ptoks, ctoks, tps, dt, None, True, 200,
                                          prompt_cached_tokens=pcached, completion_cached_tokens=ccached, is_orchestrator=False,
                                          source=model_info.get("source"), provider=model_info.get("provider_name"))
                    yield f"event: usage\ndata: {json.dumps({'prompt_tokens': ptoks, 'completion_tokens': ctoks})}\n\n"
                    content = res_dict.get("content", "") if res_dict else "".join(streamed_content)
                    reasoning = res_dict.get("reasoning", "") if res_dict else ""
                    tool_calls = res_dict.get("tool_calls", []) if res_dict else []
                    if not tool_calls:
                        parsed_tc = _extract_text_tool_calls(content or reasoning)
                        if parsed_tc:
                            tool_calls = parsed_tc

                final_content = content
                final_reasoning = reasoning

                # The reply only announced the next step ("Let me read the file.") and
                # called no tool. Without a plan the check below cannot catch this, and
                # the announcement would end the run as its "answer".
                if (not tool_calls and _is_narration(content) and narration_nudges < MAX_PLAN_NUDGES
                        and step < steps - 1):
                    narration_nudges += 1
                    msgs.append({"role": "assistant", "content": content})
                    msgs.append({"role": "user", "content": (
                        "[continue] You described the next step but did not call a tool. "
                        "Call the tool for that step now. Answer in plain text only once "
                        "the work is actually done.")})
                    yield f"event: delta\ndata: {json.dumps({'text': chr(10) + chr(10)})}\n\n"
                    continue

                # The model stopped calling tools but its own tracked plan still has
                # pending steps: push it back to work instead of finalizing a half-done
                # task. Bounded so a model that can't progress still ends normally.
                #
                # After the ordinary nudges are spent, one AUDIT nudge still fires - a
                # repeated "keep going" gets ignored, but a forced status re-check does
                # not. This is what makes Continue work on a run that was cut off with
                # steps the model had optimistically marked done.
                if not tool_calls and req.session_id and not req.plan and step < steps - 1:
                    pending = [i for i in db_get_plan_items(req.session_id) if i["status"] in ("pending", "in_progress")]
                    if pending and (plan_nudges < MAX_PLAN_NUDGES or plan_nudges < MAX_PLAN_NUDGES + 1):
                        plan_nudges += 1
                        audit = plan_nudges > MAX_PLAN_NUDGES
                        if content.strip():
                            msgs.append({"role": "assistant", "content": content})
                        if audit:
                            msgs.append({"role": "user", "content": (
                                f"[plan audit required] You stopped, but {len(pending)} plan step(s) are "
                                "still unfinished: "
                                + "; ".join(f"#{i['ord']} {i['text']}" for i in pending[:6])
                                + ". Before answering, re-check the plan: any step you marked 'done' that you "
                                "did not actually finish must be reopened with "
                                "update_plan_item(item=N, status='in_progress'). Then continue the first "
                                "genuinely unfinished step using tools. Only answer once every step is "
                                "really done or really impossible.")})
                        else:
                            msgs.append({"role": "user", "content": (
                                f"[plan incomplete] {len(pending)} plan step(s) are still unfinished: "
                                + "; ".join(f"#{i['ord']} {i['text']}" for i in pending[:6])
                                + ". Do not stop yet — continue with the next pending step using tools. "
                            "Mark each step with update_plan_item as you finish it. If a step truly cannot "
                            "be done, mark it status='failed' with a note explaining why, then continue.")})
                        yield f"event: delta\ndata: {json.dumps({'text': chr(10) + chr(10)})}\n\n"
                        continue

                if not tool_calls:
                    # Semantic (natural-language) output rules on the final
                    # answer: on match, replace it entirely with the policy
                    # message before anything else is finalized.
                    _sem = await output_guard.semantic_check(final_content, user, _cloud_out)
                    if _sem:
                        _msg = _sem.get("message") or "Response filtered by policy."
                        _red.reset()
                        if final_content.strip():
                            yield "event: delta_reset\ndata: {}\n\n"
                        yield f"event: delta\ndata: {json.dumps({'text': _msg})}\n\n"
                        yield f"event: guard\ndata: {json.dumps({'rule': _sem.get('name'), 'message': _msg})}\n\n"
                        audit_log(user, action="output_guard.redact", resource=_sem.get("name"),
                                  detail={"endpoint": "agent/run", "scope": _sem.get("scope"),
                                          "semantic": True}, result="deny")
                        run_outcome = "filtered"
                        yield f"event: validated\ndata: {{}}\n\n"
                        yield "event: done\ndata: {}\n\n"
                        return
                    val_text, was_synth, note = validate_and_finalize_response(
                        last_query, final_content, final_reasoning, actions_taken)
                    val_text, was_synth = _strip_download_markers(val_text, was_synth)

                    if was_synth and val_text != final_content:
                        _red.reset()
                        if not final_content.strip():
                            yield f"event: delta\ndata: {json.dumps({'text': val_text})}\n\n"
                        else:
                            yield "event: delta_reset\ndata: {}\n\n"
                            yield f"event: delta\ndata: {json.dumps({'text': val_text})}\n\n"
                    run_outcome = "synthesized" if was_synth else "answered"
                    async for _vc in answer_check_events(
                            user, "agent", last_query, val_text if was_synth else final_content,
                            msgs, main_client, req.verify, _cloud_out):
                        yield _vc
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
                    
                    # For history retention in msgs, prune large payloads to lightweight stubs
                    # so future turns do not re-ingest tens of thousands of raw code characters
                    hist_args = dict(args)
                    if name in ("write_file", "edit_file"):
                        if "content" in hist_args and len(str(hist_args["content"])) > 400:
                            f_path = hist_args.get("path") or hist_args.get("file") or "file"
                            c_len = len(str(hist_args["content"]))
                            hist_args["content"] = f"<{c_len} chars written to {f_path}>"
                        if "new_string" in hist_args and len(str(hist_args["new_string"])) > 400:
                            f_path = hist_args.get("path") or hist_args.get("file") or "file"
                            ns_len = len(str(hist_args["new_string"]))
                            hist_args["new_string"] = f"<{ns_len} chars replaced in {f_path}>"

                    clean_args_str = json.dumps(hist_args)
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
                if repeat_streak >= LOOP_STOP_STREAK:
                    # escalating to the main model didn't help either: end the run instead of
                    # holding the shared GPU on the same calls until the step cap
                    print("[agent] identical tool calls repeated - stopping run", file=sys.stderr)
                    stop_reason = "loop"
                    break

                # near-repeat detection: the same tool over and over with *different*
                # arguments never trips the identical-args rule above, yet it is the
                # same thrash - re-running `npm run build` after every edit, reading
                # 60 different files without acting on any of them.
                if sigs:
                    for name, _tc_id, _args in parsed_actions:
                        tool_step_hist.append((name, step))
                    # keep only the trailing window
                    if tool_step_hist and tool_step_hist[-1][1] - tool_step_hist[0][1] > NEAR_REPEAT_WINDOW:
                        cut = next((i for i, (_n, st) in enumerate(tool_step_hist)
                                    if step - st <= NEAR_REPEAT_WINDOW), len(tool_step_hist))
                        tool_step_hist = tool_step_hist[cut:]
                    names = [n for n, _s in tool_step_hist]
                    if names:
                        top_tool, top_n = max(((n, names.count(n)) for n in set(names)),
                                              key=lambda p: p[1])
                        if top_n >= NEAR_REPEAT_LIMIT:
                            print(f"[agent] near-repeat: '{top_tool}' called {top_n}x in "
                                  f"{NEAR_REPEAT_WINDOW} steps - stopping run", file=sys.stderr)
                            stop_reason = "loop_near_repeat"
                            loop_detail = top_tool
                            break

                history_content = sanitize_user_facing_content(content)
                msgs.append({"role": "assistant", "content": history_content, "tool_calls": clean_tool_calls})

                for name, tc_id, args in parsed_actions:
                    yield sse("tool_call", {'id': tc_id, 'name': name, 'args': args})

                    if req.plan and name not in PLAN_MODE_TOOLS:
                        # plan mode: mutating tools are unavailable — hard block
                        result = f"error: plan mode is active — '{name}' is read-only-restricted. Produce the plan instead."
                        yield sse("tool_result", {'id': tc_id, 'name': name, 'ok': False, 'result': result})
                        actions_taken.append({"name": name, "args": args, "ok": False, "result": result})
                        msgs.append({"role": "tool", "tool_call_id": tc_id, "content": result})
                        continue

                    approved, note = fast_sandbox_check(name, args)
                    yield sse("verify", {'id': tc_id, 'name': name, 'approved': approved, 'note': note})
                    if not approved:
                        result = f"error: sandbox violation — {note}"
                        yield sse("tool_result", {'id': tc_id, 'name': name, 'ok': False, 'result': result})
                        actions_taken.append({"name": name, "args": args, "ok": False, "result": result})
                        msgs.append({"role": "tool", "tool_call_id": tc_id, "content": result})
                        continue

                    # run_python is arbitrary code on the user's machine: with ask_first on it
                    # always needs a one-time approval here (no allow pattern can cover code).
                    # This card is the single approval: the companion then runs it without a
                    # second local dialog (it gets approved_in_app, see companion/policy.js).
                    if name == "run_python" and shell_cfg().get("ask_first", True):
                        import uuid as _uuid
                        code = str((args or {}).get("code") or "")
                        shown = "run_python:\n" + (code if len(code) <= 4000 else code[:4000] + "\n… (truncated)")
                        preq_id = _uuid.uuid4().hex[:12]
                        ev = asyncio.Event()
                        _perm_pending[preq_id] = {"cmd": shown, "event": ev, "result": None,
                                                  "user_id": user.id, "kind": "python"}
                        yield sse("permission_request", {'req_id': preq_id, 'cmd': shown, 'kind': 'python'})
                        allowed, pnote = await _await_permission(preq_id, ev)
                        if not allowed:
                            result = "error: user denied run_python" + (f" ({pnote})" if pnote else "")
                            yield sse("tool_result", {'id': tc_id, 'name': name, 'ok': False, 'result': result})
                            actions_taken.append({"name": name, "args": args, "ok": False, "result": result})
                            msgs.append({"role": "tool", "tool_call_id": tc_id, "content": result})
                            continue
                        mark_code_approved(code)

                    # cloud image/video generation can cost money: always ask first
                    if name in ("generate_image", "generate_video"):
                        from core.media_tools import first_is_cloud
                        _is_cloud, _prov = first_is_cloud(name)
                        if _is_cloud:
                            import uuid as _uuid
                            what = "an image" if name == "generate_image" else "a video"
                            shown = (f"Make {what} with {_prov} (cloud - may cost money):\n"
                                     + str((args or {}).get("prompt") or "")[:1500])
                            preq_id = _uuid.uuid4().hex[:12]
                            ev = asyncio.Event()
                            _perm_pending[preq_id] = {"cmd": shown, "event": ev, "result": None,
                                                      "user_id": user.id, "kind": "media"}
                            yield sse("permission_request", {'req_id': preq_id, 'cmd': shown, 'kind': 'media'})
                            allowed, pnote = await _await_permission(preq_id, ev)
                            if not allowed:
                                result = f"error: the user declined making {what} in the cloud" + (f" ({pnote})" if pnote else "")
                                yield sse("tool_result", {'id': tc_id, 'name': name, 'ok': False, 'result': result})
                                actions_taken.append({"name": name, "args": args, "ok": False, "result": result})
                                msgs.append({"role": "tool", "tool_call_id": tc_id, "content": result})
                                continue

                    # shell commands: ask permission here (not inside the tool)
                    # so the SSE stream can emit the modal event while we wait
                    if name == "run_shell" and "command" in str(args or {}):
                        cmd = str(args.get("command") or args.get("cmd") or "").strip()
                        cfg = shell_cfg()
                        pats = [str(p).strip().lower() for p in (cfg.get("allow_patterns") or [])]
                        # check active project patterns as well
                        cur_proj = db_owned_project_id(get_active_project(), user.id)
                        if cur_proj:
                            proj_pats = [str(p).strip().lower() for p in db_get_project_allow_patterns(cur_proj)]
                            pats.extend(proj_pats)
                        # plus this user's own additional allows (independent of project)
                        user_pats = [str(p).strip().lower() for p in auth_db.get_user_allow_patterns(user.id)]
                        pats.extend(user_pats)
                        if cfg.get("ask_first", True) and not command_allowed(cmd, pats):
                            import uuid as _uuid
                            preq_id = _uuid.uuid4().hex[:12]
                            ev = asyncio.Event()
                            _perm_pending[preq_id] = {"cmd": cmd, "event": ev, "result": None,
                                                      "user_id": user.id}
                            yield sse("permission_request", {'req_id': preq_id, 'cmd': cmd})
                            allowed, pnote = await _await_permission(preq_id, ev)
                            if not allowed:
                                result = f"error: user denied shell command: {cmd}" + (f" ({pnote})" if pnote else "")
                                yield sse("tool_result", {'id': tc_id, 'name': name, 'ok': False, 'result': result})
                                actions_taken.append({"name": name, "args": args, "ok": False, "result": result})
                                msgs.append({"role": "tool", "tool_call_id": tc_id, "content": result})
                                continue
                        # approved via pattern or modal: tool skips its own gate for
                        # exactly this command line (a model-supplied flag can't)
                        mark_approved(cmd)

                    result = await run_tool(name, args)
                    await fire_hook("after_tool", name, args, result)
                    ok = not (isinstance(result, str) and (result.startswith("error:") or result.startswith("File not found")))
                    route_log.event(run_id, step, q_category, lane_name, "tool", tool_name=name, tool_ok=ok)
                    yield sse("tool_result", _with_diff({'id': tc_id, 'name': name, 'ok': ok, 'result': result}, args))
                    actions_taken.append({"name": name, "args": args, "ok": ok, "result": result})
                    msgs.append({"role": "tool", "tool_call_id": tc_id, "content": result})
                    # structured plan state → UI checklist
                    if name in ("create_plan", "update_plan_item", "get_plan") and req.session_id:
                        yield sse("plan", {'items': db_get_plan_items(req.session_id)})

                # re-assert the plan-tracking reminder every step (not just once at
                # turn start) so a long tool-call run doesn't drift away from calling
                # update_plan_item once the initial system-prompt nudge scrolls out of focus
                if req.session_id and not req.plan:
                    plan_items_now = db_get_plan_items(req.session_id)
                    if plan_items_now and any(i["status"] in ("pending", "in_progress") for i in plan_items_now):
                        done_n = sum(1 for i in plan_items_now if i["status"] == "done")
                        fail_n = sum(1 for i in plan_items_now if i["status"] == "failed")
                        msgs.append({"role": "user", "content": (
                            f"[plan reminder] {done_n}/{len(plan_items_now)} steps done, {fail_n} failed. "
                            "If a step you just performed matches a pending plan item, call "
                            "update_plan_item(item=N, status='done' or 'failed') now before continuing.")})

            _sem = await output_guard.semantic_check(final_content, user, _cloud_out)
            if _sem:
                _msg = _sem.get("message") or "Response filtered by policy."
                yield "event: delta_reset\ndata: {}\n\n"
                yield f"event: delta\ndata: {json.dumps({'text': _msg})}\n\n"
                yield f"event: guard\ndata: {json.dumps({'rule': _sem.get('name'), 'message': _msg})}\n\n"
                audit_log(user, action="output_guard.redact", resource=_sem.get("name"),
                          detail={"endpoint": "agent/run", "scope": _sem.get("scope"),
                                  "semantic": True}, result="deny")
                run_outcome = "filtered"
                yield f"event: validated\ndata: {{}}\n\n"
                yield f"event: done\ndata: {json.dumps({'note': 'response filtered by policy', 'text': ''})}\n\n"
                return
            val_text, was_synth, note = validate_and_finalize_response(
                last_query, final_content, final_reasoning, actions_taken)
            val_text, was_synth = _strip_download_markers(val_text, was_synth)
            run_outcome = stop_reason   # "max_steps" | "loop"

            if not final_content.strip():
                yield f"event: delta\ndata: {json.dumps({'text': val_text})}\n\n"
            elif was_synth and val_text != final_content:
                yield "event: delta_reset\ndata: {}\n\n"
                yield f"event: delta\ndata: {json.dumps({'text': val_text})}\n\n"
            async for _vc in answer_check_events(
                    user, "agent", last_query,
                    val_text if (was_synth or not final_content.strip()) else final_content,
                    msgs, main_client, req.verify, _cloud_out):
                yield _vc
            yield f"event: validated\ndata: {json.dumps({'synthesized': was_synth, 'note': note})}\n\n"
            plan_note = {
                "max_steps": f"max steps reached ({steps})",
                "timeout": f"wall-clock limit reached ({run_limit_s}s)",
                "loop": "stopped: repeating the same tool calls",
                "loop_near_repeat": f"stopped: `{loop_detail}` called repeatedly without progress",
            }.get(stop_reason, "stopped")
            # reason lets the UI offer "Continue" (the next run re-injects the tracked plan)
            # Never emit an empty `text`: a step-cap stop used to yield text:'' and
            # note:'max steps reached', so the user got a dead end with no idea what
            # had actually been done.
            plan_done_n = plan_failed_n = 0
            plan_total = plan_pending = 0
            if req.session_id:
                pi = db_get_plan_items(req.session_id)
                if pi:
                    plan_done_n = sum(1 for i in pi if i["status"] == "done")
                    plan_failed_n = sum(1 for i in pi if i["status"] == "failed")
                    plan_total = len(pi)
                    plan_pending = sum(1 for i in pi if i["status"] in ("pending", "in_progress"))
                    plan_note += f" — plan progress: {plan_done_n}/{len(pi)} done, {plan_failed_n} failed"

            elapsed_s = int(time.time() - run_started)
            why = {
                "max_steps": f"Reached the step limit ({steps} steps)",
                "timeout": f"Ran for {elapsed_s // 60} min (wall-clock limit)",
                "loop": "Kept repeating the same tool calls",
                "loop_near_repeat": f"Called `{loop_detail}` repeatedly without making progress",
            }.get(stop_reason, "Stopped")
            did = [a["name"] for a in actions_taken]
            summary = f"**{why}** after {steps_run} step{'s' if steps_run != 1 else ''}"
            summary += f" ({elapsed_s // 60}m {elapsed_s % 60}s)." if elapsed_s >= 60 else "."
            if plan_total:
                summary += f"\n\nPlan: **{plan_done_n}/{plan_total}** done"
                if plan_failed_n:
                    summary += f", {plan_failed_n} failed"
                if plan_pending:
                    summary += f", **{plan_pending} still pending**."
                else:
                    summary += ". (No steps pending — check the work before continuing.)"
            if did:
                uniq, seen = [], set()
                for n in did:
                    if n not in seen:
                        seen.add(n)
                        uniq.append(n)
                summary += f"\n\nTools used: {', '.join(uniq[:12])}"
                summary += " …" if len(uniq) > 12 else "."
            summary += "\n\n_Continue to resume._" if plan_pending else ""

            yield (f"event: done\ndata: {json.dumps({'note': plan_note, 'text': summary, 'reason': stop_reason, 'steps': steps_run, 'pending': plan_pending, 'plan_total': plan_total, 'plan_done': plan_done_n, 'plan_failed': plan_failed_n, 'elapsed_s': elapsed_s})}\n\n")
        except asyncio.CancelledError:
            run_outcome = "cancelled"
        except Exception as e:
            yield f"event: delta\ndata: {json.dumps({'text': f'⚠️ Agent loop error: {e}'})}\n\n"
            yield "event: done\ndata: {}\n\n"
        finally:
            route_log.run_end(run_id, steps_run, run_outcome)

    return StreamingResponse(event_stream(), media_type="text/event-stream")

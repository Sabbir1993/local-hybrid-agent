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

from fastapi import APIRouter, Depends, UploadFile, File as FastAPIFile, Request
from fastapi.responses import StreamingResponse, JSONResponse, FileResponse
from pydantic import BaseModel

from core.auth import Principal, user_has_permission
from core.backend import device_prefix
from core.config import BASE_DIR, CONFIG_DEFAULTS
from core.deps import get_current_user, require_permission
from core.audit import audit_log
from core import input_guard
from core.knowledge_access import allowed_source_ids_for, kb_local_only, set_kb_cloud_blocked
from core.memory import search_memory_hybrid
from core.file_tools import extract_file_content, MIME_MAP
from core.db import (
    db_record_request,
    db_add_project_allow_pattern,
    db_get_plan_items,
    db_get_project_allow_patterns,
    db_owned_project_id,
    db_session_owner,
)
from core import auth_db
from core.small_model import (
    APP_CONFIG,
    small_models,
    needle_route,
    needle_available,
    router_route,
    router_available,
    router_engine_name,
)
from core.state import state
from core.request_context import get_current_user_id
from core import cloud
from core import output_guard
from core.agent_tools import (
    AGENT_TOOLS,
    AGENT_CORE_TOOLS,
    active_workspace,
    common_workspace,
    get_active_project,
    project_workspace_dir,
    set_plan_context,
    _ws_resolve,
    _common_resolve,
    _ws_changes,
    _remote_uid,
    _save_text_as_excel,
    _create_default_excel,
    generate_fresh_dashboard_html,
    tool_write_file_common,
)
from core import companion_bridge
from core.registry import registry
from core.grammar import build_tool_call_grammar, envelope_examples
from core.web_tools import register_web_tools
from core.skills import skills_prompt_fragment
from core.plugins import plugins_prompt_fragment, fire_hook
from core.shell_tools import (
    add_allow_pattern,
    command_allowed,
    mark_approved,
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


def _gpu_device_label(gpu_devices: list) -> str:
    """UI device badge for a set of local GPU indices, e.g. '2x GPU (Vulkan)'.
    Vendor-neutral: works for any backend/GPU count instead of assuming
    'Intel Arc A770'."""
    backend = (state.profile or {}).get("backend", CONFIG_DEFAULTS["backend"])
    prefix = device_prefix(backend)
    n = len(gpu_devices) if gpu_devices else 1
    return f"{n}x GPU ({prefix})" if n > 1 else f"GPU ({prefix})"


def _main_device_label() -> str:
    gpu_devices = (state.profile or {}).get("gpu_devices") or CONFIG_DEFAULTS["gpu_devices"]
    return _gpu_device_label(gpu_devices)


def _executor_device_label(gpu: "int | None" = None) -> str:
    backend = (state.profile or {}).get("backend", CONFIG_DEFAULTS["backend"])
    prefix = device_prefix(backend)
    return f"GPU #{gpu} ({prefix}{gpu})" if gpu is not None else f"GPU ({prefix})"

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
    cloud_model_override: Optional[str] = None   # key of cloud model for executor/vision lanes


AGENT_MAX_STEPS = 30


# ---------------- output sanitizer helpers ----------------
def _guard_flush_events(redactor, streamed_content) -> list:
    """Flush the output-guard holdback at end of a lane stream. Returns SSE
    chunks the caller must yield (tail delta + one-time guard notice)."""
    chunks = []
    _tail = redactor.flush()
    if _tail:
        streamed_content.append(_tail)
        chunks.append(f"event: delta\ndata: {json.dumps({'text': _tail})}\n\n")
    if redactor.matched:
        chunks.append(f"event: guard\ndata: "
                      + json.dumps({"rule": redactor.matched.get("name"),
                                    "message": redactor.matched.get("message")}) + "\n\n")
    return chunks


def _guard_audit(redactor, user, endpoint: str) -> None:
    if redactor.matched:
        audit_log(user, action="output_guard.redact", resource=redactor.matched.get("name"),
                  detail={"endpoint": endpoint, "scope": redactor.matched.get("scope"),
                          "hits": redactor.hits}, result="deny")

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
    decision: str            # allow | project | user | always | deny
    pattern: Optional[str] = None
    project_id: Optional[Union[int, str]] = None


@router.post("/agent/permission")
async def agent_permission_answer(req: PermissionAnswerReq, user: Principal = Depends(get_current_user)):
    """UI answers a permission_request emitted on the agent SSE stream."""
    rec = _perm_pending.get(req.req_id)
    # only the user whose agent run raised the request may answer it
    if rec is None or rec.get("user_id") != user.id:
        return JSONResponse({"error": "unknown or expired permission request"}, status_code=404)
    if req.decision == "always" and req.pattern:
        # "always" edits the global allowlist for every user -- same permission
        # as the Settings shell card (routes/capabilities.py)
        if not user_has_permission(user, "settings.shell.configure"):
            audit_log(user, action="settings.shell.configure", permission_key="settings.shell.configure",
                      result="deny", detail={"pattern": req.pattern, "via": "agent/permission"})
            return JSONResponse({"error": "missing permission: settings.shell.configure "
                                          "(choose 'allow for me' instead)"}, status_code=403)
        add_allow_pattern(req.pattern)
        audit_log(user, action="shell.allow_pattern.add", resource=req.pattern,
                  permission_key="settings.shell.configure", detail={"scope": "global"})
    elif req.decision == "project" and req.pattern:
        # Save to project-specific allowed patterns -- caller's own project only
        target_proj = req.project_id or get_active_project()
        if target_proj:
            pid = db_owned_project_id(target_proj, user.id)
            if pid is None:
                return JSONResponse({"error": "project not found"}, status_code=404)
            db_add_project_allow_pattern(pid, req.pattern)
    elif req.decision == "user" and req.pattern:
        # Save to this user's own allow list -- never affects other users
        auth_db.add_user_allow_pattern(user.id, req.pattern)
    rec["result"] = {"allow": req.decision != "deny",
                     "note": f"pattern allowed for {req.decision}" if req.decision in ("always", "project", "user") else ""}
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

    if not companion_bridge.is_connected(user.id):
        return JSONResponse(
            {"error": "agent_requires_companion",
             "message": "Agent Task mode requires the A770 Companion app. Install and open it, then try again."},
            status_code=403,
        )

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

    steps = max(1, min(req.max_steps, APP_CONFIG["agent"].get("max_steps", AGENT_MAX_STEPS)))
    msgs = [dict(m) for m in req.messages]

    # --- Input sanitizer (core/input_guard.py) -----------------------------
    # Runs after mode/lane resolution, so 'all-local' never trips cloud_only
    # rules. Scans all user messages plus attachment names/previews (a pasted
    # invoice is caught even when the regex only exists inside the file text).
    # NOTE: must stay AFTER the `msgs = ...` assignment above (it reads msgs).
    _any_cloud = bool(cloud_main) or bool(cloud_exec)
    _scan_texts = [str(m.get("content", "")) for m in msgs if m.get("role") == "user"]
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
    if kb_ids and _kb_query.strip():
        try:
            from core.knowledge_router import fetch_company_knowledge
            kb_hits, kb_prompt = await fetch_company_knowledge(_kb_query, kb_ids, k=6)
        except Exception as e:
            print(f"[agent] knowledge retrieval failed: {e}", file=sys.stderr)
    # Data residency (core/knowledge_access.py): internal knowledge never goes
    # to a cloud provider. A run whose question hits the KB is moved onto the
    # local lanes; if the local main model can't run, the KB context is withheld.
    if kb_hits and (use_cloud_main or cloud_exec) and kb_local_only():
        target = state.profile_path or state.profile or common.initial_profile_path
        try:
            if target:
                await state.ensure_running(target)
        except Exception as e:
            print(f"[agent] local model for knowledge query unavailable: {e}", file=sys.stderr)
        if state.is_running():
            cloud_main, cloud_exec, use_cloud_main, main_ready = None, None, False, True
            audit_log(user, action="knowledge.local_only", resource="agent/run",
                      detail={"reason": "kb hits on a cloud lane", "hits": len(kb_hits)})
        else:
            kb_hits = []
            sys_prompt += ("\n\nNOTE: The company knowledge base is restricted to local models and no "
                           "local model is loaded, so internal company data is not available for this "
                           "answer. Tell the user this instead of guessing.")
    if kb_hits:
        sys_prompt += "\n\n" + kb_prompt
    # the search_knowledge_base tool refuses while any cloud lane is in play
    # (its results would land in history that a cloud lane later reads)
    set_kb_cloud_blocked(bool(use_cloud_main or cloud_exec))
    # capability prompt fragments: skills listing + plugin guidance
    for frag in (skills_prompt_fragment(), plugins_prompt_fragment()):
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
    has_sys = False
    for m in msgs:
        if m.get("role") == "system":
            has_sys = True
            m["content"] = (m.get("content") or "").strip() + "\n\n" + sys_prompt
            break
    if not has_sys:
        msgs.insert(0, {"role": "system", "content": sys_prompt})

    if mode in ("no-orchestration", "all-cloud", "direct"):
        use_executor = False
    else:
        use_executor = bool(cloud_exec) or ex_inst.available
    main_client = cloud.CloudClient(cloud_main) if use_cloud_main else state.client

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

                executor_stuck = repeat_streak >= 2
                lane_name = "main" if not use_executor or executor_stuck else "executor"

                is_creation_or_code = any(w in last_query.lower() for w in ("make", "create", "generate", "write", "build", "code", "add", "fix", "html", "script", "page"))
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
                            nr = await asyncio.get_event_loop().run_in_executor(
                                None, router_route, last_query, all_tools())
                        except Exception as e:
                            print(f"[agent] router error: {e}", file=sys.stderr)

                    r_duration = max(0.001, time.time() - r_start)
                    if nr:
                        r_c_toks = max(1, (len(nr.get("reasoning", "")) + len(json.dumps(nr.get("args", {})))) // 4)
                        r_tps = round(r_c_toks / r_duration, 1) if r_duration > 0 else 50.0
                        monitor_end(r_rid, 200, prompt_tokens=r_p_toks, completion_tokens=r_c_toks,
                                    duration=r_duration, tps=r_tps, model=r_model_name, source="local")

                        yield f"event: lane\ndata: {json.dumps(r_model_info)}\n\n"
                        if nr.get("reasoning"):
                            yield f"event: thought\ndata: {json.dumps({'step': step + 1, 'text': nr['reasoning'], 'model': r_model_info['display']})}\n\n"
                        tc_id = "n0"
                        yield f"event: tool_call\ndata: {json.dumps({'id': tc_id, 'name': nr['name'], 'args': nr['args'], 'model': r_model_info['display'], 'device': r_model_info['device']})}\n\n"
                        result = await run_tool(nr["name"], nr["args"])
                        ok = not (isinstance(result, str) and (result.startswith("error:") or result.startswith("File not found")))
                        yield f"event: tool_result\ndata: {json.dumps({'id': tc_id, 'name': nr['name'], 'ok': ok, 'result': result, 'model': r_model_info['display']})}\n\n"
                        actions_taken.append({"name": nr["name"], "args": nr["args"], "ok": ok, "result": result})
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
                else:
                    # Main lane context compaction: protect against context window explosion / VRAM demotion
                    main_ctx = 32768
                    if cloud_main:
                        main_ctx = getattr(cloud_main, "ctx", 32768) or 32768
                    elif isinstance(state.profile, dict):
                        main_ctx = int(state.profile.get("context_size") or 32768)
                        # llama-server divides -c across -np slots unless the KV pool
                        # is unified (then --kv-unified-per-slot, if set, is the cap)
                        n_slots = int(state.profile.get("n_slots") or 1)
                        if state.profile.get("kv_unified"):
                            main_ctx = int(state.profile.get("kv_unified_per_slot") or main_ctx)
                        elif n_slots > 1:
                            main_ctx //= n_slots
                    pre_tokens = estimate_prompt_tokens(msgs)
                    budget = int(main_ctx * 0.7)
                    if pre_tokens > budget:
                        msgs[:] = compact_messages(msgs, budget)
                        post_tokens = estimate_prompt_tokens(msgs)
                        if post_tokens < pre_tokens:
                            yield (f"event: ctx\ndata: "
                                   + json.dumps({'lane': lane_name, 'before_tokens': pre_tokens,
                                                 'after_tokens': post_tokens}) + "\n\n")

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
                        elif ev == "content_delta":
                            _safe = _red.feed(val)
                            streamed_content.append(_safe)
                            if _safe:
                                yield f"event: delta\ndata: {json.dumps({'text': _safe})}\n\n"
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
                    _red.reset()
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
                            elif ev == "content_delta":
                                _safe = _red.feed(val)
                                streamed_content.append(_safe)
                                if _safe:
                                    yield f"event: delta\ndata: {json.dumps({'text': _safe})}\n\n"
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
                        yield f"event: validated\ndata: {{}}\n\n"
                        yield "event: done\ndata: {}\n\n"
                        return
                    val_text, was_synth, note = validate_and_finalize_response(
                        last_query, final_content, final_reasoning, actions_taken)

                    # Ensure any file written or created during the agent session has a [DOWNLOAD: ...] badge
                    created_files = []
                    for act in actions_taken:
                        if act.get("name") in ("write_file", "edit_file") and act.get("ok"):
                            a_args = act.get("args") or {}
                            f_path = a_args.get("path") or a_args.get("file") or a_args.get("filename")
                            if f_path:
                                c_name = Path(f_path).name
                                if c_name not in created_files:
                                    created_files.append(c_name)

                    dl_badges = [f"[DOWNLOAD: {cf}]" for cf in created_files if f"[DOWNLOAD: {cf}]" not in val_text and f"download?path={cf}" not in val_text.lower()]
                    if dl_badges:
                        val_text = val_text.rstrip() + "\n\n" + "\n".join(dl_badges)
                        was_synth = True

                    if was_synth and val_text != final_content:
                        _red.reset()
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
                            yield f"event: permission_request\ndata: {json.dumps({'req_id': preq_id, 'cmd': cmd})}\n\n"
                            allowed, pnote = await _await_permission(preq_id, ev)
                            if not allowed:
                                result = f"error: user denied shell command: {cmd}" + (f" ({pnote})" if pnote else "")
                                yield f"event: tool_result\ndata: {json.dumps({'id': tc_id, 'name': name, 'ok': False, 'result': result})}\n\n"
                                actions_taken.append({"name": name, "args": args, "ok": False, "result": result})
                                msgs.append({"role": "tool", "tool_call_id": tc_id, "content": result})
                                continue
                        # approved via pattern or modal: tool skips its own gate for
                        # exactly this command line (a model-supplied flag can't)
                        mark_approved(cmd)

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

            _sem = await output_guard.semantic_check(final_content, user, _cloud_out)
            if _sem:
                _msg = _sem.get("message") or "Response filtered by policy."
                yield "event: delta_reset\ndata: {}\n\n"
                yield f"event: delta\ndata: {json.dumps({'text': _msg})}\n\n"
                yield f"event: guard\ndata: {json.dumps({'rule': _sem.get('name'), 'message': _msg})}\n\n"
                audit_log(user, action="output_guard.redact", resource=_sem.get("name"),
                          detail={"endpoint": "agent/run", "scope": _sem.get("scope"),
                                  "semantic": True}, result="deny")
                yield f"event: validated\ndata: {{}}\n\n"
                yield f"event: done\ndata: {json.dumps({'note': 'response filtered by policy', 'text': ''})}\n\n"
                return
            val_text, was_synth, note = validate_and_finalize_response(
                last_query, final_content, final_reasoning, actions_taken)
            created_files = []
            for act in actions_taken:
                if act.get("name") in ("write_file", "edit_file") and act.get("ok"):
                    a_args = act.get("args") or {}
                    f_path = a_args.get("path") or a_args.get("file") or a_args.get("filename")
                    if f_path:
                        c_name = Path(f_path).name
                        if c_name not in created_files:
                            created_files.append(c_name)

            dl_badges = [f"[DOWNLOAD: {cf}]" for cf in created_files if f"[DOWNLOAD: {cf}]" not in val_text and f"download?path={cf}" not in val_text.lower()]
            if dl_badges:
                val_text = val_text.rstrip() + "\n\n" + "\n".join(dl_badges)
                was_synth = True

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


def _resolve_requested_file(path: str, space: Optional[str] = None) -> Optional[Path]:
    import re as _re
    p = None
    clean_name = Path(path).name
    raw_stem = Path(clean_name).stem
    suffix = Path(clean_name).suffix.lower()
    has_uuid = bool(_re.search(r'[-_][0-9a-fA-F]{8}$', raw_stem))
    clean_stem = _re.sub(r'([-_][0-9a-fA-F]{8})+$', '', raw_stem)

    # 1. If explicit space requested or specific revision uuid requested, try direct resolution first
    if space == "common":
        try:
            cand = _common_resolve(path)
            if cand.is_file():
                p = cand
        except Exception:
            pass

    if p is None and has_uuid:
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

    # 2. Candidate match: if unversioned or not found, find all candidate revisions
    # and sort newest-first by modification time (so freshly generated files always take precedence over stale ones)
    if p is None or not p.is_file():
        try:
            cand_matches = []
            for ws_dir in (common_workspace(), active_workspace()):
                if ws_dir and ws_dir.is_dir():
                    if suffix:
                        cand_matches.extend([f for f in ws_dir.glob(f"{clean_stem}-*{suffix}") if f.is_file()])
                        cand_matches.extend([f for f in ws_dir.glob(f"{clean_stem}_*{suffix}") if f.is_file()])
                        exact_cand = ws_dir / f"{clean_stem}{suffix}"
                        if exact_cand.is_file():
                            cand_matches.append(exact_cand)
                        cand_matches.extend([f for f in ws_dir.glob(f"{clean_stem}*{suffix}") if f.is_file()])
                    cand_matches.extend([f for f in ws_dir.glob(f"{clean_stem}.*") if f.is_file()])
            if cand_matches:
                seen_paths = set()
                unique_cands = []
                for c in sorted(cand_matches, key=lambda f: f.stat().st_mtime, reverse=True):
                    resolved_c = str(c.resolve())
                    if resolved_c not in seen_paths:
                        seen_paths.add(resolved_c)
                        unique_cands.append(c)
                if unique_cands:
                    p = unique_cands[0]
        except Exception:
            pass

    # 3. Fallback recovery: check if the file was created or provided in the
    # requesting user's own recent session messages (never other users')
    uid = get_current_user_id()
    if (p is None or not p.is_file()) and uid is not None:
        try:
            from core.db import _projects_db
            rows = _projects_db.execute(
                "SELECT m.content FROM messages m JOIN sessions s ON s.id = m.session_id "
                "WHERE s.user_id = ? AND (m.content LIKE ? OR m.content LIKE ?) "
                "ORDER BY m.id DESC LIMIT 10",
                (uid, f"%{clean_name}%", f"%[DOWNLOAD: {clean_name}]%")
            ).fetchall()
            for r in rows:
                c_text = r["content"] or ""
                ext = clean_name.split('.')[-1].lower() if '.' in clean_name else ''
                cand_code = None

                if ext in {'xlsx', 'xls'}:
                    # Excel spreadsheet recovery: extract markdown table or code fence
                    tbl_m = _re.search(r'(\|.+?\|\n\|[\s\-:|]+\|\n(?:\|.+?\|\n?)+)', c_text)
                    if tbl_m:
                        cand_code = tbl_m.group(1).strip()
                    if not cand_code:
                        csv_m = _re.search(r'```(?:csv|tsv|excel)?\n([\s\S]+?)\n```', c_text, _re.IGNORECASE)
                        if csv_m:
                            cand_code = csv_m.group(1).strip()
                    res_str = tool_write_file_common({"path": clean_name, "content": cand_code or c_text})
                    m = _re.search(r"\[DOWNLOAD:\s*([^\]]+)\]", res_str or "")
                    real_saved = m.group(1).strip() if m else clean_name
                    p = common_workspace() / real_saved
                    break

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
                    title_clean = clean_stem.replace('_', ' ').replace('-', ' ').title()
                    # (no invented placeholder rows for CSV: a missing file stays a 404
                    # rather than being replaced by fabricated data)
                    if ext in {'html', 'htm'}:
                        cand_code = generate_fresh_dashboard_html(title_clean, c_text.split('[DOWNLOAD:')[0].strip())
                    elif ext == 'md':
                        cand_code = f"# {title_clean}\n\n{c_text}"

                if cand_code:
                    res_str = tool_write_file_common({"path": clean_name, "content": cand_code})
                    m = _re.search(r"\[DOWNLOAD:\s*([^\]]+)\]", res_str or "")
                    real_saved = m.group(1).strip() if m else clean_name
                    p = common_workspace() / real_saved
                    # Also write unversioned copy as alias
                    try:
                        (common_workspace() / clean_name).write_text(cand_code, encoding="utf-8")
                    except Exception:
                        pass
                    break
        except Exception:
            pass

    return p


@router.get("/agent/download")
@router.get("/download")
async def agent_download(path: str, space: Optional[str] = None):
    """Serve a file as a download attachment.

    Query param:  ?path=relative/path/to/file.xlsx
    Checks common space first (for chat mode), then active workspace (for project tasks).
    Sandbox-safe: resolves via _common_resolve and _ws_resolve to prevent path traversal.
    """
    p = _resolve_requested_file(path, space)
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
    p = _resolve_requested_file(path, space)
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
        "X-Content-Type-Options": "nosniff",
    }
    if suffix in {".html", ".htm", ".svg", ".xml"}:
        # Generated documents run in an opaque origin even when opened directly
        # in a tab: scripts work (charts), but never with this app's cookies/API.
        headers["Content-Security-Policy"] = "sandbox allow-scripts allow-forms allow-popups allow-modals"
    return FileResponse(str(p), media_type=mime, headers=headers)


async def _ws_tree_scan(rel_dir: str) -> list:
    """One level of the workspace tree from the agent tools module."""
    ignored = {".git", "__pycache__", "node_modules", ".venv", "venv", "_agent_run.py"}
    ws = active_workspace().resolve()

    uid = _remote_uid()
    if uid is not None:
        # workspace lives on the user's own machine -- ask the companion.
        # Use the project's registered workspace_dir directly (the path the user
        # picked on their local machine), NOT the server-resolved active_workspace()
        # which may be a server-local fallback path (e.g. C:\AI\workspace\user_1\proj)
        # that happens to exist on the client too but points to the wrong place.
        from core.request_context import get_current_device_id
        proj = get_active_project(uid, get_current_device_id())
        proj_dir = project_workspace_dir(proj, uid) if proj else None
        root_for_companion = str(proj_dir) if proj_dir else str(ws)
        data = await companion_bridge.call(uid, "fs.tree", {"root": root_for_companion, "rel": rel_dir or ""})
        out = []
        for n in (data.get("nodes") or []):
            if n.get("name") in ignored:
                continue
            rel = n.get("path", "")
            if n.get("dir"):
                out.append({"name": n["name"], "path": rel, "dir": True, "children": None})
            else:
                changed = any(k.replace("\\", "/").endswith("/" + rel) or
                              k.replace("\\", "/") == rel for k in _ws_changes.keys())
                out.append({"name": n["name"], "path": rel, "dir": False,
                            "size": n.get("size", 0), "changed": changed})
        return out

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
async def agent_ws_tree(path: str = "", user: Principal = Depends(get_current_user)):
    curr_proj = get_active_project(user.id)
    if not curr_proj or curr_proj in ("scratch", "default"):
        return JSONResponse({"error": "No project selected", "root": "", "project": None, "nodes": [], "changes": []})

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
    return {"root": ws.name, "project": curr_proj,
            "nodes": await _ws_tree_scan(path), "changes": changes}


@router.get("/agent/ws/file")
async def agent_ws_file(path: str, user: Principal = Depends(get_current_user)):
    curr_proj = get_active_project(user.id)
    if not curr_proj or curr_proj in ("scratch", "default"):
        return JSONResponse({"error": "No project selected"}, status_code=400)

    uid = _remote_uid()
    if uid is not None:
        p = _ws_resolve(path)
        data = await companion_bridge.call(uid, "fs.read", {"path": str(p)})
        content = data.get("content")
        if content is None:
            return JSONResponse({"error": f"file not found: {path}"}, status_code=404)
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
    cloud_model_override: Optional[str] = None


@router.post("/agent/vision")
async def agent_vision(req: VisionReq, user: Principal = Depends(get_current_user)):
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

    # ── Priority 1: Main Model if capable of vision ───────────────────────
    # If the main model (local with --mmproj) is running and vision-capable,
    # use it directly for vision tasks without spawning a secondary model.
    main_ready = (state.process is not None and state.process.poll() is None and state.client is not None)
    main_vision = main_ready and bool((state.profile or {}).get("vision_capable"))
    if main_vision:
        model_name = Path((state.profile or {}).get("model_path", "")).name or "Main LLM (vision)"
        model_name = model_name.replace(".gguf", "")
        rid = monitor_begin("agent/vision", False, body_bytes, model=model_name, source="local")
        t0 = time.time()
        try:
            r = await state.client.post("/v1/chat/completions", json=payload, timeout=None)
            state.last_activity = time.time()
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
                    "lane": "main", "model": model_name}
        except Exception as e:
            monitor_end(rid, 500, duration=time.time() - t0, source="local")
            print(f"[agent/vision] main model vision failed ({model_name}): {e} - falling back to vision model", file=sys.stderr)

    # ── Priority 2: Cloud Vision Lane (or override) ───────────────────────
    cm = None
    if req.cloud_model_override:
        cm = cloud.get_cloud(req.cloud_model_override, user.id)
    if not cm:
        cm = cloud.cloud_lane("vision", user.id)
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
            print(f"[agent/vision] cloud vision lane failed ({cm.key}): {e} - falling back to small vision model", file=sys.stderr)

    # ── Priority 3: Dedicated small vision model ───────────────────────────
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
async def unload_small_models_endpoint(user: Principal = Depends(require_permission("model.local.load"))):
    small_models.unload_all()
    return {"ok": True, "unloaded": True}




import asyncio
import json
import sys
import time
from pathlib import Path
from fastapi import Depends
from fastapi.responses import StreamingResponse, JSONResponse
from core.auth import Principal
from core.deps import get_current_user
from core.audit import audit_log
from core import input_guard
from core import mcp as mcp_core
from core import output_guard
from core import verifier
from core.sse import sse
from core.small_model import APP_CONFIG
from core import cloud
from core import context_budget
from core.state import state
from core.registry import registry
from core.web_tools import register_web_tools, tool_web_search, tool_web_fetch, tool_web_search_images
from core import web_search as web_search_mod
from core.agent_tools import tool_write_file_common, CHAT_WRITE_FILE_SCHEMA, _common_resolve
from core.request_context import run_in_executor_ctx, set_tool_allowlist, tool_allowed
from core.doc_tools import (DOC_EDIT_SCHEMA, DOC_INSPECT_SCHEMA, tool_doc_edit_common,
                            tool_doc_inspect_common)
from core.agent_loop import (
    run_tool,
    safe_parse_and_repair_args,
    _extract_text_tool_calls,
    estimate_prompt_tokens,
)
from core import prompt_scope
from core.monitor import (
    _monitor_state,
    monitor_begin,
    monitor_end,
)
from core.knowledge_access import allowed_source_ids_for, kb_local_only, set_kb_cloud_blocked
from core.db import db_record_request
from .. import common
from core import reasoning
from ..common import _llm_chat_stream
from ..common.sampling_extra import sampler_extra

from .base import router
from .delivery import (
    _WRAP_REFUSAL,
    _finalize_download_tags,
    _finalize_media,
    _prompt_tokens_of,
    _saved_filename,
    _wraps_media,
)
from .file_intent import (
    PRIOR_FILE_MAX_CHARS,
    WEB_TOOL_NAMES,
    _DL_TAG_RE,
    _DOC_EDIT_EXTS,
    file_followup_intent,
    load_prior_file,
    looks_undelivered,
    session_files,
    shrink_old_tool_results,
    wants_file_output,
)
from .models import ChatRunRequest


def _record_chat_usage(res_dict, sent_tokens, msgs, tools) -> None:
    """Feed one real usage count back into the shared budget accounting.

    Only server-reported counts may anchor the learner: a fallback guess would teach
    it fiction. record_usage re-checks this, but the call site must still pass the
    exact post-shrink msgs and tools of the request the count came from.
    """
    u = (res_dict or {}).get("usage") or {}
    if u.get("prompt_tokens"):
        context_budget.record_usage("main", sent_tokens, u["prompt_tokens"], msgs, tools)


@router.post("/chat/run")
async def chat_run(req: ChatRunRequest, user: Principal = Depends(get_current_user)):
    from core.agent_tools import set_current_user
    set_current_user(user.id)
    cloud_main = cloud.cloud_lane("main", user.id)
    main_ready = (state.process is not None and state.process.poll() is None and state.client is not None)
    if not main_ready and not cloud_main:
        target = state.profile_path or state.profile or common.initial_profile_path
        if target:
            try:
                print(f"[server_manager] chat/run: model not running — auto-starting...")
                await state.ensure_running(target)
                main_ready = (state.process is not None and state.process.poll() is None and state.client is not None)
            except Exception as e:
                print(f"[server_manager] auto-start main model failed: {e}", file=sys.stderr)
    if not main_ready and not cloud_main:
        return JSONResponse({"error": "No model loaded. Please load or start a model from the top toolbar first."}, status_code=400)
    main_client = cloud.CloudClient(cloud_main) if cloud_main else state.client

    web_cap_enabled = APP_CONFIG.get("capabilities", {}).get("web", False)
    use_web = req.web_search and web_cap_enabled

    msgs = [dict(m) for m in req.messages]
    last_query = ""
    for m in reversed(msgs):
        if m.get("role") == "user":
            last_query = str(m.get("content", ""))
            break

    # --- Input sanitizer (core/input_guard.py) -----------------------------
    # cloud_only rules only fire when the main lane is a cloud model; local
    # models are allowed. block_all rules fire regardless. Human-readable
    # rule message is surfaced to the UI's existing error bubble.
    _hit = await input_guard.check_async(
        input_guard.message_texts(msgs),
        user, any_cloud_lane=cloud_main is not None)
    if _hit:
        audit_log(user, action="input_guard.block", resource=_hit.get("name"),
                  detail={"scope": _hit.get("scope"), "endpoint": "chat/run",
                          "pattern": _hit.get("_matched_pattern")}, result="deny")
        return JSONResponse({"error": _hit.get("message")}, status_code=403)

    import re as _re
    url_matches = _re.findall(r'https?://[^\s<>")\]]+', last_query)

    prior_files = session_files(msgs)
    file_followup = file_followup_intent(last_query, bool(prior_files))
    if file_followup == "where":
        # "where is the file?" - answer with the existing link instead of
        # asking a slow local model to regenerate (and usually invent) it.
        latest = prior_files[-1]
        others = prior_files[:-1][-3:]
        reply = f"Here is the latest file created in this chat:\n\n[DOWNLOAD: {latest}]"
        if others:
            reply += "\n\nEarlier files from this chat:\n\n" + "\n".join(f"[DOWNLOAD: {f}]" for f in reversed(others))
        reply += ("\n\nIf you asked for changes after this file and they aren't in it, tell me what to "
                  "change and I'll save an updated version.")

        async def _where_sse():
            yield f"event: delta\ndata: {json.dumps({'text': reply})}\n\n"
            yield f"event: done\ndata: {json.dumps({'prompt_tokens': 0, 'completion_tokens': 0, 'total_tokens': 0})}\n\n"
        return StreamingResponse(_where_sse(), media_type="text/event-stream")

    # Prompt scope S1: intent first, manuals only on intent. wants_file is
    # reused below for the tool loop (same inputs, same answer).
    wants_file = wants_file_output(last_query) or file_followup == "edit"
    has_file = bool(file_followup or wants_file)
    has_web = bool(use_web and prompt_scope.web_intent(last_query, url_matches, msgs))

    # Tools for Chat Mode: write_file always (core), the rest on intent.
    chat_tools = [CHAT_WRITE_FILE_SCHEMA]
    if has_file:
        chat_tools += [DOC_INSPECT_SCHEMA, DOC_EDIT_SCHEMA]
    from core.agent_tools import AGENT_TOOLS
    skb = next((t for t in AGENT_TOOLS if t.get("function", {}).get("name") == "search_knowledge_base"), None)
    if skb:
        chat_tools.append(skb)
    if has_web:
        register_web_tools()
        for t_name in ("web_search", "web_fetch", "web_search_images"):
            rt = registry.get(t_name)
            if rt and rt.schema:
                chat_tools.append(rt.schema)
    # images made on this PC (stable-diffusion.cpp): the model may draw when asked.
    # A cloud-first route isn't offered here (chat has no ask-first dialog for cost);
    # /image and agent mode handle those.
    chat_media_tools = set()
    try:
        from core import media_tools
        img_schema = media_tools.chat_image_tool_schema()
        if img_schema:
            chat_tools.append(img_schema)
            chat_media_tools.add("generate_image")
    except Exception as e:
        print(f"[chat] image tool unavailable: {type(e).__name__}", file=sys.stderr)
    mcp_prompt = ""
    mcp_servers = set()
    if APP_CONFIG.get("capabilities", {}).get("mcp", False):
        from core.mcp.constants import configured_servers
        mcp_names = set(configured_servers(APP_CONFIG))
        # on-mention (S1): full schemas + prompt only for named servers.
        # A custom agent's explicit allowlist below merges in (admin config wins).
        mcp_servers = prompt_scope.mcp_mentions(last_query, mcp_names, msgs)
        if mcp_servers:
            chat_tools.extend(prompt_scope.mcp_schemas_for(
                mcp_core.ready_tool_schemas(), mcp_servers))
            mcp_prompt = mcp_core.chat_prompt(only_servers=mcp_servers)

    custom_agent = None
    allowed_names = None
    if req.custom_agent_id:
        from core.auth_db import db_get_custom_agent
        custom_agent = db_get_custom_agent(req.custom_agent_id, user.id)
        if not custom_agent:
            return JSONResponse({"error": "custom_agent_not_found",
                                 "message": "The selected custom agent no longer exists or isn't shared with you."},
                                status_code=404)
        # the client sends null for values the user didn't change after picking the agent
        if req.temperature is None and custom_agent.get("temperature") is not None:
            req.temperature = float(custom_agent["temperature"])
        if not req.reasoning_effort and custom_agent.get("reasoning_effort"):
            req.reasoning_effort = custom_agent["reasoning_effort"]
        if custom_agent.get("tool_allowlist"):
            allowed_names = set(custom_agent["tool_allowlist"]) | {"write_file"}   # saving files is never withheld
            chat_tools = [t for t in chat_tools if t.get("function", {}).get("name") in allowed_names]
            # only advertise the MCP servers that still have an allowed tool;
            # explicit admin config wins over mention-gating (S1)
            allowed_srv = {n.split("__")[1] for n in allowed_names if n.startswith("mcp__") and n.count("__") >= 2}
            mcp_servers = set(allowed_srv)
            mcp_prompt = mcp_core.chat_prompt(only_servers=mcp_servers) if mcp_servers else ""
        from routes.custom_agents import apply_input_template
        apply_input_template(msgs, custom_agent)
    if req.temperature is None:
        req.temperature = 0.7
    # enforced again at dispatch (core.agent_loop.run_tool and the direct calls below)
    set_tool_allowlist(allowed_names)

    sys_parts = [common.current_date_prompt()]
    sys_labels = ["date"]   # parallel to sys_parts: P0 breakdown ruler (S5)
    if custom_agent:
        ca_prompt = (
            f"YOU ARE A SPECIALIZED CUSTOM AGENT: {custom_agent['name']} ({custom_agent.get('icon', '🤖')})\n"
            f"AGENT PERSONA & DIRECTIVES:\n{custom_agent.get('system_prompt', '')}\n"
        )
        sys_parts.append(ca_prompt)
        sys_labels.append("custom-agent")
    if req.system_prompt and req.system_prompt.strip():
        sys_parts.append(req.system_prompt.strip())
        sys_labels.append("request-system")
    if mcp_prompt:
        sys_parts.append(mcp_prompt)
        sys_labels.append("mcp")

    # Organizational knowledge base: permission-scoped routing & retrieval.
    # Injected only for company-directed queries or strongly matching chunks -
    # otherwise unrelated KB text gets mixed into general answers. The model
    # can still call search_knowledge_base explicitly.
    kb_ids = allowed_source_ids_for(user)
    kb_used = False
    kb_hits = []
    kb_prompt_block = ""
    kb_blocked_reason = ""
    from core.knowledge_router import kb_routing_query
    kb_query = kb_routing_query(msgs, last_query)
    if kb_ids and kb_query.strip() and not file_followup:
        try:
            from core.knowledge_router import fetch_company_knowledge, is_company_or_kb_query
            kb_hits, kb_prompt_block = await fetch_company_knowledge(kb_query, kb_ids, k=6)
            if kb_hits:
                auto_cos = float((APP_CONFIG.get("knowledge") or {}).get("auto_inject_cos", 0.55))
                top_cos = max(float(h.get("cos") or 0.0) for h in kb_hits)
                if not (is_company_or_kb_query(kb_query, kb_ids) or top_cos >= auto_cos):
                    kb_hits, kb_prompt_block = [], ""
            if kb_hits:
                kb_used = True
                sys_parts.append(kb_prompt_block)
                sys_labels.append("kb")
        except Exception as e:
            print(f"[chat] knowledge retrieval failed: {e}", file=sys.stderr)

    # Data residency (core/knowledge_access.py): internal knowledge never goes
    # to a cloud provider. A KB question from a cloud-bound user is answered by
    # the local model instead; if none can run, the KB context is withheld.
    if cloud_main and kb_local_only():
        if kb_hits:
            target = state.profile_path or state.profile or common.initial_profile_path
            local_err = "" if target else "no local model profile is configured"
            try:
                if target:
                    await state.ensure_running(target)
            except Exception as e:
                local_err = str(e).strip().splitlines()[0][:160] if str(e).strip() else type(e).__name__
                print(f"[chat] local model for knowledge query unavailable: {e}", file=sys.stderr)
            if state.is_running():
                cloud_main = None
                main_client = state.client
                audit_log(user, action="knowledge.local_only", resource="chat/run",
                          detail={"reason": "kb hits on a cloud-bound main lane", "hits": len(kb_hits)})
            else:
                sys_parts.remove(kb_prompt_block)
                kb_hits, kb_used = [], False
                kb_blocked_reason = ("Company knowledge base is local-only and this chat uses a cloud model. "
                                     f"The local model could not start ({local_err or 'unknown error'}). "
                                     "Start a local model or switch the main lane to local, then ask again.")
                audit_log(user, action="knowledge.blocked_cloud", resource="chat/run",
                          detail={"reason": local_err or "local model not running"}, result="deny")
                sys_parts.append("NOTE: The company knowledge base is restricted to local models and no "
                                 "local model is loaded, so internal company data is not available for "
                                 "this answer. Tell the user this instead of guessing.")
                sys_labels.append("kb-note")
        if cloud_main and skb in chat_tools:
            chat_tools.remove(skb)
    set_kb_cloud_blocked(cloud_main is not None)

    # S1: the full manual only on file intent, else a lean pointer (S4).
    if has_file:
        sys_parts.append(prompt_scope.FILE_MANUAL)
        sys_labels.append("file-manual")
    else:
        sys_parts.append(prompt_scope.FILE_MANUAL_LEAN)
        sys_labels.append("file-lean")

    if prior_files:
        latest = prior_files[-1]
        prior_block = (
            "FILES ALREADY CREATED IN THIS CONVERSATION (oldest first): " + ", ".join(prior_files[-5:]) + "\n"
            f"The most recent file is `{latest}`. If the user asks where a file is, give its "
            f"[DOWNLOAD: filename] link - do not regenerate it.")
        if file_followup == "edit" and Path(latest).suffix.lower() in _DOC_EDIT_EXTS:
            outline = await run_in_executor_ctx(tool_doc_inspect_common, {"file": latest})
            if cloud_main and await input_guard.check_async([outline], user, any_cloud_lane=True):
                outline = None
            if outline and not outline.startswith("doc error"):
                prior_block += (
                    f"\n\nThe user wants changes to `{latest}`. Do NOT regenerate it and do NOT call write_file. "
                    f"Call doc_edit(file='{latest}', ops=[...]) with ops that target ONLY the parts the user "
                    "asked to change; every other slide, cell, paragraph, style and layout is kept exactly. "
                    "Its current outline (with the addresses ops use) is below. Then give the user the "
                    "[DOWNLOAD: ...] link from the doc_edit result and list what changed.\n"
                    f"----- OUTLINE {latest} -----\n{outline}\n----- END OUTLINE -----")
            else:
                prior_block += (f"\n\nThe user wants changes to `{latest}`. Call doc_inspect(file='{latest}') "
                                "to get its outline, then doc_edit with ops for only the requested changes.")
        elif file_followup == "edit":
            ctx_chars = int(common.main_ctx_tokens(cloud_main) * 0.25 * 3.5)
            prior_text = load_prior_file(latest, min(PRIOR_FILE_MAX_CHARS, ctx_chars))
            # cloud lane: the file may carry KB-derived data - apply the same
            # input sanitizer the user's own messages go through before sending
            if prior_text and cloud_main and await input_guard.check_async([prior_text], user, any_cloud_lane=True):
                prior_text = None
            if prior_text:
                prior_block += (
                    f"\n\nThe user wants changes to `{latest}`. Its CURRENT content is below. Start from this "
                    "exact content - keep everything the user did not ask to change - and call "
                    f"write_file(path='{latest}', content=<the complete updated file>). Never rebuild it from "
                    "memory and never reply with only a description of the changes.\n"
                    f"----- BEGIN {latest} -----\n{prior_text}\n----- END {latest} -----")
            else:
                prior_block += (
                    f"\n\nThe user wants changes to `{latest}`, but its content can't be shown here (binary "
                    "format or too large). Recreate it with the requested changes using what you know from "
                    "this conversation, call write_file with the complete file, and tell the user it was "
                    "regenerated.")
        sys_parts.append(prior_block)
        sys_labels.append("prior-files")

    # S1: the full web manual + schemas only on web intent; otherwise the
    # model answers from knowledge (old rule 5) and spends nothing on browsing.
    if has_web:
        sys_parts.append(prompt_scope.WEB_MANUAL)
        sys_labels.append("web-manual")

    # Tool-loop budget: normal vs deep ("think") mode, from app.json "chat"
    chat_cfg = APP_CONFIG.get("chat") or {}
    deep = bool(req.deep_mode)
    max_turns = int(chat_cfg.get("deep_max_tool_rounds" if deep else "max_tool_rounds", 25 if deep else 15)) if chat_tools else 1
    max_web_calls = int(chat_cfg.get("deep_max_web_calls" if deep else "max_web_calls", 16 if deep else 8))
    if deep:
        sys_parts.append(
            "DEEP RESEARCH MODE:\n"
            "1. First, briefly list the sub-questions you need answered to fully satisfy the request.\n"
            f"2. Research each one with targeted web searches (use the current year; you have up to {max_web_calls} web calls).\n"
            "3. Stop searching as soon as you have enough data, then write the complete deliverable in one go. "
            "Cite sources, and label any figure you could not verify as an estimate.")
        sys_labels.append("deep")
    # How much the model reasons: the composer's effort level, independent of
    # Deep research; mapped per local/cloud target in common._llm_chat_stream_raw
    effort = reasoning.resolve(req.reasoning_effort, deep)

    combined_sys = "\n\n".join(sys_parts).strip()
    if combined_sys:
        has_sys = False
        for m in msgs:
            if m.get("role") == "system":
                has_sys = True
                m["content"] = combined_sys + "\n\n" + (m.get("content") or "").strip()
                break
        if not has_sys:
            msgs.insert(0, {"role": "system", "content": combined_sys})

    # P0 ruler (S5): per-section sizes, only with PROMPT_BREAKDOWN=1 or
    # app.json debug.prompt_breakdown. Never logs user text.
    prompt_scope.maybe_log("chat/run", list(zip(sys_labels, sys_parts)), chat_tools,
                           extra=f"hist={len(msgs)}")

    # Report the model actually answering: cloud model when the main lane is
    # cloud-bound, else the loaded local gguf (fallback label matches the old
    # "Main LLM" placeholder for a lane with nothing loaded yet).
    if cloud_main:
        clean_model_name = cloud_main.model_id
        model_display = cloud_main.display
        model_source = "cloud"
        model_provider = cloud_main.provider_name
    else:
        m_name = state.profile.get("model_path", "") if state.profile else ""
        clean_model_name = Path(m_name).name.replace(".gguf", "") if m_name else "Main LLM"
        model_display = f"\U0001F9E0 {clean_model_name}"
        model_source = "local"
        model_provider = None

    async def event_stream():
        nonlocal clean_model_name, model_display, model_source, model_provider
        lane_info = {"lane": "main", "model": clean_model_name, "display": model_display,
                     "source": model_source, "provider": model_provider}
        yield f"event: lane\ndata: {json.dumps(lane_info)}\n\n"
        if custom_agent:
            yield f"event: custom_agent\ndata: {json.dumps({'id': custom_agent['id'], 'name': custom_agent['name'], 'icon': custom_agent.get('icon', '🤖')})}\n\n"
        if kb_blocked_reason:
            yield f"event: kb_blocked\ndata: {json.dumps({'message': kb_blocked_reason})}\n\n"
        chat_rid = monitor_begin("chat/run", True, n_msgs=len(msgs),
                                 model=clean_model_name, source=model_source, provider=model_provider)
        t0 = time.time()
        turn_limit = max_turns       # grows by one if a continuation nudge needs it
        web_calls = 0
        research_nudged = False
        continued = False
        finish_reason = None
        written_files = []
        made_media = []              # markdown of pictures/videos generate_image made this turn
        final_prompt_toks = None     # same figure goes to the done event and the monitor
        # Budget from the shared accounting, not a hardcoded fraction: the probed window
        # when the server reports one (falling back to config), tightened by the learned
        # estimator error. The old `* 0.7` ignored the margin config, the safety factor,
        # and cloud cost caps - the three things context_budget exists to enforce.
        ctx_window = await context_budget.probe_window("main", main_client)
        if not ctx_window:
            ctx_window = int(common.main_ctx_tokens(cloud_main))
        ctx_budget = context_budget.budget_for("main", ctx_window, cloud=bool(cloud_main))
        # --- Output sanitizer: redact model deltas before they reach the
        # client (cloud_only rules only fire while the lane is cloud; the
        # fallback event recreates the redactor for the local lane).
        redactor = output_guard.OutputRedactor(user, model_source == "cloud")
        guard_event_sent = False

        def _guard_notice():
            nonlocal guard_event_sent
            if redactor.matched and not guard_event_sent:
                guard_event_sent = True
                return (f"event: guard\ndata: "
                        + json.dumps({"rule": redactor.matched.get("name"),
                                      "message": redactor.matched.get("message")}) + "\n\n")
            return ""

        try:
            turn = -1
            while True:
                turn += 1
                if turn >= turn_limit:
                    break
                # Turn 0 proactive URL fetch: if user specifically asks to summarize or inspect a URL
                if turn == 0 and use_web and chat_tools and url_matches and tool_allowed("web_fetch"):
                    is_fetch_intent = any(w in last_query.lower() for w in (
                        "summarise", "summarize", "read", "fetch", "check", "browse", "what is on",
                        "site", "website", "page", "link", "article", "look at", "review", "tell me about"
                    )) or len(last_query.strip()) <= len(url_matches[0]) + 15
                    if is_fetch_intent:
                        target_url = url_matches[0]
                        tc_id = "fetch_0"
                        yield sse("tool_call", {'id': tc_id, 'name': 'web_fetch', 'args': {'url': target_url}})
                        res_str = await tool_web_fetch({"url": target_url})
                        ok = not (isinstance(res_str, str) and (res_str.startswith("error:") or res_str.startswith("File not found")))
                        yield sse("tool_result", {'id': tc_id, 'name': 'web_fetch', 'ok': ok, 'result': res_str})
                        msgs.append({
                            "role": "assistant",
                            "content": f"I will fetch and read the content from {target_url}.",
                            "tool_calls": [{"id": tc_id, "type": "function", "function": {"name": "web_fetch", "arguments": json.dumps({"url": target_url})}}]
                        })
                        msgs.append({"role": "tool", "tool_call_id": tc_id, "content": res_str})
                        continue

                # Turn 0 proactive Knowledge Base routing: if user query routed to company knowledge base
                if turn == 0 and kb_used and kb_hits:
                    tc_id = "kb_fetch_0"
                    source_names = ", ".join(sorted(set(h.get("title", "") for h in kb_hits)))
                    yield sse("tool_call", {'id': tc_id, 'name': 'search_knowledge_base', 'args': {'query': last_query}})
                    yield sse("tool_result", {'id': tc_id, 'name': 'search_knowledge_base', 'ok': True, 'result': f'Retrieved {len(kb_hits)} records from company knowledge base ({source_names})'})

                res_dict = None
                streamed_content = []

                # Research budget: once web calls are used up (or only the last two
                # turns remain) drop the web tools but keep write_file, so the
                # remaining turns go to producing the answer instead of more searches.
                research_over = use_web and web_calls > 0 and (
                    web_calls >= max_web_calls or turn >= turn_limit - 2)
                if turn == turn_limit - 1 and msgs and msgs[-1].get("role") == "tool":
                    # final turn: must answer now; a requested file can still be written
                    current_tools = [CHAT_WRITE_FILE_SCHEMA] if (wants_file and tool_allowed("write_file")) else None
                elif research_over:
                    current_tools = [t for t in chat_tools
                                     if t.get("function", {}).get("name") not in WEB_TOOL_NAMES]
                else:
                    current_tools = chat_tools
                if research_over and not research_nudged:
                    research_nudged = True
                    msgs.append({"role": "user", "content": (
                        "[system note] The research phase is over - do not search any more. Using the "
                        "results above, produce the complete answer now"
                        + (" and call write_file with the full file content." if wants_file else ".")
                        + " Do not announce what you will do; just do it.")})

                shrink_old_tool_results(msgs, ctx_budget, tools=current_tools)

                if cloud_main and cloud.cloud_bindings(user.id).get("fallback_local", True):
                    fb_local = None
                    target = state.profile_path or state.profile or common.initial_profile_path
                    if target:
                        try:
                            if state.process is None or state.process.poll() is not None:
                                await state.ensure_running(target)
                            fb_local = state.client
                        except Exception as e:
                            print(f"[chat] local fallback unavailable: {e}", file=sys.stderr)
                    chat_rep = req.repeat_penalty if req.repeat_penalty is not None else 1.15
                    chat_stream = common._llm_chat_stream_with_fallback(
                        main_client, fb_local, msgs, current_tools, req.temperature,
                        req.max_tokens, repeat_penalty=chat_rep, rid=chat_rid, lane="main", effort=effort,
                        top_p=req.top_p, min_p=req.min_p, presence_penalty=req.presence_penalty, top_k=req.top_k, extra=sampler_extra(req))
                else:
                    chat_rep = req.repeat_penalty if req.repeat_penalty is not None else 1.15
                    chat_stream = _llm_chat_stream(main_client, msgs, tools=current_tools, temperature=req.temperature,
                                                   max_tokens=req.max_tokens, repeat_penalty=chat_rep, rid=chat_rid, effort=effort,
                                                   top_p=req.top_p, min_p=req.min_p, presence_penalty=req.presence_penalty, top_k=req.top_k, extra=sampler_extra(req))
                # Estimate of what this turn actually sends, for anchoring below. Must be
                # taken post-shrink with the exact tools of this turn - an estimate from
                # before compaction, or without the schemas, would teach the budget learner
                # the wrong ratio.
                sent_tokens = context_budget.prompt_tokens_for("main", msgs, current_tools)
                async for ev, val in chat_stream:
                    if ev == "queued":
                        yield f"event: queued\ndata: {json.dumps(val)}\n\n"
                        continue
                    if ev == "fallback":
                        fb_name = state.profile.get("model_path", "") if state.profile else ""
                        clean_model_name = Path(fb_name).name.replace(".gguf", "") if fb_name else "Main LLM"
                        model_display = f"\U0001F9E0 {clean_model_name}"
                        model_source, model_provider = "local", None
                        fb_lane_info = {"lane": "main", "model": clean_model_name, "display": model_display,
                                        "source": model_source, "provider": model_provider}
                        yield f"event: lane\ndata: {json.dumps(fb_lane_info)}\n\n"
                        # lane switched cloud -> local: drain the old redactor's
                        # holdback and rebuild with the local flag
                        _tail = redactor.flush()
                        if _tail:
                            streamed_content.append(_tail)
                            yield f"event: delta\ndata: {json.dumps({'text': _tail})}\n\n"
                        redactor = output_guard.OutputRedactor(user, False)
                        continue
                    if ev == "thought_delta":
                        yield f"event: thought_delta\ndata: {json.dumps({'delta': val})}\n\n"
                    elif ev == "content_to_thought":
                        # forced-open <think>: the text streamed as the answer was reasoning
                        redactor.reset()
                        streamed_content.clear()
                        yield "event: delta_to_thought\ndata: {}\n\n"
                    elif ev == "content_delta":
                        _safe = redactor.feed(val)
                        streamed_content.append(_safe)
                        if _safe:
                            yield f"event: delta\ndata: {json.dumps({'text': _safe})}\n\n"
                        _n = _guard_notice()
                        if _n:
                            yield _n
                    elif ev == "tool_preparing":
                        yield sse("tool_preparing", val)
                    elif ev == "result":
                        res_dict = val

                # end of this turn's stream: release the redactor holdback so
                # the final chars of the answer are shown too
                _tail = redactor.flush()
                if _tail:
                    streamed_content.append(_tail)
                    yield f"event: delta\ndata: {json.dumps({'text': _tail})}\n\n"
                _n = _guard_notice()
                if _n:
                    yield _n

                # Semantic (natural-language) output rules: evaluated on the
                # completed turn. On match, replace the whole answer with the
                # rule's human-readable message (streamed text can't be unsent,
                # but delta_reset makes the UI drop it from the visible reply).
                _sem = await output_guard.semantic_check(
                    res_dict.get("content", "") if res_dict else "".join(streamed_content),
                    user, model_source == "cloud")
                if _sem:
                    if streamed_content:
                        redactor.reset()
                        yield "event: delta_reset\ndata: {}\n\n"
                    _msg = _sem.get("message") or "Response filtered by policy."
                    yield f"event: delta\ndata: {json.dumps({'text': _msg})}\n\n"
                    yield f"event: guard\ndata: {json.dumps({'rule': _sem.get('name'), 'message': _msg})}\n\n"
                    audit_log(user, action="output_guard.redact", resource=_sem.get("name"),
                              detail={"endpoint": "chat/run", "scope": _sem.get("scope"),
                                      "semantic": True}, result="deny")
                    yield f"event: done\ndata: {{}}\n\n"
                    return

                content = res_dict.get("content", "") if res_dict else "".join(streamed_content)
                reasoning = res_dict.get("reasoning", "") if res_dict else ""
                tool_calls = res_dict.get("tool_calls", []) if res_dict else []
                finish_reason = res_dict.get("finish_reason") if res_dict else None
                if finish_reason == "length":
                    print(f"[chat] turn {turn}: generation hit the token/context limit "
                          f"(max_tokens={req.max_tokens}, ctx_budget={ctx_budget})", file=sys.stderr)

                # Fallback: check if text tool calls were emitted - in the reply, or
                # inside the reasoning when the model wrote the call while "thinking"
                # and only a preamble ("Let me compile...") reached the content
                if not tool_calls:
                    parsed_tc = _extract_text_tool_calls(content) if content else []
                    if not parsed_tc and reasoning:
                        parsed_tc = _extract_text_tool_calls(reasoning)
                    if parsed_tc:
                        tool_calls = parsed_tc

                # Auto-recovery 1: if model emitted a refusal saying it can't browse the internet
                is_refusal = any(w in content.lower() for w in (
                    "i can't browse the live internet", "i cannot browse the live internet",
                    "i can't browse", "i cannot browse", "i don't have internet", "i lack internet",
                    "as an ai, i cannot access", "as an ai, i can't access", "i can't fetch the exact current content"
                ))
                if is_refusal and turn == 0 and chat_tools and tool_allowed("web_fetch" if url_matches else "web_search"):
                    redactor.reset()
                    yield "event: delta_reset\ndata: {}\n\n"
                    if url_matches:
                        target_url = url_matches[0]
                        tc_id = "recov_fetch_0"
                        yield sse("tool_call", {'id': tc_id, 'name': 'web_fetch', 'args': {'url': target_url}})
                        res_str = await tool_web_fetch({"url": target_url})
                        ok = not (isinstance(res_str, str) and (res_str.startswith("error:") or res_str.startswith("File not found")))
                        yield sse("tool_result", {'id': tc_id, 'name': 'web_fetch', 'ok': ok, 'result': res_str})
                        msgs.append({"role": "assistant", "content": f"I will fetch the contents of {target_url}.", "tool_calls": [{"id": tc_id, "type": "function", "function": {"name": "web_fetch", "arguments": json.dumps({"url": target_url})}}]})
                        msgs.append({"role": "tool", "tool_call_id": tc_id, "content": res_str})
                        continue
                    else:
                        q_clean = _re.sub(r'^(search|google|find|look up|what is|who is)\s+(for\s+)?', '', last_query, flags=_re.IGNORECASE).strip()
                        # the small executor model turns the chatty message into keywords (if it's running)
                        q_search = await web_search_mod.rewrite_query(last_query, q_clean or last_query)
                        tc_id = "recov_search_0"
                        yield sse("tool_call", {'id': tc_id, 'name': 'web_search', 'args': {'query': q_search}})
                        res_str = await tool_web_search({"query": q_search})
                        ok = not (isinstance(res_str, str) and (res_str.startswith("error:") or res_str.startswith("File not found")))
                        yield sse("tool_result", {'id': tc_id, 'name': 'web_search', 'ok': ok, 'result': res_str})
                        msgs.append({"role": "assistant", "content": f"I will search the web for {q_search}.", "tool_calls": [{"id": tc_id, "type": "function", "function": {"name": "web_search", "arguments": json.dumps({"query": q_search})}}]})
                        msgs.append({"role": "tool", "tool_call_id": tc_id, "content": res_str})
                        continue

                # Auto-recovery 2: if model emitted a refusal regarding local file access or emitted data without calling write_file
                is_file_refusal = any(w in content.lower() for w in (
                    "i don't have the ability to directly edit or open local files",
                    "i don't have the ability to directly edit",
                    "cannot directly edit or open local files",
                    "cannot directly edit or open",
                    "i lack the ability to directly edit",
                    "cannot access your local files",
                    "cannot save files to your computer",
                    "as an ai, i cannot create files",
                ))
                # Require an actual file-creation verb close to an actual file-format
                # noun (not just "file" on its own, which co-occurs with ordinary
                # questions like "what's wrong in this csv"), and never fire on a
                # turn that only answered from the knowledge base - that's a Q&A
                # reply citing KB text, not a file-creation request.
                _file_verb_re = r'(fill|write|save|create|generate|make|export|download|share)\w*'
                _file_noun_re = r'(excel|spreadsheet|workbook|csv|\.xlsx|\.xls|\.csv|\.json|\.py|\.html|\.txt|\.docx|\.pptx|\.ppt|\.pdf|pdf|presentation|slides|deck)\b'
                is_file_intent = (not kb_used) and bool(
                    _re.search(_file_verb_re + r'.{0,25}' + _file_noun_re, last_query, _re.IGNORECASE)
                    or _re.search(_file_noun_re + r'.{0,25}' + _file_verb_re, last_query, _re.IGNORECASE)
                )

                # Check if model output contains [DOWNLOAD: ...] or code blocks intended as files
                dl_tags = _re.findall(r'\[DOWNLOAD:\s*([^\]]+)\]', content)
                missing_dl = []   # tags the model wrote without producing any content
                for dl_f in dl_tags:
                    clean_fname = Path(dl_f.strip()).name
                    try:
                        already_saved = _common_resolve(clean_fname).is_file()
                    except PermissionError:
                        already_saved = False
                    # a tag for a file that already exists is a link to it, never a
                    # cue to regenerate it from the reply text
                    if _wraps_media(made_media, clean_fname, last_query) and not already_saved:
                        content = _re.sub(rf'[ \t]*\[DOWNLOAD:\s*{_re.escape(dl_f)}\][ \t]*', '', content)
                        yield f"event: delta_replace\ndata: {json.dumps({'text': content})}\n\n"
                        continue
                    if clean_fname not in written_files and not already_saved:
                        # Extract matching code fence or full content to save to common storage
                        cand_code = None
                        ext = clean_fname.split('.')[-1].lower() if '.' in clean_fname else ''
                        if ext:
                            # Try finding fenced block with this language
                            fence_m = _re.search(rf'```{ext}\b[^\n]*\n([\s\S]+?)\n```', content, _re.IGNORECASE)
                            if fence_m:
                                cand_code = fence_m.group(1).strip()
                        if not cand_code:
                            # Try any fenced block in the message
                            any_fence = _re.search(r'```[^\n]*\n([\s\S]+?)\n```', content)
                            if any_fence:
                                cand_code = any_fence.group(1).strip()
                        if not cand_code and ext in {'html', 'htm', 'xml', 'svg'}:
                            html_tag_m = _re.search(r'(<!DOCTYPE\s+html[\s\S]*?</html>|<html[\s\S]*?</html>|<svg[\s\S]*?</svg>)', content, _re.IGNORECASE)
                            if html_tag_m:
                                cand_code = html_tag_m.group(1).strip()
                        if not cand_code and ext in {'xlsx', 'xls', 'csv'}:
                            # The model may have described the data as a markdown table
                            # instead of a fenced block - pull that out before falling
                            # back to placeholder rows.
                            tbl_m = _re.search(r'(\|.+?\|\n\|[\s\-:|]+\|\n(?:\|.+?\|\n?)+)', content)
                            if tbl_m:
                                cand_code = tbl_m.group(1).strip()
                        if not cand_code:
                            # The model wrote [DOWNLOAD: x] but never produced the content for
                            # it. Never invent a document: route it to missing_dl, which nudges
                            # the model to output the real thing and, failing that, drops the
                            # dead link. (no invented placeholder rows for csv/xlsx; no canned
                            # agenda deck for pptx; no PDF built out of the chat reply; no canned
                            # HTML dashboard - all of those carried invented content before)
                            missing_dl.append(clean_fname)

                        if cand_code:
                            res_str = (tool_write_file_common({"path": clean_fname, "content": cand_code})
                                       if tool_allowed("write_file") else "error: tool 'write_file' is not enabled for the active custom agent")
                            if res_str.startswith("error:"):
                                # the write failed: keep the tag as a dead link so the
                                # missing_dl pass below drops it instead of advertising it
                                missing_dl.append(clean_fname)
                            else:
                                real_saved = _saved_filename(res_str, clean_fname)
                                written_files.append(real_saved)
                                if real_saved != clean_fname and content:
                                    content = _re.sub(rf'\[DOWNLOAD:\s*{_re.escape(clean_fname)}\]', f'[DOWNLOAD: {real_saved}]', content)
                                    yield f"event: delta_replace\ndata: {json.dumps({'text': content})}\n\n"


                if (is_file_refusal or is_file_intent) and turn == 0 and not tool_calls:

                    table_match = _re.search(r'(\|.+?\|\n\|[\s\-:|]+\|\n(?:\|.+?\|\n?)+)', content)
                    code_match = _re.search(r'```(?:[a-zA-Z0-9_\-]+)?\n([\s\S]+?)\n```', content)

                    data_to_save = None
                    if table_match:
                        data_to_save = table_match.group(1).strip()
                    elif code_match:
                        data_to_save = code_match.group(1).strip()
                    elif "|" in content and content.count("\n") >= 2:
                        data_to_save = content.strip()

                    if data_to_save:
                        fname_cand = None
                        f_matches = _re.findall(r'[\w\-.]+\.(?:xlsx|xls|csv|json|py|html|txt|md|pdf|pptx|ppt)', last_query, _re.IGNORECASE)
                        if f_matches:
                            fname_cand = f_matches[0]
                        if not fname_cand:
                            c_matches = _re.findall(r'[\w\-.]+\.(?:xlsx|xls|csv|json|py|html|txt|md|pdf|pptx|ppt)', content, _re.IGNORECASE)
                            if c_matches:
                                fname_cand = c_matches[0]
                        if not fname_cand:
                            lq_low = last_query.lower()
                            c_low = content.lower()
                            if "pdf" in lq_low or "pdf" in c_low:
                                fname_cand = "document.pdf"
                            elif "ppt" in lq_low or "slide" in lq_low or "presentation" in lq_low:
                                fname_cand = "presentation.pptx"
                            elif "excel" in lq_low or "excel" in c_low:
                                fname_cand = "data.xlsx"
                            else:
                                fname_cand = "data.csv"

                        # tool_write_file_common() always concatenates a unique id onto
                        # this base name before saving, so every generation lands on its
                        # own file - the name picked here is only a human-readable stem.
                        target_filename = Path(fname_cand).name
                        tc_id = "recov_write_0"
                        redactor.reset()
                        yield "event: delta_reset\ndata: {}\n\n"
                        yield sse("tool_call", {'id': tc_id, 'name': 'write_file', 'args': {'path': target_filename, 'content': data_to_save}})
                        res_str = (tool_write_file_common({"path": target_filename, "content": data_to_save})
                                   if tool_allowed("write_file") else "error: tool 'write_file' is not enabled for the active custom agent")
                        ok = not res_str.startswith("error:")
                        target_filename = _saved_filename(res_str, target_filename)
                        yield sse("tool_result", {'id': tc_id, 'name': 'write_file', 'ok': ok, 'result': res_str})

                        if ok:
                            written_files.append(target_filename)
                            final_msg = f"I have filled and saved the data to **{target_filename}** in common storage.\n\n[DOWNLOAD: {target_filename}]"
                            if table_match:
                                final_msg += f"\n\nHere is a preview of the saved rows:\n\n{table_match.group(1)}"
                        else:
                            # never claim a save that did not happen, and never advertise a
                            # [DOWNLOAD:] link to a file that is not on disk
                            final_msg = (f"I could not save that as a file - **nothing was written**.\n\n"
                                         f"{res_str}\n\nAsk me to produce the rows or the code "
                                         f"explicitly and I will try again.")
                        yield f"event: delta\ndata: {json.dumps({'text': final_msg})}\n\n"
                        content = final_msg

                        msgs.append({
                            "role": "assistant",
                            "content": final_msg,
                            "tool_calls": [{"id": tc_id, "type": "function", "function": {"name": "write_file", "arguments": json.dumps({"path": target_filename, "content": data_to_save})}}]
                        })
                        msgs.append({"role": "tool", "tool_call_id": tc_id, "content": res_str})

                        gen_toks = max(1, round(len(content) / 3.5))
                        prompt_toks = final_prompt_toks = _prompt_tokens_of(None, msgs, current_tools)
                        _record_chat_usage(None, sent_tokens, msgs, current_tools)
                        yield f"event: done\ndata: {json.dumps({'prompt_tokens': prompt_toks, 'completion_tokens': gen_toks, 'total_tokens': prompt_toks + gen_toks})}\n\n"
                        return

                # Announced-but-not-delivered: "Let me compile the HTML now:" and
                # stop. Nudge once to actually produce it (granting one extra turn
                # if the budget is spent) instead of accepting the preamble.
                if (not tool_calls and chat_tools and not continued
                        and (missing_dl or looks_undelivered(content, wants_file, bool(written_files)))):
                    continued = True
                    if turn >= turn_limit - 1:
                        turn_limit += 1
                    if streamed_content:
                        redactor.reset()
                        yield "event: delta_reset\ndata: {}\n\n"
                    msgs.append({"role": "assistant", "content": content or ""})
                    msgs.append({"role": "user", "content": (
                        "[system note] You announced the deliverable but did not produce it. Output it now, "
                        "complete, with no preamble: "
                        + ("call write_file with the full file content (for HTML: a complete standalone "
                           "document), then give the [DOWNLOAD: filename] link."
                           if (wants_file or missing_dl) else "write the full answer.")
                        + " Use only data from the conversation and tool results; mark unverified figures as estimates.")})
                    continue

                if missing_dl:
                    # continuation already used: drop the dead link rather than
                    # serving a fabricated file
                    for mf in missing_dl:
                        content = _re.sub(rf'\[DOWNLOAD:\s*{_re.escape(mf)}\]',
                                          f'*(file `{mf}` was not generated - please ask again)*', content)
                    yield f"event: delta_replace\ndata: {json.dumps({'text': content})}\n\n"

                if (not tool_calls and continued and wants_file and not written_files
                        and looks_undelivered(content, True, False)):
                    # the one nudge was spent and the model still only promised
                    # the file - say so instead of leaving a fake "here it comes"
                    note = ("\n\n*(The file was not generated this time - the model described it but did not "
                            "save it. Please ask again"
                            + (f"; your last saved file is still available: [DOWNLOAD: {prior_files[-1]}]"
                               if prior_files else "") + ")*")
                    content = (content or "") + note
                    yield f"event: delta\ndata: {json.dumps({'text': note})}\n\n"

                if not tool_calls or not chat_tools:
                    # Guard against empty/blank assistant response:
                    if not (content and content.strip()):
                        if reasoning and len(reasoning.strip()) > 15:
                            content = reasoning.strip()
                            yield f"event: delta\ndata: {json.dumps({'text': content})}\n\n"
                        else:
                            last_tool_results = [m.get("content", "") for m in msgs if m.get("role") == "tool"]
                            if last_tool_results:
                                summary_lines = []
                                for tr in last_tool_results[-3:]:
                                    cleaned_tr = str(tr).strip()
                                    if not cleaned_tr.startswith("error:"):
                                        summary_lines.append(cleaned_tr[:500])
                                if summary_parts := summary_lines:
                                    content = "I searched for information on your query:\n\n" + "\n\n---\n\n".join(summary_parts)
                                else:
                                    content = "Search completed, but no relevant details were returned."
                                yield f"event: delta\ndata: {json.dumps({'text': content})}\n\n"
                            else:
                                content = "I have processed your request."
                                yield f"event: delta\ndata: {json.dumps({'text': content})}\n\n"

                    if written_files:
                        content, _chg = _finalize_download_tags(content, written_files, last_query)
                        if _chg:
                            yield f"event: delta_replace\ndata: {json.dumps({'text': content})}\n\n"
                    if made_media:
                        content, _chg = _finalize_media(content, made_media)
                        if _chg:
                            yield f"event: delta_replace\ndata: {json.dumps({'text': content})}\n\n"

                    gen_toks = 0
                    if res_dict:
                        if res_dict.get("usage"):
                            gen_toks = res_dict["usage"].get("completion_tokens", 0)
                        elif res_dict.get("timings"):
                            gen_toks = res_dict["timings"].get("predicted_n", 0)
                    if not gen_toks:
                        req_mon = _monitor_state["active"].get(chat_rid)
                        gen_toks = req_mon.get("gen_tokens", 0) if req_mon else 0
                    if not gen_toks and content:
                        gen_toks = max(1, round(len(content) / 3.5))
                    prompt_toks = final_prompt_toks = _prompt_tokens_of(res_dict, msgs, current_tools)
                    _record_chat_usage(res_dict, sent_tokens, msgs, current_tools)
                    async for _vc in verifier.sse_events(user, "chat", last_query, content, msgs,
                                                         main_client, req.verify, model_source == "cloud"):
                        yield _vc
                    yield f"event: done\ndata: {json.dumps({'prompt_tokens': prompt_toks, 'completion_tokens': gen_toks, 'total_tokens': prompt_toks + gen_toks})}\n\n"
                    return

                # Reset any pre-tool preamble streamed to the user
                if streamed_content:
                    redactor.reset()
                    yield f"event: delta_reset\ndata: {{}}\n\n"

                clean_tool_calls = []
                tool_results_list = []
                for idx, tc in enumerate(tool_calls):
                    fn = tc.get("function", {})
                    t_name = fn.get("name", "web_search")
                    tc_id = tc.get("id") or f"chat_call_{turn}_{idx}"
                    args_raw = fn.get("arguments", "{}")
                    args = safe_parse_and_repair_args(args_raw, t_name) if isinstance(args_raw, (str, dict)) else {}
                    if not isinstance(args, dict):
                        args = {"query": str(args)}

                    yield sse("tool_call", {'id': tc_id, 'name': t_name, 'args': args})

                    try:
                        if not tool_allowed(t_name):
                            # text-parsed calls can name any tool; the schema filter isn't enough
                            res_str = f"error: tool '{t_name}' is not enabled for the active custom agent"
                        elif t_name in WEB_TOOL_NAMES and web_calls >= max_web_calls:
                            # text-emitted calls bypass the tools list; enforce the budget here too
                            res_str = ("error: web research budget used up - do not search again. "
                                       "Produce the final answer from the results you already have.")
                        elif t_name == "web_search":
                            q = args.get("query") or args.get("q") or ""
                            res_str = await tool_web_search({"query": q})
                        elif t_name == "web_search_images":
                            q = args.get("query") or args.get("q") or ""
                            res_str = await tool_web_search_images({"query": q})
                        elif t_name == "web_fetch":
                            u = args.get("url") or ""
                            res_str = await tool_web_fetch({"url": u})
                        elif t_name in ("generate_image", "generate_video") and t_name not in chat_media_tools:
                            res_str = ("error: making images isn't available in chat right now - "
                                       "ask the user to use /image or agent mode")
                        elif t_name in chat_media_tools:
                            # stream the job's steps into the tool card while it works
                            from core import media_tools as _mt
                            res_str = "error: failed"
                            async for ev, *rest in _mt.generate_events(
                                    "image" if t_name == "generate_image" else "video", args):
                                if ev == "progress":
                                    yield sse("tool_progress", {"id": tc_id, "name": t_name, **rest[0]})
                                else:
                                    res_str, done = rest
                                    if done and done.get("markdown"):
                                        made_media.append(done["markdown"])
                        elif t_name in ("doc_inspect", "doc_edit"):
                            impl = tool_doc_inspect_common if t_name == "doc_inspect" else tool_doc_edit_common
                            res_str = await run_in_executor_ctx(impl, args)
                            if res_str.startswith("doc error"):
                                res_str = "error: " + res_str
                            else:
                                written_files.extend(_DL_TAG_RE.findall(res_str))
                        elif t_name == "write_file" and _wraps_media(
                                made_media, args.get("path") or args.get("file") or args.get("filename"), last_query):
                            res_str = _WRAP_REFUSAL
                        elif t_name == "write_file":
                            res_str = tool_write_file_common(args)
                            p_name = args.get("path") or args.get("file") or args.get("filename")
                            # a failed write must not contribute a download link: every
                            # written_files entry later becomes a [DOWNLOAD:] marker, which
                            # would point the UI at a file that does not exist
                            if p_name and not res_str.startswith("error:"):
                                real_name = _saved_filename(res_str, Path(p_name).name)
                                written_files.append(real_name)
                                # The model's own reply (written before this tool result
                                # exists) may still reference the pre-write name it chose;
                                # rewrite the model's arguments in place so any later
                                # transcript scraping (or DOWNLOAD-tag echoing) downstream
                                # picks up the real on-disk filename instead.
                                args["path"] = real_name
                                orig_clean = Path(p_name).name
                                if orig_clean != real_name and content:
                                    content = _re.sub(rf'\[DOWNLOAD:\s*{_re.escape(orig_clean)}\]', f'[DOWNLOAD: {real_name}]', content)
                                    yield f"event: delta_replace\ndata: {json.dumps({'text': content})}\n\n"
                        else:
                            res_str = await run_tool(t_name, args)
                        ok = not (isinstance(res_str, str) and (res_str.startswith("error:") or res_str.startswith("File not found")))
                    except Exception as e:
                        res_str = f"error: {e}"
                        ok = False

                    if t_name in WEB_TOOL_NAMES:
                        web_calls += 1
                    yield sse("tool_result", {'id': tc_id, 'name': t_name, 'ok': ok, 'result': res_str})

                    hist_args = dict(args)
                    if t_name in ("write_file", "edit_file"):
                        if "content" in hist_args and len(str(hist_args["content"])) > 400:
                            f_path = hist_args.get("path") or hist_args.get("file") or "file"
                            c_len = len(str(hist_args["content"]))
                            hist_args["content"] = f"<{c_len} chars written to {f_path}>"

                    clean_tool_calls.append({
                        "id": tc_id,
                        "type": "function",
                        "function": {"name": t_name, "arguments": json.dumps(hist_args)}
                    })
                    tool_results_list.append((tc_id, res_str))

                msgs.append({"role": "assistant", "content": content or "", "tool_calls": clean_tool_calls})
                for tid, r_out in tool_results_list:
                    msgs.append({"role": "tool", "tool_call_id": tid, "content": r_out})

            # Guaranteed synthesis pass: if the turn loop finished and the last message in msgs is a tool response,
            # the model executed tools but hasn't yet synthesized the final text answer for the user.
            # Call the model with tools=None to force it to formulate a comprehensive final answer!
            if msgs and msgs[-1].get("role") == "tool":
                streamed_content = []
                res_dict = None
                if cloud_main and cloud.cloud_bindings(user.id).get("fallback_local", True):
                    fb_local = None
                    target = state.profile_path or state.profile or common.initial_profile_path
                    if target:
                        try:
                            if state.process is None or state.process.poll() is not None:
                                await state.ensure_running(target)
                            fb_local = state.client
                        except Exception:
                            pass
                    final_rep = req.repeat_penalty if req.repeat_penalty is not None else 1.15
                    final_stream = common._llm_chat_stream_with_fallback(
                        main_client, fb_local, msgs, None, req.temperature,
                        req.max_tokens, repeat_penalty=final_rep, rid=chat_rid, lane="main", effort=effort,
                        top_p=req.top_p, min_p=req.min_p, presence_penalty=req.presence_penalty, top_k=req.top_k, extra=sampler_extra(req))
                else:
                    final_rep = req.repeat_penalty if req.repeat_penalty is not None else 1.15
                    final_stream = _llm_chat_stream(main_client, msgs, tools=None, temperature=req.temperature,
                                                    max_tokens=req.max_tokens, repeat_penalty=final_rep, rid=chat_rid, effort=effort,
                                                    top_p=req.top_p, min_p=req.min_p, presence_penalty=req.presence_penalty, top_k=req.top_k, extra=sampler_extra(req))

                async for ev, val in final_stream:
                    if ev == "queued":
                        yield f"event: queued\ndata: {json.dumps(val)}\n\n"
                        continue
                    if ev == "thought_delta":
                        yield f"event: thought_delta\ndata: {json.dumps({'delta': val})}\n\n"
                    elif ev == "content_to_thought":
                        # forced-open <think>: the text streamed as the answer was reasoning
                        redactor.reset()
                        streamed_content.clear()
                        yield "event: delta_to_thought\ndata: {}\n\n"
                    elif ev == "content_delta":
                        _safe = redactor.feed(val)
                        streamed_content.append(_safe)
                        if _safe:
                            yield f"event: delta\ndata: {json.dumps({'text': _safe})}\n\n"
                        _n = _guard_notice()
                        if _n:
                            yield _n
                    elif ev == "result":
                        res_dict = val

                _tail = redactor.flush()
                if _tail:
                    streamed_content.append(_tail)
                    yield f"event: delta\ndata: {json.dumps({'text': _tail})}\n\n"
                content = res_dict.get("content", "") if res_dict else "".join(streamed_content)
                if res_dict and res_dict.get("reasoning"):
                    reasoning = res_dict["reasoning"]

            # Robust fallback: if content is still empty, never terminate silently without output
            if not (content and content.strip()):
                if reasoning and len(reasoning.strip()) > 15:
                    content = reasoning.strip()
                    yield f"event: delta\ndata: {json.dumps({'text': content})}\n\n"
                else:
                    last_tool_results = [m.get("content", "") for m in msgs if m.get("role") == "tool"]
                    if last_tool_results:
                        summary_lines = ["I searched for information on your query:\n"]
                        for tr in last_tool_results[-3:]:
                            cleaned_tr = str(tr).strip()[:500]
                            if not cleaned_tr.startswith("error:"):
                                summary_lines.append(cleaned_tr)
                        content = "\n\n---\n\n".join(summary_lines)
                        yield f"event: delta\ndata: {json.dumps({'text': content})}\n\n"
                    else:
                        content = "I have processed your request."
                        yield f"event: delta\ndata: {json.dumps({'text': content})}\n\n"

            if written_files:
                content, _chg = _finalize_download_tags(content, written_files, last_query)
                if _chg:
                    yield f"event: delta_replace\ndata: {json.dumps({'text': content})}\n\n"
            if made_media:
                content, _chg = _finalize_media(content, made_media)
                if _chg:
                    yield f"event: delta_replace\ndata: {json.dumps({'text': content})}\n\n"

            gen_toks = 0
            if res_dict:
                if res_dict.get("usage"):
                    gen_toks = res_dict["usage"].get("completion_tokens", 0)
                elif res_dict.get("timings"):
                    gen_toks = res_dict["timings"].get("predicted_n", 0)
            if not gen_toks:
                req_mon = _monitor_state["active"].get(chat_rid)
                gen_toks = req_mon.get("gen_tokens", 0) if req_mon else 0
            if not gen_toks and content:
                gen_toks = max(1, round(len(content) / 3.5))
            prompt_toks = final_prompt_toks = _prompt_tokens_of(res_dict, msgs, current_tools)
            _record_chat_usage(res_dict, sent_tokens, msgs, current_tools)

            async for _vc in verifier.sse_events(user, "chat", last_query, content, msgs,
                                                 main_client, req.verify, model_source == "cloud"):
                yield _vc
            yield f"event: done\ndata: {json.dumps({'prompt_tokens': prompt_toks, 'completion_tokens': gen_toks, 'total_tokens': prompt_toks + gen_toks})}\n\n"
        except asyncio.CancelledError:
            pass
        except Exception as e:
            yield f"event: delta\ndata: {json.dumps({'text': f'⚠️ Chat error: {e}'})}\n\n"
            yield f"event: done\ndata: {{}}\n\n"
        finally:
            if redactor.matched:
                audit_log(user, action="output_guard.redact", resource=redactor.matched.get("name"),
                          detail={"endpoint": "chat/run", "scope": redactor.matched.get("scope"),
                                  "hits": redactor.hits}, result="deny")
            dt = time.time() - t0
            req_mon = _monitor_state["active"].get(chat_rid)
            toks = req_mon.get("gen_tokens") if req_mon else None
            tps = (toks / dt) if (toks and dt and dt > 0) else None
            ptoks = final_prompt_toks or estimate_prompt_tokens(msgs)
            pcached = (sum(len(m.get("content", "")) for m in msgs[:-1]) // 4) if len(msgs) > 1 else 0
            monitor_end(chat_rid, 200, prompt_tokens=ptoks, completion_tokens=toks, tps=tps, duration=dt,
                        model=clean_model_name, prompt_cached=pcached, completion_cached=0,
                        source=model_source, provider=model_provider)
            db_record_request("chat/run", clean_model_name, ptoks, toks, tps, dt, None, True, 200,
                              prompt_cached_tokens=pcached, completion_cached_tokens=0, is_orchestrator=False,
                              source=model_source, provider=model_provider)

    return StreamingResponse(event_stream(), media_type="text/event-stream")

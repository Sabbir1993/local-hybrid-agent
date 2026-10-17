import asyncio
import json
import re
import sys
import time
import uuid
from pathlib import Path
from fastapi import Depends, Request
from fastapi.responses import StreamingResponse, JSONResponse
from core.auth import Principal
from core.deps import get_current_user
from core.audit import audit_log
from core import input_guard, runs
from core.chat_errors import friendly_chat_error
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
from core.doc_tools import (CHAT_IMPLS as _DOC_CHAT_IMPLS, DOC_APPEND_SCHEMA, DOC_CREATE_SCHEMA, DOC_EDIT_SCHEMA,
                            DOC_INSPECT_SCHEMA, tool_doc_inspect_common)
from core.agent_loop import doc_redirect as _doc_redirect
from core.agent_loop import (
    run_tool,
    safe_parse_and_repair_args,
    _extract_text_tool_calls,
    estimate_prompt_tokens,
)
from core.agent_loop.degeneration import RUNAWAY_MSG, sanitize_user_facing_content
from core import prompt_scope
from core.doc_tools import builds
from core.monitor import (
    _monitor_state,
    monitor_begin,
    monitor_end,
)
from core.knowledge_access import allowed_source_ids_for, cloud_ok_ids, hits_need_local, kb_local_only, set_kb_cloud_blocked
from core.db import db_record_request
from .. import common
from core import deck_research, reasoning
from ..common import _llm_chat_stream
from ..common.sampling_extra import sampler_extra

from .base import router
from .delivery import (
    _NO_TOOL_ANSWER,
    _WRAP_REFUSAL,
    chat_write_file,
    _ALREADY_SAVED,
    _DUPLICATE_WORKBOOK,
    _duplicate_workbook,
    _already_written,
    _finalize_download_tags,
    scrub_server_paths,
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
    missing_type,
    load_prior_file,
    looks_undelivered,
    pick_target,
    session_files,
    shrink_old_tool_results,
    wants_file_output,
    serves_brief_card,
    KB_TOOL_NAMES,
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


WEB_AFTER_KB_BLOCK = (" The user has web search turned on, so you may search the public web for general or public "
                      "information about the question. Say clearly that such facts come from the public web, "
                      "not from company records, and never present web results as internal company data.")


@router.post("/chat/run")
async def chat_run(req: ChatRunRequest, user: Principal = Depends(get_current_user), request: Request = None):
    from core.agent_tools import set_current_user
    from core import voice_policy
    set_current_user(user.id)
    voice_policy.apply(req)     # a spoken turn: no hidden reasoning, short answer, no answer-check rewrite
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
    # deck pictures: the web is searched only when the user switched web search on for this chat
    from core.doc_tools import slide_images
    slide_images.allow_web.set(bool(use_web))
    slide_images.main_loop.set(asyncio.get_running_loop())

    msgs = [dict(m) for m in req.messages]
    last_query = ""
    for m in reversed(msgs):
        if m.get("role") == "user":
            last_query = str(m.get("content", ""))
            break
    from core.doc_tools import operations as deck_operations
    _style = re.search(r"slide style\??\s*:\s*(designer|editable)", last_query or "", re.I)
    deck_operations.forced_mode.set(_style.group(1).lower() if _style else "")      # the questionnaire's answer wins

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
    # what the user typed, without the attachment text the client pastes into the same message: every intent
    # check below must read this, or a word inside a sheet ("add", "ssl", "policy") decides what the turn is
    from core.knowledge_router import has_attachment_text, own_words, should_consult_kb
    own_q = own_words(last_query, 2000)
    # attached data anywhere in the conversation (a follow-up like 'which status has the most rows' has none in its own message)
    has_attachments = any(m.get('role') == 'user' and has_attachment_text(str(m.get('content') or '')) for m in msgs)
    _prev_a = next((str(m.get("content") or "") for m in reversed(msgs[:-1]) if m.get("role") == "assistant"), "")
    _asked = "```questionnaire" in _prev_a or _prev_a.rstrip()[-300:].rstrip("*_ ").endswith("?")
    file_followup = file_followup_intent(own_q, bool(prior_files), prior_files, _asked)
    absent_type = missing_type(own_q, prior_files) if file_followup == "where" else ""
    if absent_type:
        file_followup = None            # "where is the spreadsheet?" when only a .md exists: build it, do not re-send the .md
    if file_followup == "where":
        # "where is the file?" - answer with the existing link instead of
        # asking a slow local model to regenerate (and usually invent) it.
        latest = pick_target(own_q, prior_files)
        others = [f for f in prior_files if f != latest][-3:]
        reply = f"Here is the file you asked about:\n\n[DOWNLOAD: {latest}]"
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
    # a short reply to the assistant's own "which style / how many slides?" question continues the deck request
    prev_assistant = next((str(m.get("content") or "") for m in reversed(msgs[:-1]) if m.get("role") == "assistant"), "")[-500:]
    deck_answer = (len(last_query) < 240 and "?" in prev_assistant and "[DOWNLOAD" not in prev_assistant
                   and bool(prompt_scope._PPTX_RE.search(prev_assistant)))
    pdf_build = prompt_scope.pdf_brief_wanted(last_query, msgs[:-1])      # a PDF request, or the answers to our PDF card
    docx_build = prompt_scope.docx_brief_wanted(last_query, msgs[:-1])    # a Word request, or the answers to our Word card
    longdoc_build = pdf_build or docx_build                               # both are written in pieces: doc_create, doc_append
    brief_phase = prompt_scope.brief_phase(last_query, msgs[:-1])         # "ask" (card now) / "build" (answered) / ""
    # The model writes the "few quick choices" card, as it always did. The server's copy of it (prompt_scope.brief_card) is used
    # only when the model fails to produce one (a local model sometimes narrates its reasoning instead), or always when
    # chat.brief_card_server is "always".
    card_mode = (APP_CONFIG.get("chat") or {}).get("brief_card_server", "fallback")
    card = prompt_scope.brief_card("deck" if prompt_scope.deck_brief_wanted(last_query, msgs[:-1]) else "docx" if docx_build else "pdf") if brief_phase == "ask" else ""
    if brief_phase == "ask" and card_mode == "always":

        async def _card_sse():
            yield f"event: delta\ndata: {json.dumps({'text': card})}\n\n"
            yield f"event: done\ndata: {json.dumps({'prompt_tokens': 0, 'completion_tokens': 0, 'total_tokens': 0})}\n\n"
        return StreamingResponse(_card_sse(), media_type="text/event-stream")
    resume_note = builds.resume_note(user.id, last_query)                 # "continue" after a stop / restart mid-deck
    sheet_build = prompt_scope.sheet_build_wanted(last_query, msgs[:-1])        # "make me an Excel workbook ..."
    sheet_build = sheet_build or absent_type == "spreadsheet"
    explicit_ext = prompt_scope.explicit_format(own_q)              # the user named the file type: that is the deliverable
    if explicit_ext == ".xlsx":
        sheet_build = sheet_build or prompt_scope.sheet_build_wanted(own_q + " create", msgs[:-1])
    wants_file = wants_file_output(own_q) or file_followup == "edit" or deck_answer or longdoc_build or bool(resume_note) or sheet_build or bool(absent_type)
    has_file = bool(file_followup or wants_file)
    web_explicit = bool(use_web and prompt_scope.web_intent(last_query, url_matches, msgs,
                                                            explicit_only=voice_policy.web_explicit_only(req),
                                                            research=bool((APP_CONFIG.get("chat") or {}).get("research_web", False))))
    has_web = False   # decided after the KB lookup (core/kb_coverage.py): the web fills what the KB cannot

    # Tools for Chat Mode: write_file always (core), the rest on intent.
    chat_tools = [CHAT_WRITE_FILE_SCHEMA]
    # a spreadsheet attached to this conversation: the model gets its profile in the message and data_query for exact figures
    has_data_attachment = any(m.get("role") == "user" and prompt_scope.DATA_ATTACHMENT_RX.search(str(m.get("content") or ""))
                              for m in msgs)
    if has_data_attachment:
        from core.agent_tools import AGENT_TOOLS as _ALL_TOOLS
        chat_tools += [t for t in _ALL_TOOLS if t.get("function", {}).get("name") in ("data_query", "data_sql", "data_clean")]
    pdf_work = prompt_scope.pdf_tools_wanted(own_q, prior_files)
    if pdf_work:
        from core.agent_tools import AGENT_TOOLS as _PDF_ALL
        chat_tools += [t for t in _PDF_ALL if t.get("function", {}).get("name", "").startswith("pdf_")]
    if has_file:
        chat_tools += [DOC_INSPECT_SCHEMA, DOC_EDIT_SCHEMA]
        if longdoc_build or resume_note or brief_phase or sheet_build or prompt_scope.deck_brief_wanted(last_query, msgs[:-1]):
            chat_tools += [DOC_CREATE_SCHEMA, DOC_APPEND_SCHEMA]       # decks / PDFs are built in pieces
    # The isolated code runner (core/sandbox): offered only when it is paired, switched on, answering AND the request needs custom
    # code. The app never runs model-written code itself; with no runner the tool does not exist for the model.
    sandbox_on = False
    try:
        from core import sandbox as _sandbox
        if _sandbox.sandbox_wanted(own_q, has_data_attachment) and await _sandbox.available():
            chat_tools.append(_sandbox.RUN_SANDBOX_SCHEMA)
            sandbox_on = True
    except Exception as _e:
        print(f"[chat] code runner check failed: {type(_e).__name__}", file=sys.stderr)
    from core.agent_tools import AGENT_TOOLS
    skb = next((t for t in AGENT_TOOLS if t.get("function", {}).get("name") == "search_knowledge_base"), None)
    if skb:
        chat_tools.append(skb)
        chat_tools.extend(t for t in AGENT_TOOLS if t.get("function", {}).get("name") in ("kb_find_mentions", "kb_query", "kb_sql"))
    # images made on this PC (stable-diffusion.cpp): the model may draw when asked.
    # A cloud-first route isn't offered here (chat has no ask-first dialog for cost);
    # /image and agent mode handle those.
    chat_media_tools = set()
    try:
        from core import media_tools
        img_schema = media_tools.chat_image_tool_schema() if prompt_scope.image_intent(last_query, msgs[:-1]) else None
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
        agent_dev_id = custom_agent.get("device_id")
        cur_dev_id = request.headers.get("X-Device-Id") if request is not None else None
        if agent_dev_id and cur_dev_id:
            from core.request_context import normalize_device_id
            if normalize_device_id(agent_dev_id) != normalize_device_id(cur_dev_id):
                return JSONResponse(
                    {"error": "custom_agent_other_device",
                     "message": f"This agent is bound to '{custom_agent.get('device_name') or 'another device'}'. Please fork it to use on this machine."},
                    status_code=403
                )
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
    if req.voice:
        from core.voice_prompt import VOICE_RULES
        sys_parts.append(VOICE_RULES)
        sys_labels.append("voice")

    deep = bool(req.deep_mode)
    # How much the model reasons: the composer's effort level, independent of
    # Deep research; mapped per local/cloud target in common._llm_chat_stream_raw
    effort = reasoning.resolve(req.reasoning_effort, deep)
    # Building or editing an Office file / PDF is writing, not puzzle solving: unrestricted ("Extra") thinking only burns
    # minutes before the first slide. The ceiling is app.json reasoning_effort.doc_build_ceiling (default "medium").
    doc_build = (longdoc_build or prompt_scope.deck_brief_wanted(last_query, msgs[:-1]) or file_followup == "edit"
                 or bool(_re.search(r"\.(?:pptx|docx|pdf)\b", prev_assistant, _re.IGNORECASE)))
    if brief_phase == "build":
        effort = deck_research.effort_for(effort, APP_CONFIG)       # a deck is not a quick answer: at least medium thinking
    if doc_build:
        effort = reasoning.cap(effort, str((APP_CONFIG.get("reasoning_effort") or {}).get("doc_build_ceiling", "medium")))

    # Organizational knowledge base: permission-scoped routing & retrieval.
    # Injected only for company-directed queries or strongly matching chunks -
    # otherwise unrelated KB text gets mixed into general answers. The model
    # can still call search_knowledge_base explicitly.
    kb_ids = allowed_source_ids_for(user)
    if not kb_ids:                       # nothing to search: do not offer the tools (the agent route already does this)
        chat_tools[:] = [t for t in chat_tools if t.get("function", {}).get("name") not in KB_TOOL_NAMES]
    kb_used = False
    kb_hits = []
    kb_prompt_block = ""
    kb_blocked_reason = ""
    cov_hits = []
    from core.knowledge_router import kb_routing_query
    kb_query = kb_routing_query(msgs, last_query)
    if brief_phase == "build":
        # the latest message is only the card's answers: route the KB by the request the card belongs to
        kb_query = own_words(prompt_scope.brief_request(msgs[:-1]) or last_query, 600)
    # one policy for "should the company KB be searched for this turn" (core/knowledge_router.should_consult_kb): the
    # user's own words decide, attached data comes first, small talk never retrieves; /kb on|off overrides it.
    consult_kb, kb_reason = should_consult_kb(kb_query, has_attachments, voice_policy.kb_company_only(req), req.kb)
    skip_kb = not consult_kb
    if req.kb == "off" and wants_file_output(own_q):    # a pure file request with the KB switched off: do not tempt the model into the KB tools
        chat_tools[:] = [t for t in chat_tools if t.get("function", {}).get("name") not in KB_TOOL_NAMES]
    if kb_ids and kb_query.strip() and not file_followup and not skip_kb:
        try:
            from core.knowledge_router import fetch_company_knowledge, is_company_or_kb_query
            kb_hits, kb_prompt_block = await fetch_company_knowledge(kb_query, kb_ids, k=6)
            if kb_hits:
                auto_cos = float((APP_CONFIG.get("knowledge") or {}).get("auto_inject_cos", 0.55))
                top_cos = max(float(h.get("cos") or 0.0) for h in kb_hits)
                if not (is_company_or_kb_query(kb_query, kb_ids) or top_cos >= auto_cos):
                    kb_hits, kb_prompt_block = [], ""
            cov_hits = list(kb_hits)         # scores only feed the web decision below (also when withheld)
            if kb_hits:
                kb_used = True
                sys_parts.append(kb_prompt_block)
                sys_labels.append("kb")
        except Exception as e:
            print(f"[chat] knowledge retrieval failed: {e}", file=sys.stderr)
    if kb_ids:
        from core import kb_log
        kb_log.record(user.id, "chat", consult_kb and not file_followup, kb_reason, len(cov_hits),
                      max((float(h.get("cos") or 0.0) for h in cov_hits), default=None), kb_used)

    # Data residency (core/knowledge_access.py): internal knowledge never goes
    # to a cloud provider. A KB question from a cloud-bound user is answered by
    # the local model instead; if none can run, the KB context is withheld.
    if cloud_main and kb_local_only():
        if kb_hits and hits_need_local(kb_hits):     # sources the admin cleared for cloud stay on the cloud lane
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
        if cloud_main and skb in chat_tools and not cloud_ok_ids(kb_ids):
            chat_tools.remove(skb)
            chat_tools[:] = [t for t in chat_tools if t.get("function", {}).get("name") not in ("kb_find_mentions", "kb_query")]
    set_kb_cloud_blocked(cloud_main is not None)
    # Web only for what the KB cannot answer (core/kb_coverage.py): full -> none, partial -> lean hint + small
    # budget, none -> only on explicit web intent; the budget grows with reasoning effort / Deep mode.
    from core import kb_coverage
    web_budget_n = None
    web_lean = False
    cov = "none"
    if kb_coverage.gap_fill_enabled():
        if use_web and not web_explicit and not voice_policy.active(req):
            cov = await kb_coverage.resolve(kb_query, cov_hits, blocked=bool(kb_blocked_reason))
        else:
            cov = "none"
        plan = kb_coverage.decide_web(cov, kb_coverage.tier(effort, deep), bool(use_web), web_explicit)
        has_web, web_lean, web_budget_n = plan["web"], plan["lean"], plan["budget"]
        print(f"[chat] route kb={cov} web={'lean' if web_lean and has_web else ('full' if has_web else 'off')} "
              f"budget={web_budget_n} tier={kb_coverage.tier(effort, deep)}", file=sys.stderr)
    else:
        has_web = web_explicit
    # building a deck / PDF: its own, larger research budget (searches + full-page reads) before the first slide
    plan_w = deck_research.web_plan(APP_CONFIG, brief_phase, bool(use_web), cov, has_web, web_lean, web_budget_n)
    has_web, web_lean, web_budget_n = plan_w["has_web"], plan_w["lean"], plan_w["budget"]
    if brief_phase == "ask":
        has_web = False          # nothing researched before the card is kept (only the final text is), so don't search
    if has_web:
        register_web_tools()
        for t_name in ("web_search", "web_fetch", "web_search_images"):
            if brief_phase == "build" and t_name == "web_search_images":
                continue         # deck pictures come from `image:` lines (core/doc_tools/slide_images.py)
            rt = registry.get(t_name)
            if rt and rt.schema and (allowed_names is None or t_name in allowed_names):
                chat_tools.append(rt.schema)
        if kb_blocked_reason and not web_lean:
            sys_parts.append(WEB_AFTER_KB_BLOCK.strip())
            sys_labels.append("kb-web-note")

    # S1: the full manual only on file intent, else a lean pointer (S4).
    if has_file:
        sys_parts.append(prompt_scope.file_manual(DOC_CREATE_SCHEMA in chat_tools))
        sys_labels.append("file-manual")
        if prompt_scope.pptx_intent(last_query, prior_files, msgs):
            sys_parts.append(prompt_scope.PPTX_MANUAL)
            sys_labels.append("pptx-manual")
        if sheet_build:
            sys_parts.append(prompt_scope.XLSX_MANUAL)
            sys_labels.append("xlsx-manual")
    else:
        sys_parts.append(prompt_scope.FILE_MANUAL_LEAN)
        sys_labels.append("file-lean")

    if pdf_work:
        sys_parts.append(prompt_scope.PDF_TOOLS_MANUAL)
        sys_labels.append("pdf-tools")
    if has_data_attachment:
        sys_parts.append(prompt_scope.ATTACHED_DATA)
        sys_labels.append("attached-data")

    if prior_files:
        latest = pick_target(own_q, prior_files)           # the file the user means: named, or the newest
        prior_block = (
            "FILES IN THIS CONVERSATION (attached by the user or created here, oldest first): " + ", ".join(prior_files[-5:]) + "\n"
            f"{'The most recent file is' if latest == prior_files[-1] else 'The file the user means is'} `{latest}`. "
            f"If the user asks where a file is, give its "
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
    if has_web and web_lean:
        sys_parts.append(prompt_scope.WEB_LEAN.format(
            kb=prompt_scope.WEB_LEAN_KB["blocked" if kb_blocked_reason else ("used" if kb_used else "none")],
            n=web_budget_n))
        sys_labels.append("web-lean")
    elif has_web:
        sys_parts.append(prompt_scope.WEB_MANUAL)
        sys_labels.append("web-manual")
        if kb_used:
            sys_parts.append(prompt_scope.PROVENANCE)
            sys_labels.append("sources")

    # advice-style answers: sections, thumbnails, a questionnaire card (chat.style, off unless enabled)
    if prompt_scope.style_wanted(last_query, msgs[:-1], voice=bool(req.voice),
                                 enabled=bool((APP_CONFIG.get("chat") or {}).get("style", False))):
        sys_parts.append(prompt_scope.CHAT_STYLE)
        sys_labels.append("style")

    # "how does X work" answers may carry one drawn flow/cards block (chat.visuals, on unless set false)
    if prompt_scope.visual_wanted(last_query, voice=bool(req.voice),
                                  enabled=bool((APP_CONFIG.get("chat") or {}).get("visuals", True))):
        sys_parts.append(prompt_scope.VISUAL_STYLE)
        sys_labels.append("visual")

    # PowerPoint requests: ask slide count / audience / palette first, then build a real .pptx
    if prompt_scope.deck_brief_wanted(last_query, msgs[:-1]):
        sys_parts.append(prompt_scope.DECK_BRIEF)
        sys_labels.append("deck-brief")
    elif pdf_build:
        sys_parts.append(prompt_scope.PDF_BRIEF)
        sys_labels.append("pdf-brief")
    elif docx_build:
        sys_parts.append(prompt_scope.DOCX_BRIEF)
        sys_labels.append("docx-brief")
    if sandbox_on:
        sys_parts.append(_sandbox.SANDBOX_MANUAL)
        sys_labels.append("sandbox")
    if brief_phase:
        sys_parts.append(prompt_scope.BRIEF_BUILD_NOW if brief_phase == "build" else prompt_scope.BRIEF_ASK_NOW)
        sys_labels.append("brief-" + brief_phase)
    if explicit_ext and has_file:
        sys_parts.append(prompt_scope.format_note(explicit_ext))
        sys_labels.append("explicit-format")
    if absent_type:
        sys_parts.append(f"[system note] The user is asking for a {absent_type} that has NOT been made in this chat yet (only other "
                         f"file types exist). Create it now from the content already discussed in this conversation: do not "
                         f"re-send the other file, do not ask what to include, and finish with the [DOWNLOAD: file] link.")
        sys_labels.append("missing-type")
    if resume_note:
        sys_parts.append(resume_note)
        sys_labels.append("build-resume")

    # Tool-loop budget: normal vs deep ("think") mode, from app.json "chat"
    chat_cfg = APP_CONFIG.get("chat") or {}
    max_turns = int(chat_cfg.get("deep_max_tool_rounds" if deep else "max_tool_rounds", 25 if deep else 15)) if chat_tools else 1
    max_web_calls = int(chat_cfg.get("deep_max_web_calls" if deep else "max_web_calls", 16 if deep else 8))
    if web_budget_n is not None:                      # coverage-aware budget (scales with effort / Deep)
        max_web_calls = web_budget_n
        if has_web and chat_tools:
            max_turns = kb_coverage.rounds_for(max_web_calls, max_turns)
            if brief_phase == "build":
                max_turns = deck_research.turns_for(max_web_calls, max_turns)      # research rounds + the build rounds
    if deep:
        sys_parts.append(
            "DEEP RESEARCH MODE:\n"
            "1. First, briefly list the sub-questions you need answered to fully satisfy the request.\n"
            + (f"2. Research each one with targeted web searches (use the current year; you have up to {max_web_calls} web calls).\n"
               if has_web else
               "2. Answer each one from the knowledge base and what you know (no web tools are offered for this question).\n")
            + "3. Stop searching as soon as you have enough data, then write the complete deliverable in one go. "
            "Cite sources, and label any figure you could not verify as an estimate.")
        sys_labels.append("deep")
    deck_lim = deck_research.build_limits(APP_CONFIG, brief_phase)     # no repeat penalty, capped turn length while building
    turn_max_tokens = deck_research.clamp_tokens(req.max_tokens, deck_lim["max_tokens"])
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
        run_id = uuid.uuid4().hex
        run_end_state, run_end_reason, turn = "completed", None, -1
        runaway_hits = 0
        runs.begin(run_id, "chat", user.id, stage="stream")
        t0 = time.time()
        turn_limit = max_turns       # grows by one if a continuation nudge needs it
        web_calls = 0
        web_seen = set()
        research_nudged = False
        continued = False
        finish_reason = None
        written_files = []
        write_redirected = set()     # core.agent_loop.doc_redirect: the "use doc_create for a PDF / Word / deck" refusal is said once
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
        redactor = output_guard.OutputRedactor(user, model_source == "cloud",
                                               sentence_release=voice_policy.sentence_release(req))
        think_redactor = output_guard.OutputRedactor(user, model_source == "cloud")
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
                run_cap = float(chat_cfg.get("run_timeout_s", 1800) or 0)
                if run_cap and turn and time.time() - t0 > run_cap:
                    run_end_reason = "timeout"      # chat had no wall-clock stop: the tool-free wrap-up below answers
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
                    yield sse("tool_call", {'id': tc_id, 'name': 'search_knowledge_base', 'args': {'query': kb_query, 'auto': True}})   # the short routed question, not the whole message
                    yield sse("tool_result", {'id': tc_id, 'name': 'search_knowledge_base', 'ok': True, 'result': f'Retrieved {len(kb_hits)} records from company knowledge base ({source_names}) - automatic lookup from your question'})

                # Deck / PDF research: the broad searches run side by side (web I/O overlaps), the model then only follows up
                if (turn == 0 and brief_phase == "build" and has_web and use_web and not written_files and tool_allowed("web_search")
                        and not url_matches):
                    pre_q = deck_research.parallel_queries(prompt_scope.brief_request(msgs[:-1]) or last_query, max_web_calls, APP_CONFIG)
                    if pre_q:
                        cap = deck_research.web_cap(brief_phase)
                        ids = [f"prefetch_{i}" for i in range(len(pre_q))]
                        for tid, q in zip(ids, pre_q):
                            yield sse("tool_call", {'id': tid, 'name': 'web_search', 'args': {'query': q}})
                        outs = await asyncio.gather(*[tool_web_search({"query": q}) for q in pre_q], return_exceptions=True)
                        calls_msg, tool_msgs = [], []
                        for tid, q, out in zip(ids, pre_q, outs):
                            out = f"error: search failed ({type(out).__name__})" if isinstance(out, BaseException) else out
                            web_seen.add(kb_coverage.query_key(q))      # not charged to the budget: it is for the model's follow-ups
                            out = kb_coverage.cap_web_result(out, len(tool_msgs) + 1, *cap)
                            ok = not (isinstance(out, str) and out.startswith("error:"))
                            yield sse("tool_result", {'id': tid, 'name': 'web_search', 'ok': ok, 'result': out})
                            calls_msg.append({"id": tid, "type": "function", "function": {"name": "web_search", "arguments": json.dumps({"query": q})}})
                            tool_msgs.append({"role": "tool", "tool_call_id": tid, "content": out})
                        msgs.append({"role": "assistant", "content": f"I searched {len(pre_q)} angles at once; the web budget is untouched, so I can follow up on gaps.", "tool_calls": calls_msg})
                        msgs.extend(tool_msgs)

                res_dict = None
                streamed_content = []

                # Research budget: once web calls are used up (or only the last two
                # turns remain) drop the web tools but keep write_file, so the
                # remaining turns go to producing the answer instead of more searches.
                research_over = bool(use_web and web_calls > 0 and (
                    web_calls >= max_web_calls or turn >= turn_limit - 2))
                if brief_phase == "build" and written_files:
                    research_over = True        # the deck exists: from here on only doc_append, no more searching
                if turn == turn_limit - 1 and msgs and msgs[-1].get("role") == "tool":
                    # final turn: must answer now; a requested file can still be written. A deck / PDF being built in pieces keeps its
                    # document tools: with write_file only, the model said "there is no doc_append tool" and left the deck half built.
                    doc_names = ("doc_create", "doc_append", "doc_inspect", "doc_edit")
                    doc_tools = [t for t in chat_tools if t.get("function", {}).get("name") in doc_names]
                    if doc_tools and (brief_phase == "build" or resume_note or longdoc_build) and wants_file:
                        current_tools = doc_tools
                    else:
                        current_tools = [CHAT_WRITE_FILE_SCHEMA] if (wants_file and tool_allowed("write_file")) else None
                elif research_over:
                    current_tools = [t for t in chat_tools
                                     if t.get("function", {}).get("name") not in WEB_TOOL_NAMES]
                else:
                    current_tools = chat_tools
                if research_over and not research_nudged and brief_phase == "build":
                    research_nudged = True
                    msgs.append({"role": "user", "content": (
                        "[system note] Do not search and do not ask the user anything: their answers are already in the "
                        "conversation. Continue building the document: call doc_append with the next slides/sections of "
                        "your plan (done=true on the last call).")})
                elif research_over and not research_nudged:
                    research_nudged = True
                    msgs.append({"role": "user", "content": (
                        "[system note] The research phase is over - do not search any more. Using the "
                        "results above, produce the complete answer now"
                        + (" and call write_file with the full file content." if wants_file else ".")
                        + " Do not announce what you will do; just do it.")})

                shrink_old_tool_results(msgs, ctx_budget, tools=current_tools)

                # chat.force_doc_tool (off by default): the first build turn of a PDF / Word document must be a tool call. The API
                # enforces it, the prompt only asks. Not while web tools are on offer (research may come first), not after tool
                # results (the model then chooses), and a server that rejects tool_choice is remembered and asked without it.
                _names = {t.get("function", {}).get("name") for t in (current_tools or [])}
                tc_kw = ({"tool_choice": "required"}
                         if (chat_cfg.get("force_doc_tool", False) and brief_phase == "build" and not written_files
                             and "doc_create" in _names and not (_names & set(WEB_TOOL_NAMES))
                             and not (msgs and msgs[-1].get("role") == "tool"))
                         else {})

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
                    chat_rep = deck_lim["repeat_penalty"] or (req.repeat_penalty if req.repeat_penalty is not None else 1.15)
                    chat_stream = common._llm_chat_stream_with_fallback(
                        main_client, fb_local, msgs, current_tools, req.temperature,
                        turn_max_tokens, repeat_penalty=chat_rep, rid=chat_rid, lane="main", effort=effort,
                        top_p=req.top_p, min_p=req.min_p, presence_penalty=req.presence_penalty, top_k=req.top_k, extra=sampler_extra(req),
                        **tc_kw)
                else:
                    chat_rep = deck_lim["repeat_penalty"] or (req.repeat_penalty if req.repeat_penalty is not None else 1.15)
                    chat_stream = _llm_chat_stream(main_client, msgs, tools=current_tools, temperature=req.temperature,
                                                   max_tokens=turn_max_tokens, repeat_penalty=chat_rep, rid=chat_rid, effort=effort,
                                                   top_p=req.top_p, min_p=req.min_p, presence_penalty=req.presence_penalty, top_k=req.top_k, extra=sampler_extra(req),
                                                   **tc_kw)
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
                        redactor = output_guard.OutputRedactor(user, False,
                                                               sentence_release=voice_policy.sentence_release(req))
                        think_redactor = output_guard.OutputRedactor(user, False)
                        continue
                    if ev == "thought_delta":
                        _t = think_redactor.feed(val)
                        if _t:
                            yield f"event: thought_delta\ndata: {json.dumps({'delta': _t})}\n\n"
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

                _tt = think_redactor.flush()
                if _tt:
                    yield f"event: thought_delta\ndata: {json.dumps({'delta': _tt})}\n\n"
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
                    redactor.reset()
                    # purge: the withheld answer must not be moved into the "thinking" block, and the
                    # thinking itself (which quoted the data) is dropped with it
                    yield "event: delta_reset\ndata: {\"purge\": true}\n\n"
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
                            res_str = (chat_write_file({"path": clean_fname, "content": cand_code})
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


                # the questionnaire card is the answer to a PDF / deck request: its JSON is not file content
                if (is_file_refusal or is_file_intent) and turn == 0 and not tool_calls and not serves_brief_card(content, brief_phase):

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
                        res_str = (chat_write_file({"path": target_filename, "content": data_to_save})
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
                # In the build phase the user has already answered the card: any text-only reply with no file yet (the card asked
                # again, the document dumped in a code fence, a question) is an undelivered build, which looks_undelivered misses
                # when the reply holds a fence.
                build_undelivered = brief_phase == "build" and not written_files
                doc_flow = DOC_CREATE_SCHEMA in chat_tools
                if (not tool_calls and chat_tools and not continued
                        and (missing_dl or build_undelivered or looks_undelivered(content, wants_file, bool(written_files)))):
                    continued = True
                    if turn >= turn_limit - 1:
                        turn_limit += 1
                    if streamed_content:
                        redactor.reset()
                        yield "event: delta_reset\ndata: {}\n\n"
                    msgs.append({"role": "assistant", "content": content or ""})
                    if doc_flow and build_undelivered:
                        nudge = ("[system note] The user's answers are already in the conversation: do not ask anything and do not "
                                 "write the document as text. Call doc_create now with the first part of the document as markdown "
                                 "(tables as markdown tables), then doc_append for the rest (done=true on the last call); if you "
                                 "already started it, call doc_append to continue.")
                    else:
                        nudge = ("[system note] You announced the deliverable but did not produce it. Output it now, "
                                 "complete, with no preamble: "
                                 + ("call write_file with the full file content (for HTML: a complete standalone "
                                    "document), then give the [DOWNLOAD: filename] link."
                                    if (wants_file or missing_dl) else "write the full answer.")
                                 + " Use only data from the conversation and tool results; mark unverified figures as estimates.")
                    msgs.append({"role": "user", "content": nudge})
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

                    if card and card_mode is not False and "```questionnaire" not in content:
                        content = card                      # the model did not produce the card: send ours instead
                        yield f"event: delta_replace\ndata: {json.dumps({'text': content})}\n\n"
                    _clean = scrub_server_paths(content)
                    if _clean != content:
                        content = _clean
                        yield f"event: delta_replace\ndata: {json.dumps({'text': content})}\n\n"
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
                        if args.get("_runaway"):
                            # the stream guard cut this call while it was looping: never run it, ask once more, then stop
                            runaway_hits += 1
                            res_str = RUNAWAY_MSG
                            if runaway_hits > int(chat_cfg.get("runaway_retries", 2)):
                                turn_limit = turn + 1
                                res_str += ". Stop retrying: summarise what is done so far in plain text"
                        elif not tool_allowed(t_name):
                            # text-parsed calls can name any tool; the schema filter isn't enough
                            res_str = f"error: tool '{t_name}' is not enabled for the active custom agent"
                        elif t_name in WEB_TOOL_NAMES and web_calls >= max_web_calls:
                            # text-emitted calls bypass the tools list; enforce the budget here too
                            res_str = ("error: web research budget used up - do not search again. "
                                       "Produce the final answer from the results you already have.")
                        elif t_name == "web_search":
                            q = args.get("query") or args.get("q") or ""
                            qk = kb_coverage.query_key(q)
                            if qk and qk in web_seen:
                                res_str = ("note: you already ran this search - use the earlier results or "
                                           "search for something different.")
                            else:
                                web_seen.add(qk)
                                res_str = kb_coverage.cap_web_result(await tool_web_search({"query": q}), web_calls, *deck_research.web_cap(brief_phase))
                        elif t_name == "web_search_images":
                            q = args.get("query") or args.get("q") or ""
                            res_str = await tool_web_search_images({"query": q})
                        elif t_name == "web_fetch":
                            u = args.get("url") or ""
                            res_str = kb_coverage.cap_web_result(await tool_web_fetch({"url": u}), web_calls, *deck_research.web_cap(brief_phase))
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
                        elif t_name == "write_file" and (_redirect := _doc_redirect.write_note(
                                t_name, args, {t.get("function", {}).get("name") for t in (current_tools or [])}, write_redirected)):
                            res_str = _redirect
                        elif t_name in ("write_file", "doc_create") and _duplicate_workbook(
                                prior_files + written_files, args, own_q):
                            res_str = _DUPLICATE_WORKBOOK.format(name=[f for f in prior_files + written_files if f.lower().endswith((".xlsx", ".xls"))][-1])
                        elif t_name in ("write_file", "doc_create") and prompt_scope.format_mismatch(
                                explicit_ext, args.get("path") or args.get("file") or args.get("filename")):
                            res_str = prompt_scope.format_mismatch(
                                explicit_ext, args.get("path") or args.get("file") or args.get("filename"))
                        elif t_name == "run_sandbox_python" and sandbox_on:
                            res_str = await _sandbox.tool_run_sandbox_python(args, user.id)
                            written_files.extend(f for f in _DL_TAG_RE.findall(res_str) if f not in written_files)
                        elif t_name in ("doc_inspect", "doc_edit", "doc_create", "doc_append"):
                            impl = _DOC_CHAT_IMPLS[t_name]
                            res_str = await run_in_executor_ctx(impl, args)
                            if res_str.startswith("doc error"):
                                res_str = "error: " + res_str
                            else:
                                written_files.extend(f for f in _DL_TAG_RE.findall(res_str) if f not in written_files)   # one deck, many doc_append steps
                        elif t_name == "write_file" and _wraps_media(
                                made_media, args.get("path") or args.get("file") or args.get("filename"), last_query):
                            res_str = _WRAP_REFUSAL
                        elif t_name == "write_file" and _already_written(written_files, args):
                            # the model keeps re-saving the same document (each save gets a fresh unique name, so it never
                            # sees "done"): say it is saved and end the research/write rounds
                            res_str = _ALREADY_SAVED.format(name=written_files[-1])
                            turn_limit = min(turn_limit, turn + 1)
                        elif t_name == "write_file":
                            # off the event loop: a deck / PDF written here may look up pictures (web, image model)
                            res_str = await run_in_executor_ctx(chat_write_file, args)
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

                    if not ok and t_name in chat_media_tools and "isn't loaded" in str(res_str):
                        # the image/video model is not running: stop offering the tool, or the model retries it every round
                        chat_tools[:] = [t for t in chat_tools if t.get("function", {}).get("name") != t_name]
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
                    final_rep = deck_lim["repeat_penalty"] or (req.repeat_penalty if req.repeat_penalty is not None else 1.15)
                    final_stream = common._llm_chat_stream_with_fallback(
                        main_client, fb_local, msgs, None, req.temperature,
                        turn_max_tokens, repeat_penalty=final_rep, rid=chat_rid, lane="main", effort=effort,
                        top_p=req.top_p, min_p=req.min_p, presence_penalty=req.presence_penalty, top_k=req.top_k, extra=sampler_extra(req))
                else:
                    final_rep = deck_lim["repeat_penalty"] or (req.repeat_penalty if req.repeat_penalty is not None else 1.15)
                    final_stream = _llm_chat_stream(main_client, msgs, tools=None, temperature=req.temperature,
                                                    max_tokens=turn_max_tokens, repeat_penalty=final_rep, rid=chat_rid, effort=effort,
                                                    top_p=req.top_p, min_p=req.min_p, presence_penalty=req.presence_penalty, top_k=req.top_k, extra=sampler_extra(req))

                async for ev, val in final_stream:
                    if ev == "queued":
                        yield f"event: queued\ndata: {json.dumps(val)}\n\n"
                        continue
                    if ev == "thought_delta":
                        _t = think_redactor.feed(val)
                        if _t:
                            yield f"event: thought_delta\ndata: {json.dumps({'delta': _t})}\n\n"
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

                _tt = think_redactor.flush()
                if _tt:
                    yield f"event: thought_delta\ndata: {json.dumps({'delta': _tt})}\n\n"
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

            if "<tool_call>" in content or "<function=" in content or "<function " in content:
                # the tool-free wrap-up turn tried to call a tool as text: never show that markup as the answer
                content = sanitize_user_facing_content(re.sub(r"<function[=\s][\s\S]*?</function>", "", content)) or _NO_TOOL_ANSWER
                yield f"event: delta_replace\ndata: {json.dumps({'text': content})}\n\n"

            if card and card_mode is not False and "```questionnaire" not in content:
                content = card
                yield f"event: delta_replace\ndata: {json.dumps({'text': content})}\n\n"
            _clean = scrub_server_paths(content)
            if _clean != content:
                content = _clean
                yield f"event: delta_replace\ndata: {json.dumps({'text': content})}\n\n"
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
            run_end_state = "cancelled"
        except Exception as e:
            run_end_state, run_end_reason = "failed", type(e).__name__
            print(f"[chat] request failed: {type(e).__name__}: {e}", file=sys.stderr)
            yield f"event: delta\ndata: {json.dumps({'text': friendly_chat_error(e)})}\n\n"
            yield f"event: done\ndata: {{}}\n\n"
        finally:
            runs.end(run_id, run_end_state, run_end_reason, step=turn)
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
                              source=model_source, provider=model_provider, user_id=user.id, run_id=run_id)

    return StreamingResponse(event_stream(), media_type="text/event-stream")

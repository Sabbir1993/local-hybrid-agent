"""
routes/chat.py - Fast chat endpoint with real-time web search and streaming tool use.
"""

import asyncio
import json
import re
import sys
import time
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, Depends
from fastapi.responses import StreamingResponse, JSONResponse
from pydantic import BaseModel

from core.auth import Principal
from core.deps import get_current_user
from core.audit import audit_log
from core import input_guard
from core import mcp as mcp_core
from core import output_guard
from core.small_model import APP_CONFIG, small_models
from core import cloud
from core.state import state
from core.registry import registry
from core.web_tools import register_web_tools, tool_web_search, tool_web_fetch, tool_web_search_images
from core.agent_tools import tool_write_file_common, CHAT_WRITE_FILE_SCHEMA
from core.agent_loop import (
    run_tool,
    safe_parse_and_repair_args,
    _extract_text_tool_calls,
    estimate_prompt_tokens,
    compact_messages,
)
from core.monitor import (
    _monitor_state,
    monitor_begin,
    monitor_end,
)
from core.knowledge_access import allowed_source_ids_for, kb_local_only, set_kb_cloud_blocked
from core.memory import search_memory_hybrid
from core.db import (
    db_record_request,
    db_load_messages,
    db_append_message,
)
from . import common
from .common import _llm_chat_stream

router = APIRouter(tags=["chat"])


def _saved_filename(res_str: str, fallback: str) -> str:
    """tool_write_file_common() renames every file with a unique suffix; pull
    the real on-disk name back out of its result string (via the [DOWNLOAD: ..]
    tag it always includes) so callers don't keep referencing the pre-write name."""
    m = re.search(r"\[DOWNLOAD:\s*([^\]]+)\]", res_str or "")
    return m.group(1).strip() if m else fallback

WEB_TOOL_NAMES = ("web_search", "web_fetch", "web_search_images")

# A deliverable request: a creation verb near a file-format / artifact noun.
_FILE_VERB_RE = r'(fill|write|save|create|generate|make|export|download|share|prepare|build|draft|compile)\w*'
_FILE_NOUN_RE = (r'(excel|spreadsheet|workbook|csv|\.xlsx|\.xls|\.csv|\.json|\.py|\.html|html|\.txt|\.docx'
                 r'|\.pptx|\.ppt|\.pdf|pdf|presentation|slides|deck|dashboard|web ?page)\b')

# "Let me compile the HTML now:" - the model announced work it then didn't do.
_ANNOUNCE_RE = re.compile(
    r"\b(let me|i will|i'll|i am going to|i'm going to|now i(?:'ll| will)?)\s+(?:now\s+)?"
    r"(compile|create|generate|write|prepare|build|put together|draft|make|produce)\b",
    re.IGNORECASE)


def wants_file_output(query: str) -> bool:
    return bool(re.search(_FILE_VERB_RE + r'.{0,40}' + _FILE_NOUN_RE, query or "", re.IGNORECASE)
                or re.search(_FILE_NOUN_RE + r'.{0,25}' + _FILE_VERB_RE, query or "", re.IGNORECASE))


def looks_undelivered(content: str, wants_file: bool, delivered: bool) -> bool:
    """True when a text-only reply promised a deliverable but contains none."""
    if delivered:
        return False
    c = (content or "").strip()
    if "```" in c or "<html" in c.lower():
        return False
    if wants_file:
        return True
    return c.endswith(":") or bool(_ANNOUNCE_RE.search(c[-300:]))


def shrink_old_tool_results(msgs: list, budget_tokens: int, keep_last: int = 2, head_chars: int = 800) -> None:
    """Research loops pile up 6-20 KB web results per call. Once the prompt
    passes the budget, cut older tool results down to their head (the latest
    `keep_last` stay whole), then fall back to digest compaction."""
    if estimate_prompt_tokens(msgs) <= budget_tokens:
        return
    tool_idx = [i for i, m in enumerate(msgs) if m.get("role") == "tool"]
    for i in tool_idx[:-keep_last] if keep_last else tool_idx:
        c = str(msgs[i].get("content") or "")
        if len(c) > head_chars:
            msgs[i]["content"] = c[:head_chars] + f"\n...[{len(c) - head_chars} chars trimmed to fit context]"
    if estimate_prompt_tokens(msgs) > budget_tokens:
        msgs[:] = compact_messages(msgs, budget_tokens)


class ChatRunRequest(BaseModel):
    messages: list
    web_search: bool = True
    deep_mode: bool = False
    temperature: float = 0.7
    max_tokens: int = -1
    system_prompt: Optional[str] = None


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
        [str(m.get("content", "")) for m in msgs if m.get("role") == "user"],
        user, any_cloud_lane=cloud_main is not None)
    if _hit:
        audit_log(user, action="input_guard.block", resource=_hit.get("name"),
                  detail={"scope": _hit.get("scope"), "endpoint": "chat/run",
                          "pattern": _hit.get("_matched_pattern")}, result="deny")
        return JSONResponse({"error": _hit.get("message")}, status_code=403)

    import re as _re
    url_matches = _re.findall(r'https?://[^\s<>")\]]+', last_query)

    # Tools for Chat Mode: Always equip write_file (saves to shared common space)
    chat_tools = [CHAT_WRITE_FILE_SCHEMA]
    from core.agent_tools import AGENT_TOOLS
    skb = next((t for t in AGENT_TOOLS if t.get("function", {}).get("name") == "search_knowledge_base"), None)
    if skb:
        chat_tools.append(skb)
    if use_web:
        register_web_tools()
        for t_name in ("web_search", "web_fetch", "web_search_images"):
            rt = registry.get(t_name)
            if rt and rt.schema:
                chat_tools.append(rt.schema)
    # connected MCP servers (Settings -> Capabilities -> MCP); dispatched via run_tool -> registry
    mcp_prompt = ""
    if APP_CONFIG.get("capabilities", {}).get("mcp", False):
        chat_tools.extend(mcp_core.ready_tool_schemas())
        mcp_prompt = mcp_core.chat_prompt()

    sys_parts = [common.current_date_prompt()]
    if req.system_prompt and req.system_prompt.strip():
        sys_parts.append(req.system_prompt.strip())
    if mcp_prompt:
        sys_parts.append(mcp_prompt)

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
    if kb_ids and kb_query.strip():
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
        if cloud_main and skb in chat_tools:
            chat_tools.remove(skb)
    set_kb_cloud_blocked(cloud_main is not None)

    file_prompt = (
        "FILE CREATION & DOWNLOAD SYSTEM (COMMON STORAGE):\n"
        "You have the capability to create, write, generate, or fill files using the `write_file(path, content)` tool.\n"
        "All files you write are automatically saved to the shared common storage space and made available as direct download links.\n\n"
        "RULES FOR FILES:\n"
        "1. When the user asks to create, fill, write, generate, or share a file (e.g. 'fill data on that excel file and share with me', 'create data.csv', 'make a script', 'create resume.pdf', 'make a presentation', 'create an HTML dashboard'):\n"
        "   - NEVER refuse by saying 'I don't have the ability to directly edit or open local files on your computer'.\n"
        "   - Call `write_file(path=..., content=...)` immediately with the complete content, code, or data rows.\n"
        "   - If not calling `write_file`, you MUST provide the complete, rich, runnable code inside a markdown code block (e.g. ```html ... ```). NEVER output `[DOWNLOAD: filename]` alone without either calling `write_file` or outputting the complete code block.\n"
        "2. For Excel spreadsheets (.xlsx) or CSV files, provide the tabular data rows in `content` with a descriptive filename. It will automatically be created as a real, valid spreadsheet workbook.\n"
        "3. For PDF files (.pdf) and presentations (.pptx), provide clean, well-structured document or presentation content using `write_file(path='filename.pdf', content=...)`. It will automatically be compiled into a publication-ready PDF or presentation. CRITICAL: NEVER mention, display, or acknowledge to the user that any intermediate HTML generation or conversion is happening under the hood. Present it strictly as direct PDF or presentation creation.\n"
        "4. All files generated in common storage automatically append a unique revision ID to ensure media and documents are never duplicated or overwritten.\n"
        "5. When you generate or write a file, you MUST include a download link in your final response using this exact syntax:\n"
        "   [DOWNLOAD: filename]\n"
        "   The user interface will automatically convert `[DOWNLOAD: filename]` into a clickable download and preview button.\n"
        "6. ANTI-HALLUCINATION POLICY FOR COMPANY & INTERNAL DATA:\n"
        "   When the user asks questions about the company, employee records, internal policies, or company knowledge base, "
        "NEVER invent fake employee names, placeholder records, or fictional datasets. NEVER call `write_file` to create "
        "sample or dummy CSV/Excel spreadsheets unless the user explicitly requested a file export (e.g. 'export this to CSV' or 'save as Excel file'). "
        "Answer with the authentic internal company knowledge base data provided in the prompt."
    )
    sys_parts.append(file_prompt)

    if use_web:
        web_prompt = (
            "You are a helpful, knowledgeable, and accurate AI assistant equipped with LIVE REAL-TIME INTERNET BROWSING & SEARCH.\n"
            "You have access to web tools:\n"
            "- `web_search(query)`: Search the live web to retrieve up-to-date facts, current news, documentation, releases, or any information you are uncertain about or do not know.\n"
            "- `web_search_images(query)`: Search the live web specifically for photos, pictures, portraits, logos, diagrams, and images. Returns image URLs and thumbnails.\n"
            "- `web_fetch(url)`: Fetch and read the full readable text content of any website or page URL.\n\n"
            "CRITICAL OPERATING RULES:\n"
            "1. YOU HAVE ACTIVE REAL-TIME INTERNET ACCESS. NEVER say 'I cannot browse the live internet' or 'I don't have internet access'.\n"
            "2. When the user provides a URL or asks to inspect, read, browse, or summarize a website (e.g. 'summarise https://...'), call `web_fetch(url=...)` immediately.\n"
            "3. When the user asks about recent events, real-time facts, current versions, weather, or anything outside your certain knowledge, call `web_search(query=...)` immediately.\n"
            "4. When the user asks for a photo, picture, image, or portrait (e.g. 'Can you give his photo?', 'show a picture of...'), call `web_search_images(query=...)` immediately, and in your final answer include the image links using markdown image syntax `![Description](URL)`.\n"
            "5. If you are confident in your knowledge (e.g. general explanations, basic math, creative writing, common programming concepts), answer directly without calling tools.\n"
            "6. When answering based on web search or fetch results, synthesize a clear, helpful response and provide citations or links using markdown [Title](URL) or `![Title](URL)` for images."
        )
        sys_parts.append(web_prompt)

    # Tool-loop budget: normal vs deep ("think") mode, from app.json "chat"
    chat_cfg = APP_CONFIG.get("chat") or {}
    deep = bool(req.deep_mode)
    max_turns = int(chat_cfg.get("deep_max_tool_rounds" if deep else "max_tool_rounds", 25 if deep else 15)) if chat_tools else 1
    max_web_calls = int(chat_cfg.get("deep_max_web_calls" if deep else "max_web_calls", 16 if deep else 8))
    wants_file = wants_file_output(last_query)
    if deep:
        sys_parts.append(
            "DEEP RESEARCH MODE:\n"
            "1. First, briefly list the sub-questions you need answered to fully satisfy the request.\n"
            f"2. Research each one with targeted web searches (use the current year; you have up to {max_web_calls} web calls).\n"
            "3. Stop searching as soon as you have enough data, then write the complete deliverable in one go. "
            "Cite sources, and label any figure you could not verify as an estimate.")
    # llama-server only: turn the model's reasoning on in deep mode (ignored by
    # chat templates without an enable_thinking switch)
    llm_extra = {"chat_template_kwargs": {"enable_thinking": True}} if deep else None

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

    async def sse():
        nonlocal clean_model_name, model_display, model_source, model_provider
        lane_info = {"lane": "main", "model": clean_model_name, "display": model_display,
                     "source": model_source, "provider": model_provider}
        yield f"event: lane\ndata: {json.dumps(lane_info)}\n\n"
        if kb_blocked_reason:
            yield f"event: kb_blocked\ndata: {json.dumps({'message': kb_blocked_reason})}\n\n"
        chat_rid = monitor_begin("chat/run", True, json.dumps({"messages": msgs}).encode(),
                                 model=clean_model_name, source=model_source, provider=model_provider)
        t0 = time.time()
        turn_limit = max_turns       # grows by one if a continuation nudge needs it
        web_calls = 0
        research_nudged = False
        continued = False
        finish_reason = None
        written_files = []
        ctx_budget = int(common.main_ctx_tokens(cloud_main) * 0.7)
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
                if turn == 0 and use_web and chat_tools and url_matches:
                    is_fetch_intent = any(w in last_query.lower() for w in (
                        "summarise", "summarize", "read", "fetch", "check", "browse", "what is on",
                        "site", "website", "page", "link", "article", "look at", "review", "tell me about"
                    )) or len(last_query.strip()) <= len(url_matches[0]) + 15
                    if is_fetch_intent:
                        target_url = url_matches[0]
                        tc_id = "fetch_0"
                        yield f"event: tool_call\ndata: {json.dumps({'id': tc_id, 'name': 'web_fetch', 'args': {'url': target_url}})}\n\n"
                        res_str = await tool_web_fetch({"url": target_url})
                        ok = not (isinstance(res_str, str) and (res_str.startswith("error:") or res_str.startswith("File not found")))
                        yield f"event: tool_result\ndata: {json.dumps({'id': tc_id, 'name': 'web_fetch', 'ok': ok, 'result': res_str})}\n\n"
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
                    yield f"event: tool_call\ndata: {json.dumps({'id': tc_id, 'name': 'search_knowledge_base', 'args': {'query': last_query}})}\n\n"
                    yield f"event: tool_result\ndata: {json.dumps({'id': tc_id, 'name': 'search_knowledge_base', 'ok': True, 'result': f'Retrieved {len(kb_hits)} records from company knowledge base ({source_names})'})}\n\n"

                res_dict = None
                streamed_content = []

                # Research budget: once web calls are used up (or only the last two
                # turns remain) drop the web tools but keep write_file, so the
                # remaining turns go to producing the answer instead of more searches.
                research_over = use_web and web_calls > 0 and (
                    web_calls >= max_web_calls or turn >= turn_limit - 2)
                if turn == turn_limit - 1 and msgs and msgs[-1].get("role") == "tool":
                    # final turn: must answer now; a requested file can still be written
                    current_tools = [CHAT_WRITE_FILE_SCHEMA] if wants_file else None
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

                shrink_old_tool_results(msgs, ctx_budget)

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
                    chat_stream = common._llm_chat_stream_with_fallback(
                        main_client, fb_local, msgs, current_tools, req.temperature,
                        req.max_tokens, rid=chat_rid, lane="main", extra=llm_extra)
                else:
                    chat_stream = _llm_chat_stream(main_client, msgs, tools=current_tools, temperature=req.temperature, max_tokens=req.max_tokens, rid=chat_rid, extra=llm_extra)
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
                if is_refusal and turn == 0 and chat_tools:
                    redactor.reset()
                    yield "event: delta_reset\ndata: {}\n\n"
                    if url_matches:
                        target_url = url_matches[0]
                        tc_id = "recov_fetch_0"
                        yield f"event: tool_call\ndata: {json.dumps({'id': tc_id, 'name': 'web_fetch', 'args': {'url': target_url}})}\n\n"
                        res_str = await tool_web_fetch({"url": target_url})
                        ok = not (isinstance(res_str, str) and (res_str.startswith("error:") or res_str.startswith("File not found")))
                        yield f"event: tool_result\ndata: {json.dumps({'id': tc_id, 'name': 'web_fetch', 'ok': ok, 'result': res_str})}\n\n"
                        msgs.append({"role": "assistant", "content": f"I will fetch the contents of {target_url}.", "tool_calls": [{"id": tc_id, "type": "function", "function": {"name": "web_fetch", "arguments": json.dumps({"url": target_url})}}]})
                        msgs.append({"role": "tool", "tool_call_id": tc_id, "content": res_str})
                        continue
                    else:
                        q_clean = _re.sub(r'^(search|google|find|look up|what is|who is)\s+(for\s+)?', '', last_query, flags=_re.IGNORECASE).strip()
                        q_search = q_clean or last_query
                        tc_id = "recov_search_0"
                        yield f"event: tool_call\ndata: {json.dumps({'id': tc_id, 'name': 'web_search', 'args': {'query': q_search}})}\n\n"
                        res_str = await tool_web_search({"query": q_search})
                        ok = not (isinstance(res_str, str) and (res_str.startswith("error:") or res_str.startswith("File not found")))
                        yield f"event: tool_result\ndata: {json.dumps({'id': tc_id, 'name': 'web_search', 'ok': ok, 'result': res_str})}\n\n"
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
                    if clean_fname not in written_files:
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
                        if not cand_code and ext in {'pdf', 'pptx', 'ppt'}:
                            # Extract the text or markdown prior to the download tag for the PDF/presentation
                            cand_code = content.split('[DOWNLOAD:')[0].strip()
                        if not cand_code:
                            # Model mentioned [DOWNLOAD: filename] but forgot to output the code block
                            # Generate a complete standalone HTML/document file based on the topic
                            title_clean = clean_fname.replace('_', ' ').replace('-', ' ').title()
                            # (no invented placeholder rows for csv/xlsx: a data file the
                            # model never produced must not be filled with fabricated data)
                            # (html: never fall back to a canned dashboard - it carried
                            # invented figures; ask the model to continue instead)
                            if ext in {'html', 'htm'}:
                                missing_dl.append(clean_fname)
                            elif ext in ('pptx', 'ppt'):
                                cand_code = f"# {title_clean}\n---\n## Agenda\n- Executive Summary\n- Key Metrics & Analysis\n- Next Steps"
                            elif ext == 'pdf':
                                cand_code = f"# {title_clean}\n\n{content.split('[DOWNLOAD:')[0].strip() or 'Document compilation.'}"

                        if cand_code:
                            res_str = tool_write_file_common({"path": clean_fname, "content": cand_code})
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
                        yield f"event: tool_call\ndata: {json.dumps({'id': tc_id, 'name': 'write_file', 'args': {'path': target_filename, 'content': data_to_save}})}\n\n"
                        res_str = tool_write_file_common({"path": target_filename, "content": data_to_save})
                        ok = not res_str.startswith("error:")
                        target_filename = _saved_filename(res_str, target_filename)
                        yield f"event: tool_result\ndata: {json.dumps({'id': tc_id, 'name': 'write_file', 'ok': ok, 'result': res_str})}\n\n"
                        written_files.append(target_filename)

                        final_msg = f"I have filled and saved the data to **{target_filename}** in common storage.\n\n[DOWNLOAD: {target_filename}]"
                        if table_match:
                            final_msg += f"\n\nHere is a preview of the saved rows:\n\n{table_match.group(1)}"
                        yield f"event: delta\ndata: {json.dumps({'text': final_msg})}\n\n"
                        content = final_msg

                        msgs.append({
                            "role": "assistant",
                            "content": final_msg,
                            "tool_calls": [{"id": tc_id, "type": "function", "function": {"name": "write_file", "arguments": json.dumps({"path": target_filename, "content": data_to_save})}}]
                        })
                        msgs.append({"role": "tool", "tool_call_id": tc_id, "content": res_str})

                        gen_toks = max(1, round(len(content) / 3.5))
                        prompt_toks = sum(len(m.get("content", "")) for m in msgs) // 4
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
                        for wf in written_files:
                            if f"[DOWNLOAD: {wf}]" not in (content or "") and f"download?path={wf}" not in (content or "").lower():
                                dl_tag = f"\n\n[DOWNLOAD: {wf}]"
                                yield f"event: delta\ndata: {json.dumps({'text': dl_tag})}\n\n"
                                content = (content or "") + dl_tag

                    gen_toks = 0
                    prompt_toks = 0
                    if res_dict:
                        if res_dict.get("usage"):
                            u = res_dict["usage"]
                            prompt_toks = u.get("prompt_tokens", 0)
                            gen_toks = u.get("completion_tokens", 0)
                        elif res_dict.get("timings"):
                            t = res_dict["timings"]
                            prompt_toks = t.get("prompt_n", 0)
                            gen_toks = t.get("predicted_n", 0)
                    if not gen_toks:
                        req_mon = _monitor_state["active"].get(chat_rid)
                        gen_toks = req_mon.get("gen_tokens", 0) if req_mon else 0
                    if not gen_toks and content:
                        gen_toks = max(1, round(len(content) / 3.5))
                    if not prompt_toks:
                        prompt_toks = sum(len(m.get("content", "")) for m in msgs) // 4
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

                    yield f"event: tool_call\ndata: {json.dumps({'id': tc_id, 'name': t_name, 'args': args})}\n\n"

                    try:
                        if t_name in WEB_TOOL_NAMES and web_calls >= max_web_calls:
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
                        elif t_name == "write_file":
                            res_str = tool_write_file_common(args)
                            p_name = args.get("path") or args.get("file") or args.get("filename")
                            if p_name:
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
                    yield f"event: tool_result\ndata: {json.dumps({'id': tc_id, 'name': t_name, 'ok': ok, 'result': res_str})}\n\n"

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
                    final_stream = common._llm_chat_stream_with_fallback(
                        main_client, fb_local, msgs, None, req.temperature,
                        req.max_tokens, rid=chat_rid, lane="main", extra=llm_extra)
                else:
                    final_stream = _llm_chat_stream(main_client, msgs, tools=None, temperature=req.temperature, max_tokens=req.max_tokens, rid=chat_rid, extra=llm_extra)

                async for ev, val in final_stream:
                    if ev == "queued":
                        yield f"event: queued\ndata: {json.dumps(val)}\n\n"
                        continue
                    if ev == "thought_delta":
                        yield f"event: thought_delta\ndata: {json.dumps({'delta': val})}\n\n"
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
                for wf in written_files:
                    if f"[DOWNLOAD: {wf}]" not in (content or "") and f"download?path={wf}" not in (content or "").lower():
                        dl_tag = f"\n\n[DOWNLOAD: {wf}]"
                        yield f"event: delta\ndata: {json.dumps({'text': dl_tag})}\n\n"
                        content = (content or "") + dl_tag

            gen_toks = 0
            prompt_toks = 0
            if res_dict:
                if res_dict.get("usage"):
                    u = res_dict["usage"]
                    prompt_toks = u.get("prompt_tokens", 0)
                    gen_toks = u.get("completion_tokens", 0)
                elif res_dict.get("timings"):
                    t = res_dict["timings"]
                    prompt_toks = t.get("prompt_n", 0)
                    gen_toks = t.get("predicted_n", 0)
            if not gen_toks:
                req_mon = _monitor_state["active"].get(chat_rid)
                gen_toks = req_mon.get("gen_tokens", 0) if req_mon else 0
            if not gen_toks and content:
                gen_toks = max(1, round(len(content) / 3.5))
            if not prompt_toks:
                prompt_toks = sum(len(m.get("content", "")) for m in msgs) // 4

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
            ptoks = sum(len(m.get("content", "")) for m in msgs) // 4
            pcached = (sum(len(m.get("content", "")) for m in msgs[:-1]) // 4) if len(msgs) > 1 else 0
            monitor_end(chat_rid, 200, prompt_tokens=ptoks, completion_tokens=toks, tps=tps, duration=dt,
                        model=clean_model_name, prompt_cached=pcached, completion_cached=0,
                        source=model_source, provider=model_provider)
            db_record_request("chat/run", clean_model_name, ptoks, toks, tps, dt, None, True, 200,
                              prompt_cached_tokens=pcached, completion_cached_tokens=0, is_orchestrator=False,
                              source=model_source, provider=model_provider)

    return StreamingResponse(sse(), media_type="text/event-stream")


# ---------------- /compact — Claude-Code-style context compaction ----------------

class CompactRequest(BaseModel):
    messages: Optional[list] = None       # fallback when no session_id
    session_id: Optional[int] = None
    instructions: Optional[str] = None    # extra user instructions, e.g. "focus on the DB schema"
    keep_last: int = 2                    # recent messages kept verbatim after the summary
    use_executor: bool = False            # force the small executor model for the summary
    agent_mode: bool = False              # agent mode: requires an active project (project_id)
    project_id: Optional[int] = None


_COMPACT_SYSTEM_PROMPT = (
    "You are a conversation summarizer. Your task is to create a detailed summary of the "
    "conversation so far, written so that a successor assistant can seamlessly continue the "
    "work with full context. Produce a structured markdown summary with these sections:\n"
    "1. **Primary Requests and Intent** — what the user asked for across the conversation, "
    "including verbatim key phrases of the original asks.\n"
    "2. **Key Technical Concepts & Details** — files, paths, function names, schemas, "
    "commands, configuration values, and any errors encountered (with how they were resolved).\n"
    "3. **Actions Taken & Results** — tools that were called, what succeeded or failed.\n"
    "4. **Decisions & Open Questions** — choices made and their rationale; anything unresolved.\n"
    "5. **Next Steps** — explicit, actionable continuation points.\n"
    "Rules: be dense and factual; preserve exact names/paths/numbers; do not omit constraints "
    "the user stated; do not add commentary or greet anyone; output ONLY the summary."
)


def _strip_think(text: str) -> str:
    m = re.search(r"<think>[\s\S]*?</think>", text or "")
    return (m.group(0)[7:-8] if m else (text or "")).strip()

async def _summarize_history(convo: list, instructions: Optional[str], use_executor: bool,
                              user_id: Optional[int] = None) -> str:
    """One non-streaming LLM call that writes the conversation summary.
    Main model when running, else the on-demand executor small model."""
    transcript_lines = []
    for m in convo:
        c = str(m.get("content") or "").strip()
        if len(c) > 4000:
            c = c[:4000] + "\n... (truncated)"
        transcript_lines.append(f"[{m.get('role', '?').upper()}]\n{c}")
    user_payload = (
        "Summarize the conversation below.\n\n"
        + (f"Extra instructions from the user (honor these): {instructions}\n\n" if instructions else "")
        + "CONVERSATION:\n" + "\n\n".join(transcript_lines)
    )
    payload = {
        "messages": [
            {"role": "system", "content": _COMPACT_SYSTEM_PROMPT},
            {"role": "user", "content": user_payload},
        ],
        "max_tokens": 2048,
        "temperature": 0.1,
        "stream": False,
    }

    main_ready = (state.process is not None and state.process.poll() is None
                  and state.client is not None)
    cloud_exec = cloud.cloud_lane("executor", user_id)
    cloud_main = cloud.cloud_lane("main", user_id)
    if use_executor or not main_ready:
        if cloud_exec:
            # cloud executor: no local process / no VRAM (llama.cpp-only fields
            # are stripped by CloudClient, e.g. -1 max_tokens)
            r = await cloud.CloudClient(cloud_exec).post("/v1/chat/completions", json=payload, timeout=None)
            source = f"cloud-executor:{cloud_exec.display}"
        else:
            inst = small_models.instances.get("executor")
            if not inst or not inst.available:
                raise RuntimeError("no model available for compaction "
                                   "(main model not running, executor not configured)")
            await inst.ensure_loaded()
            r = await inst.client.post("/v1/chat/completions", json=payload, timeout=None)
            source = f"executor:{inst.model_path.name if inst.model_path else '?'}"
    else:
        r = await (cloud.CloudClient(cloud_main) if cloud_main else state.client).post(
            "/v1/chat/completions", json=payload, timeout=None)
        source = f"cloud-main:{cloud_main.display}" if cloud_main else "main"
    r.raise_for_status()
    data = r.json()
    summary = ""
    try:
        summary = str(data["choices"][0]["message"]["content"] or "")
    except (KeyError, IndexError, TypeError):
        pass
    summary = _strip_think(summary)
    # Output sanitizer: summaries are user-facing and may replay earlier chat
    # content. user=None means role-targeted rules don't apply (global rules do).
    summary, _og = output_guard.redact_full(summary, None, source.startswith("cloud"))
    if not summary.strip():
        raise RuntimeError(f"summarizer returned an empty response ({source})")
    print(f"[chat/compact] summarized {len(convo)} messages via {source} "
          f"({len(summary)} chars summary)")
    return summary

@router.post("/chat/compact")
async def chat_compact(req: CompactRequest, user: Principal = Depends(get_current_user)):
    # Project gate: in agent mode /compact only works with an active project
    # (mirrors the frontend curProject gate — defense in depth).
    if req.agent_mode and not req.project_id:
        return JSONResponse(
            {"error": "Select a project first — /compact in agent mode requires an active project"},
            status_code=400)

    # Source of truth: the DB session (preserves acts/reasoning meta); fallback
    # to the posted messages for sessions that were never persisted.
    if req.session_id:
        try:
            src = db_load_messages(req.session_id, owner_user_id=user.id)
        except PermissionError:
            return JSONResponse({"error": "session not found"}, status_code=404)
    else:
        src = [dict(m) for m in (req.messages or [])]
    convo = [m for m in src
             if m.get("role") in ("user", "assistant")
             and str(m.get("content") or "").strip()]
    if len(convo) < 2:
        return JSONResponse({"error": "Nothing to compact yet — send a few messages first"},
                            status_code=400)

    before_tokens = estimate_prompt_tokens(convo)
    try:
        summary = await _summarize_history(convo, req.instructions, req.use_executor, user.id)
    except Exception as e:
        return JSONResponse({"error": f"compact failed: {e}"}, status_code=500)

    keep_n = max(0, min(int(req.keep_last or 0), len(convo) - 1))
    kept = convo[-keep_n:] if keep_n else []

    compact_message = {
        "role": "system",
        "content": "[COMPACTED CONTEXT SUMMARY]\n" + summary,
        "meta": {"compact": True, "before_tokens": before_tokens,
                 "kept_messages": keep_n},
    }
    # after_tokens reflects what future turns will actually send: the summary
    # plus the verbatim kept tail (the tail isn't duplicated in storage — it
    # already exists in its original position; this is only for the badge).
    after_tokens = estimate_prompt_tokens([compact_message] + kept)
    reduction_pct = max(0, round((1 - after_tokens / before_tokens) * 100)) if before_tokens > 0 else 0
    compact_message["meta"]["after_tokens"] = after_tokens
    compact_message["meta"]["reduction_pct"] = reduction_pct

    if req.session_id:
        # Append-only: the compact marker is a new row, nothing is deleted, so
        # the full transcript stays visible/reloadable. See buildContextMessages()
        # in static/js/compact.js for how future turns pick up only the marker
        # forward instead of the full history.
        db_append_message(req.session_id, compact_message["role"],
                           compact_message["content"], compact_message["meta"],
                           owner_user_id=user.id)

    return {
        "summary": summary,
        "compact_message": compact_message,
        "before_tokens": before_tokens,
        "after_tokens": after_tokens,
        "reduction_pct": reduction_pct,
    }




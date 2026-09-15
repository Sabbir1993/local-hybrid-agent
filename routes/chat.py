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

from fastapi import APIRouter
from fastapi.responses import StreamingResponse, JSONResponse
from pydantic import BaseModel

from core.small_model import APP_CONFIG
from core.state import state
from core.registry import registry
from core.web_tools import register_web_tools, tool_web_search, tool_web_fetch
from core.agent_loop import (
    run_tool,
    safe_parse_and_repair_args,
    _extract_text_tool_calls,
)
from core.monitor import (
    _monitor_state,
    monitor_begin,
    monitor_end,
)
from core.db import db_record_request
from . import common
from .common import _llm_chat_stream

router = APIRouter(tags=["chat"])

class ChatRunRequest(BaseModel):
    messages: list
    web_search: bool = True
    temperature: float = 0.7
    max_tokens: int = 4096
    system_prompt: Optional[str] = None


@router.post("/chat/run")
async def chat_run(req: ChatRunRequest):
    main_ready = (state.process is not None and state.process.poll() is None and state.client is not None)
    if not main_ready:
        target = state.profile_path or state.profile or common.initial_profile_path
        if target:
            try:
                print(f"[server_manager] chat/run: model not running — auto-starting...")
                await state.load_profile(target)
                main_ready = (state.process is not None and state.process.poll() is None and state.client is not None)
            except Exception as e:
                print(f"[server_manager] auto-start main model failed: {e}", file=sys.stderr)
    if not main_ready:
        return JSONResponse({"error": "No model loaded. Please load or start a model from the top toolbar first."}, status_code=400)

    web_cap_enabled = APP_CONFIG.get("capabilities", {}).get("web", False)
    use_web = req.web_search and web_cap_enabled

    msgs = [dict(m) for m in req.messages]
    last_query = ""
    for m in reversed(msgs):
        if m.get("role") == "user":
            last_query = str(m.get("content", ""))
            break

    import re as _re
    url_matches = _re.findall(r'https?://[^\s<>")\]]+', last_query)

    chat_tools = None
    if use_web:
        register_web_tools()
        tools_list = []
        for t_name in ("web_search", "web_fetch"):
            rt = registry.get(t_name)
            if rt and rt.schema:
                tools_list.append(rt.schema)
        if tools_list:
            chat_tools = tools_list

    sys_parts = []
    if req.system_prompt and req.system_prompt.strip():
        sys_parts.append(req.system_prompt.strip())

    if use_web and chat_tools:
        web_prompt = (
            "You are a helpful, knowledgeable, and accurate AI assistant equipped with LIVE REAL-TIME INTERNET BROWSING & SEARCH.\n"
            "You have access to web tools:\n"
            "- `web_search(query)`: Search the live web (DuckDuckGo) to retrieve up-to-date facts, current news, documentation, releases, or any information you are uncertain about or do not know.\n"
            "- `web_fetch(url)`: Fetch and read the full readable text content of any website or page URL.\n\n"
            "CRITICAL OPERATING RULES:\n"
            "1. YOU HAVE ACTIVE REAL-TIME INTERNET ACCESS. NEVER say 'I cannot browse the live internet' or 'I don't have internet access'.\n"
            "2. When the user provides a URL or asks to inspect, read, browse, or summarize a website (e.g. 'summarise https://...'), call `web_fetch(url=...)` immediately.\n"
            "3. When the user asks about recent events, real-time facts, current versions, weather, or anything outside your certain knowledge, call `web_search(query=...)` immediately.\n"
            "4. If you are confident in your knowledge (e.g. general explanations, basic math, creative writing, common programming concepts), answer directly without calling tools.\n"
            "5. When answering based on web search or fetch results, synthesize a clear, helpful response and provide citations or links using markdown [Title](URL)."
        )
        sys_parts.append(web_prompt)

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

    m_name = state.profile.get("model_path", "") if state.profile else ""
    clean_model_name = Path(m_name).name.replace(".gguf", "") if m_name else "Main LLM"

    async def sse():
        chat_rid = monitor_begin("chat/run", True, json.dumps({"messages": msgs}).encode(), model=clean_model_name)
        t0 = time.time()
        max_turns = 4 if chat_tools else 1

        try:
            for turn in range(max_turns):
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

                res_dict = None
                streamed_content = []

                async for ev, val in _llm_chat_stream(state.client, msgs, tools=chat_tools, temperature=req.temperature, max_tokens=req.max_tokens, rid=chat_rid):
                    if ev == "thought_delta":
                        yield f"event: thought_delta\ndata: {json.dumps({'delta': val})}\n\n"
                    elif ev == "content_delta":
                        streamed_content.append(val)
                        yield f"event: delta\ndata: {json.dumps({'text': val})}\n\n"
                    elif ev == "result":
                        res_dict = val

                content = res_dict.get("content", "") if res_dict else "".join(streamed_content)
                reasoning = res_dict.get("reasoning", "") if res_dict else ""
                tool_calls = res_dict.get("tool_calls", []) if res_dict else []

                # Fallback: check if text tool calls were emitted
                if not tool_calls:
                    parsed_tc = _extract_text_tool_calls(content or reasoning)
                    if parsed_tc:
                        tool_calls = parsed_tc

                # Auto-recovery: if model emitted a refusal saying it can't browse the internet
                is_refusal = any(w in content.lower() for w in (
                    "i can't browse the live internet", "i cannot browse the live internet",
                    "i can't browse", "i cannot browse", "i don't have internet", "i lack internet",
                    "as an ai, i cannot access", "as an ai, i can't access", "i can't fetch the exact current content"
                ))
                if is_refusal and turn == 0 and chat_tools:
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

                if not tool_calls or not chat_tools:
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
                        if t_name == "web_search":
                            q = args.get("query") or args.get("q") or ""
                            res_str = await tool_web_search({"query": q})
                        elif t_name == "web_fetch":
                            u = args.get("url") or ""
                            res_str = await tool_web_fetch({"url": u})
                        else:
                            res_str = await run_tool(t_name, args)
                        ok = not (isinstance(res_str, str) and (res_str.startswith("error:") or res_str.startswith("File not found")))
                    except Exception as e:
                        res_str = f"error: {e}"
                        ok = False

                    yield f"event: tool_result\ndata: {json.dumps({'id': tc_id, 'name': t_name, 'ok': ok, 'result': res_str})}\n\n"

                    clean_tool_calls.append({
                        "id": tc_id,
                        "type": "function",
                        "function": {"name": t_name, "arguments": json.dumps(args)}
                    })
                    tool_results_list.append((tc_id, res_str))

                msgs.append({"role": "assistant", "content": content or "", "tool_calls": clean_tool_calls})
                for tid, r_out in tool_results_list:
                    msgs.append({"role": "tool", "tool_call_id": tid, "content": r_out})

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
            dt = time.time() - t0
            req_mon = _monitor_state["active"].get(chat_rid)
            toks = req_mon.get("gen_tokens") if req_mon else None
            tps = (toks / dt) if (toks and dt and dt > 0) else None
            ptoks = sum(len(m.get("content", "")) for m in msgs) // 4
            pcached = (sum(len(m.get("content", "")) for m in msgs[:-1]) // 4) if len(msgs) > 1 else 0
            monitor_end(chat_rid, 200, prompt_tokens=ptoks, completion_tokens=toks, tps=tps, duration=dt,
                        model=clean_model_name, prompt_cached=pcached, completion_cached=0)
            db_record_request("chat/run", clean_model_name, ptoks, toks, tps, dt, None, True, 200,
                              prompt_cached_tokens=pcached, completion_cached_tokens=0, is_orchestrator=False)

    return StreamingResponse(sse(), media_type="text/event-stream")




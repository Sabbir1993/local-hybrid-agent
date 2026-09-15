"""
routes/common.py - Shared state and LLM streaming utilities across routes.
"""

import json
from pathlib import Path
from typing import Optional

from core.monitor import monitor_token
from core.agent_loop import safe_parse_and_repair_args

# Global CLI runtime overrides
initial_profile_path: Optional[Path] = None
models_dir: Optional[Path] = None
curStatus_model_hint: Optional[str] = None

async def _process_sse_stream(response, rid: Optional[int] = None):
    content_acc = []
    reasoning_acc = []
    accumulated_tcs = {}
    in_think_tag = False

    last_usage = None
    last_timings = None
    async for line in response.aiter_lines():
        line = line.strip()
        if not line or not line.startswith("data: "):
            continue
        raw = line[6:].strip()
        if raw == "[DONE]":
            break
        try:
            data = json.loads(raw)
        except Exception:
            continue

        if isinstance(data.get("usage"), dict):
            last_usage = data["usage"]
        if isinstance(data.get("timings"), dict):
            last_timings = data["timings"]

        choices = data.get("choices") or []
        if not choices:
            continue
        delta = choices[0].get("delta") or {}

        r_chunk = delta.get("reasoning_content")
        if r_chunk:
            reasoning_acc.append(r_chunk)
            if rid:
                monitor_token(rid, 1)
            yield ("thought_delta", r_chunk)

        c_chunk = delta.get("content")
        if c_chunk:
            if "<think>" in c_chunk:
                in_think_tag = True
                parts = c_chunk.split("<think>", 1)
                if parts[0]:
                    content_acc.append(parts[0])
                    if rid:
                        monitor_token(rid, 1)
                    yield ("content_delta", parts[0])
                if len(parts) > 1 and parts[1]:
                    if "</think>" in parts[1]:
                        in_think_tag = False
                        th_part, post = parts[1].split("</think>", 1)
                        reasoning_acc.append(th_part)
                        if rid:
                            monitor_token(rid, 1)
                        yield ("thought_delta", th_part)
                        if post:
                            content_acc.append(post)
                            if rid:
                                monitor_token(rid, 1)
                            yield ("content_delta", post)
                    else:
                        reasoning_acc.append(parts[1])
                        if rid:
                            monitor_token(rid, 1)
                        yield ("thought_delta", parts[1])
            elif "</think>" in c_chunk and in_think_tag:
                in_think_tag = False
                th_part, post = c_chunk.split("</think>", 1)
                if th_part:
                    reasoning_acc.append(th_part)
                    if rid:
                        monitor_token(rid, 1)
                    yield ("thought_delta", th_part)
                if post:
                    content_acc.append(post)
                    if rid:
                        monitor_token(rid, 1)
                    yield ("content_delta", post)
            elif in_think_tag:
                reasoning_acc.append(c_chunk)
                if rid:
                    monitor_token(rid, 1)
                yield ("thought_delta", c_chunk)
            else:
                content_acc.append(c_chunk)
                if rid:
                    monitor_token(rid, 1)
                yield ("content_delta", c_chunk)

        tcs = delta.get("tool_calls") or []
        for tc in tcs:
            idx = tc.get("index", 0)
            if idx not in accumulated_tcs:
                accumulated_tcs[idx] = {
                    "id": tc.get("id") or f"call_{idx}",
                    "type": "function",
                    "function": {"name": "", "arguments": ""}
                }
            fn = tc.get("function") or {}
            if fn.get("name"):
                accumulated_tcs[idx]["function"]["name"] += fn["name"]
                if rid:
                    monitor_token(rid, 1)
            if fn.get("arguments"):
                accumulated_tcs[idx]["function"]["arguments"] += fn["arguments"]
                if rid:
                    # Estimate generated token count from character chunks
                    chunk_toks = max(1, len(fn["arguments"]) // 4)
                    monitor_token(rid, chunk_toks)

    tool_calls = [accumulated_tcs[k] for k in sorted(accumulated_tcs.keys())]
    full_content = "".join(content_acc)
    full_reasoning = "".join(reasoning_acc)
    yield ("result", {
        "content": full_content,
        "reasoning": full_reasoning,
        "tool_calls": tool_calls,
        "usage": last_usage,
        "timings": last_timings,
    })




async def _llm_chat_stream(client_or_state, msgs: list, tools=None, temperature=0.4, max_tokens=-1, repeat_penalty=1.15, rid: Optional[int] = None):
    payload = {
        "messages": msgs,
        "temperature": temperature,
        "repeat_penalty": repeat_penalty,
        "stream": True,
    }
    if max_tokens is not None and int(max_tokens) > 0:
        payload["max_tokens"] = int(max_tokens)
    else:
        payload["max_tokens"] = -1
    if tools:
        payload["tools"] = tools

    async with client_or_state.stream("POST", "/v1/chat/completions", json=payload, timeout=None) as response:
        if response.status_code != 200:
            err_text = await response.aread()
            err_msg = err_text.decode("utf-8", "replace")[:300]
            if response.status_code == 500 and "Failed to parse tool call arguments as JSON" in err_msg:
                sanitized_msgs = []
                for m in msgs:
                    m_copy = dict(m)
                    if m_copy.get("tool_calls"):
                        new_tcs = []
                        for tc in m_copy["tool_calls"]:
                            tc_c = dict(tc)
                            fn_c = dict(tc_c.get("function", {}))
                            raw_a = fn_c.get("arguments", "{}")
                            if isinstance(raw_a, str):
                                try:
                                    json.loads(raw_a)
                                except Exception:
                                    fn_c["arguments"] = json.dumps(safe_parse_and_repair_args(raw_a))
                            else:
                                fn_c["arguments"] = json.dumps(raw_a)
                            tc_c["function"] = fn_c
                            new_tcs.append(tc_c)
                        m_copy["tool_calls"] = new_tcs
                    sanitized_msgs.append(m_copy)
                payload["messages"] = sanitized_msgs
                async with client_or_state.stream("POST", "/v1/chat/completions", json=payload, timeout=None) as retry_resp:
                    if retry_resp.status_code != 200:
                        re_err = await retry_resp.aread()
                        payload_notools = dict(payload)
                        payload_notools.pop("tools", None)
                        try:
                            async with client_or_state.stream("POST", "/v1/chat/completions", json=payload_notools, timeout=None) as fb_resp:
                                if fb_resp.status_code == 200:
                                    async for item in _process_sse_stream(fb_resp, rid=rid):
                                        yield item
                                    return
                        except Exception:
                            pass
                        raise RuntimeError(f"upstream {retry_resp.status_code}: {re_err.decode('utf-8', 'replace')[:200]}")
                    async for item in _process_sse_stream(retry_resp, rid=rid):
                        yield item
                return
            raise RuntimeError(f"upstream {response.status_code}: {err_msg}")

        async for item in _process_sse_stream(response, rid=rid):
            yield item




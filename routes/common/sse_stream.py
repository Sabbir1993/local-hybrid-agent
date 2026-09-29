import json
import re
import time
from typing import Optional
from core.monitor import monitor_token

from .think_splitter import ThinkSplitter, _emit_split


async def _process_sse_stream(response, rid: Optional[int] = None):
    content_acc = []
    reasoning_acc = []
    accumulated_tcs = {}
    think = ThinkSplitter()

    last_usage = None
    last_timings = None
    finish_reason = None
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
        if choices[0].get("finish_reason"):
            finish_reason = choices[0]["finish_reason"]
        delta = choices[0].get("delta") or {}

        r_chunk = delta.get("reasoning_content")
        if r_chunk:
            reasoning_acc.append(r_chunk)
            if rid:
                monitor_token(rid, 1)
            yield ("thought_delta", r_chunk)

        c_chunk = delta.get("content")
        if c_chunk:
            for item in _emit_split(think, think.feed(c_chunk), content_acc, reasoning_acc, rid):
                yield item

        tcs = delta.get("tool_calls") or []
        for tc in tcs:
            idx = tc.get("index", 0)
            if idx not in accumulated_tcs:
                accumulated_tcs[idx] = {
                    "id": tc.get("id") or f"call_{idx}",
                    "type": "function",
                    "function": {"name": "", "arguments": ""},
                    "_stream_meta": {"last_yield": 0, "path": ""}
                }
            fn = tc.get("function") or {}
            fn_name_chunk = fn.get("name")
            if fn_name_chunk:
                accumulated_tcs[idx]["function"]["name"] += fn_name_chunk
                if rid:
                    monitor_token(rid, 1)
            fn_args_chunk = fn.get("arguments")
            if fn_args_chunk:
                accumulated_tcs[idx]["function"]["arguments"] += fn_args_chunk
                if rid:
                    # Estimate generated token count from character chunks
                    chunk_toks = max(1, len(fn_args_chunk) // 4)
                    monitor_token(rid, chunk_toks)

            cur_fn_name = accumulated_tcs[idx]["function"]["name"]
            cur_args_str = accumulated_tcs[idx]["function"]["arguments"]
            tc_meta = accumulated_tcs[idx].setdefault("_stream_meta", {"last_yield": 0, "path": ""})
            now_t = time.time()
            if cur_fn_name and (now_t - tc_meta["last_yield"] > 0.35 or not tc_meta["last_yield"]):
                tc_meta["last_yield"] = now_t
                if not tc_meta["path"] and cur_args_str:
                    m_path = re.search(r'"(?:path|file|filename)"\s*:\s*"([^"]+)"', cur_args_str)
                    if m_path:
                        tc_meta["path"] = m_path.group(1)
                yield ("tool_preparing", {
                    "name": cur_fn_name,
                    "path": tc_meta["path"],
                    "bytes": len(cur_args_str),
                    "id": accumulated_tcs[idx]["id"]
                })

    for item in _emit_split(think, think.flush(), content_acc, reasoning_acc, rid):
        yield item
    for k in accumulated_tcs:
        accumulated_tcs[k].pop("_stream_meta", None)
    tool_calls = [accumulated_tcs[k] for k in sorted(accumulated_tcs.keys())]
    full_content = "".join(content_acc)
    full_reasoning = "".join(reasoning_acc)
    yield ("result", {
        "content": full_content,
        "reasoning": full_reasoning,
        "tool_calls": tool_calls,
        "usage": last_usage,
        "timings": last_timings,
        "finish_reason": finish_reason,
    })

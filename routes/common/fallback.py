import sys
from typing import Optional

from .llm_stream import _llm_chat_stream


async def _llm_chat_stream_with_fallback(primary, fallback, msgs: list, tools=None, temperature=0.4,
                                        max_tokens=-1, repeat_penalty=1.15, rid: Optional[int] = None,
                                        grammar: Optional[str] = None, lane: str = "cloud",
                                        extra: Optional[dict] = None, effort: Optional[str] = None,
                                        top_p: Optional[float] = None, min_p: Optional[float] = None,
                                        presence_penalty: Optional[float] = None, top_k: Optional[int] = None,
                                        tool_choice: Optional[str] = None):
    """Stream from `primary` (usually a CloudClient); if it fails before emitting any
    content, retry the same request on `fallback` (the local lane) instead of failing
    the whole agent step. Yields ("fallback", reason) once before switching so callers
    can tell the UI which engine actually answered."""
    produced = False
    try:
        async for item in _llm_chat_stream(primary, msgs, tools, temperature, max_tokens,
                                           repeat_penalty, rid, grammar, extra=extra, effort=effort,
                                           top_p=top_p, min_p=min_p, presence_penalty=presence_penalty, top_k=top_k,
                                           tool_choice=tool_choice):
            produced = True
            yield item
        return
    except Exception as e:
        if produced or fallback is None:
            raise
        reason = f"{type(e).__name__}: {str(e)[:200]}"
        print(f"[common] {lane} cloud lane failed ({reason}) - falling back to the local model",
              file=sys.stderr)
        yield ("fallback", reason)
    async for item in _llm_chat_stream(fallback, msgs, tools, temperature, max_tokens,
                                       repeat_penalty, rid, grammar, extra=extra, effort=effort,
                                       top_p=top_p, min_p=min_p, presence_penalty=presence_penalty, top_k=top_k,
                                           tool_choice=tool_choice):
        yield item

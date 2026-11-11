"""Makes a vLLM server answer like llama-server, so the chat code needs no engine checks.

It is an httpx transport under state.client (the main model's client). With llama.cpp loaded it passes everything through
untouched. With vLLM loaded it:
  - removes llama.cpp-only request fields (slot pinning, cache_prompt, thinking budget), renames repeat_penalty, sends a
    GBNF grammar the vLLM way, and asks for token usage on streams;
  - renames vLLM's "reasoning" delta to "reasoning_content" (what the stream reader takes);
  - adds a llama-style "timings" object built from vLLM's usage and the measured times, so the speed badge, cache counts
    and context ring keep working;
  - answers GET /slots itself (context window = --max-model-len, fill = last request), for the context budget and the
    status panel.
"""
import json
import time
from typing import Callable, Optional

import httpx

from . import engine_of

DROP_FIELDS = ("id_slot", "slot_id", "cache_prompt", "n_keep", "n_discard", "t_max_predict_ms", "samplers",
               "timings_per_token", "thinking_budget_tokens", "return_progress")
GEN_PATHS = ("/v1/chat/completions", "/v1/completions")

# what /slots reports while vLLM is loaded: each user's last request size (their own context ring), and how many run now
_last = {"inflight": 0}
_last_by_user: dict = {}        # user id (None = no user) -> (prompt tokens, completion tokens)
_MAX_USERS = 500


def _uid():
    try:
        from ..request_context import get_current_user_id
        return get_current_user_id()
    except Exception:
        return None


def adapt_request(payload: dict, profile: Optional[dict]) -> dict:
    from .vllm import served_name, settings
    p = {k: v for k, v in payload.items() if k not in DROP_FIELDS}
    if "repeat_penalty" in p:
        rp = p.pop("repeat_penalty")
        p.setdefault("repetition_penalty", rp)
    if "n_predict" in p:
        n = p.pop("n_predict")
        p.setdefault("max_tokens", n)
    try:
        if "max_tokens" in p and int(p["max_tokens"]) <= 0:
            p.pop("max_tokens")                       # llama's -1 = "no limit"; vLLM refuses a negative
    except (TypeError, ValueError):
        p.pop("max_tokens")
    g = p.pop("grammar", None)
    if g:
        if str(settings().get("grammar_param")) == "guided_grammar":
            p["guided_grammar"] = g
        else:
            p["structured_outputs"] = {**(p.get("structured_outputs") or {}), "grammar": g}
    if p.get("stream"):
        p["stream_options"] = {**(p.get("stream_options") or {}), "include_usage": True}
    if not p.get("model") and profile:
        p["model"] = served_name(profile)
    return p


def timings_from_usage(usage: dict, t_start: float, t_first: Optional[float], t_end: float) -> dict:
    """llama-server's timings fields from an OpenAI usage object and wall-clock times (seconds)."""
    usage = usage or {}
    prompt = int(usage.get("prompt_tokens") or 0)
    cached = int(((usage.get("prompt_tokens_details") or {}).get("cached_tokens")) or 0)
    gen = int(usage.get("completion_tokens") or 0)
    first = t_first if t_first is not None else t_end
    prompt_ms = max(0.001, (first - t_start) * 1000)
    gen_ms = max(0.001, (t_end - first) * 1000) if t_first is not None else max(0.001, (t_end - t_start) * 1000)
    fresh = max(0, prompt - cached)
    return {"prompt_n": fresh, "cache_n": cached, "prompt_ms": round(prompt_ms, 1),
            "prompt_per_second": round(fresh * 1000 / prompt_ms, 2) if fresh else 0.0,
            "predicted_n": gen, "predicted_ms": round(gen_ms, 1),
            "predicted_per_second": round(gen * 1000 / gen_ms, 2) if gen else 0.0,
            "engine": "vllm"}


def _rename_reasoning(obj: dict) -> None:
    for ch in obj.get("choices") or []:
        for key in ("delta", "message"):
            d = ch.get(key)
            if isinstance(d, dict) and d.get("reasoning") and not d.get("reasoning_content"):
                d["reasoning_content"] = d.pop("reasoning")


def _has_output(obj: dict) -> bool:
    for ch in obj.get("choices") or []:
        d = ch.get("delta") or {}
        if d.get("content") or d.get("reasoning") or d.get("reasoning_content") or d.get("tool_calls") or ch.get("text"):
            return True
    return False


def _note_usage(usage: dict, uid=None) -> None:
    if len(_last_by_user) >= _MAX_USERS and uid not in _last_by_user:
        _last_by_user.pop(next(iter(_last_by_user)))
    _last_by_user[uid] = (int(usage.get("prompt_tokens") or 0), int(usage.get("completion_tokens") or 0))


class _SseRewrite(httpx.AsyncByteStream):
    """Rewrites a vLLM SSE stream line by line (see the module doc)."""

    def __init__(self, inner, t_start: float, uid=None):
        self._uid = uid
        self._inner = inner
        self._t0 = t_start
        self._t_first = None
        self._closed = False

    def _line(self, line: bytes) -> bytes:
        if not line.startswith(b"data:"):
            return line
        raw = line[5:].strip()
        if not raw or raw == b"[DONE]":
            return line
        try:
            obj = json.loads(raw)
        except ValueError:
            return line
        if not isinstance(obj, dict):
            return line
        if self._t_first is None and _has_output(obj):
            self._t_first = time.monotonic()
        _rename_reasoning(obj)
        if isinstance(obj.get("usage"), dict) and not obj.get("timings"):
            obj["timings"] = timings_from_usage(obj["usage"], self._t0, self._t_first, time.monotonic())
            _note_usage(obj["usage"], self._uid)
        return b"data: " + json.dumps(obj, ensure_ascii=False).encode("utf-8")

    async def __aiter__(self):
        buf = b""
        try:
            async for chunk in self._inner:
                buf += chunk
                while b"\n" in buf:
                    line, buf = buf.split(b"\n", 1)
                    cr = line.endswith(b"\r")
                    out = self._line(line[:-1] if cr else line)
                    yield out + (b"\r\n" if cr else b"\n")
            if buf:
                yield self._line(buf)
        finally:
            await self.aclose()

    async def aclose(self) -> None:
        if not self._closed:
            self._closed = True
            _last["inflight"] = max(0, _last["inflight"] - 1)
            await self._inner.aclose()


class EngineTransport(httpx.AsyncBaseTransport):
    """state.client's transport. profile_fn() returns the main model's profile (None when nothing is loaded)."""

    def __init__(self, profile_fn: Callable[[], Optional[dict]], inner: Optional[httpx.AsyncBaseTransport] = None):
        self._profile = profile_fn
        self._inner = inner or httpx.AsyncHTTPTransport()

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        profile = self._profile()
        if engine_of(profile) != "vllm":
            return await self._inner.handle_async_request(request)
        path = request.url.path
        if request.method == "GET" and path == "/slots":
            return self._slots(profile)
        if request.method != "POST" or path not in GEN_PATHS:
            return await self._inner.handle_async_request(request)
        try:
            payload = json.loads(await request.aread() or b"{}")
        except ValueError:
            return await self._inner.handle_async_request(request)
        if not isinstance(payload, dict):
            return await self._inner.handle_async_request(request)
        headers = {k: v for k, v in request.headers.items() if k.lower() not in ("content-length", "accept-encoding")}
        headers["accept-encoding"] = "identity"            # the stream is rewritten here, so it must arrive uncompressed
        req = httpx.Request(request.method, request.url, headers=headers,
                            content=json.dumps(adapt_request(payload, profile)).encode("utf-8"),
                            extensions=request.extensions)
        t0 = time.monotonic()
        uid = _uid()
        resp = await self._inner.handle_async_request(req)
        if resp.status_code != 200:
            return resp
        out_headers = [(k, v) for k, v in resp.headers.items() if k.lower() not in ("content-length", "content-encoding")]
        if "text/event-stream" in resp.headers.get("content-type", ""):
            _last["inflight"] += 1
            return httpx.Response(200, headers=out_headers, stream=_SseRewrite(resp.stream, t0, uid),
                                  extensions=resp.extensions)
        body = b""
        try:
            async for chunk in resp.stream:
                body += chunk
        finally:
            await resp.stream.aclose()
        try:
            obj = json.loads(body)
            if isinstance(obj, dict):
                _rename_reasoning(obj)
                if isinstance(obj.get("usage"), dict):
                    obj["timings"] = timings_from_usage(obj["usage"], t0, None, time.monotonic())
                    _note_usage(obj["usage"], uid)
                body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        except ValueError:
            pass
        return httpx.Response(200, headers=out_headers, content=body, extensions=resp.extensions)

    @staticmethod
    def _slots(profile: dict) -> httpx.Response:
        from ..config import CONFIG_DEFAULTS
        n_ctx = int((profile or {}).get("context_size") or CONFIG_DEFAULTS["context_size"])
        # one entry, so "the caller's slot" is always this one, filled with the caller's own last request
        prompt, completion = _last_by_user.get(_uid(), (0, 0))
        slot = {"id": 0, "n_ctx": n_ctx, "n_prompt_tokens": prompt,
                "next_token": [{"n_decoded": completion}], "is_processing": _last["inflight"] > 0}
        return httpx.Response(200, json=[slot])

    async def aclose(self) -> None:
        await self._inner.aclose()

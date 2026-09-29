"""Input/output guard helpers and SSE redactor for the proxy router."""

import copy
import json

from core import input_guard, output_guard
from core.audit import audit_log


def _prompt_texts(payload: dict) -> list:
    """User-authored text in an OpenAI chat / completions / llama.cpp body."""
    texts = input_guard.message_texts(payload.get("messages"))   # every role, not just "user"
    prompt = payload.get("prompt")
    if isinstance(prompt, str):
        texts.append(prompt)
    elif isinstance(prompt, list):
        texts.extend(p for p in prompt if isinstance(p, str))
    return texts


def _redact_json(data, user, is_cloud: bool):
    """Redact assistant text in a non-streaming response body (in place)."""
    matched = None
    if not isinstance(data, dict):
        return data, None
    for ch in data.get("choices") or []:
        if not isinstance(ch, dict):
            continue
        msg = ch.get("message")
        if isinstance(msg, dict) and isinstance(msg.get("content"), str):
            msg["content"], m = output_guard.redact_full(msg["content"], user, is_cloud)
            matched = matched or m
        if isinstance(ch.get("text"), str):
            ch["text"], m = output_guard.redact_full(ch["text"], user, is_cloud)
            matched = matched or m
    if isinstance(data.get("content"), str):   # llama.cpp native /completion
        data["content"], m = output_guard.redact_full(data["content"], user, is_cloud)
        matched = matched or m
    return data, matched


def _audit_redaction(user, matched, path: str, hits: int = 1) -> None:
    if matched:
        audit_log(user, action="output_guard.redact", resource=matched.get("name"),
                  detail={"endpoint": f"proxy/{path}", "scope": matched.get("scope"),
                          "hits": hits}, result="deny")


def _guarded_json_response(r, user, is_cloud: bool, path: str):
    from fastapi.responses import JSONResponse
    if not r.headers.get("content-type", "").startswith("application/json"):
        text, matched = output_guard.redact_full(r.text, user, is_cloud)
        _audit_redaction(user, matched, path)
        return JSONResponse(content={"raw": text}, status_code=r.status_code)
    data, matched = _redact_json(r.json(), user, is_cloud)
    _audit_redaction(user, matched, path)
    return JSONResponse(content=data, status_code=r.status_code)


class _SSERedactor:
    """Rewrites an SSE byte stream so assistant text passes through the output
    guard. Lines are re-assembled across chunk boundaries; non-data lines and
    non-text fields pass through untouched. Held-back text is flushed as one
    extra delta right before [DONE]."""

    def __init__(self, user, is_cloud: bool):
        self.red = output_guard.OutputRedactor(user, is_cloud)
        self.pending = b""
        self.last_obj = None

    @property
    def active(self) -> bool:
        return bool(self.red.rules)

    def feed(self, chunk: bytes) -> bytes:
        self.pending += chunk
        *lines, self.pending = self.pending.split(b"\n")
        if not lines:
            return b""
        return b"\n".join(self._rewrite(ln.rstrip(b"\r")) for ln in lines) + b"\n"

    def close(self) -> bytes:
        out = self._rewrite(self.pending) if self.pending else b""
        self.pending = b""
        tail = self.red.flush()     # stream ended without [DONE]
        if tail and self.last_obj is not None:
            out += b"\ndata: " + json.dumps(self._as_delta(tail)).encode() + b"\n\n"
        return out

    def _rewrite(self, line: bytes) -> bytes:
        if not line.startswith(b"data:"):
            return line
        body = line[5:].strip()
        if body == b"[DONE]":
            tail = self.red.flush()
            if tail and self.last_obj is not None:
                return b"data: " + json.dumps(self._as_delta(tail)).encode() + b"\n\ndata: [DONE]"
            return line
        try:
            obj = json.loads(body)
        except Exception:
            return line
        if not isinstance(obj, dict):
            return line
        changed = False
        for ch in obj.get("choices") or []:
            if not isinstance(ch, dict):
                continue
            d = ch.get("delta")
            if isinstance(d, dict) and isinstance(d.get("content"), str):
                d["content"] = self.red.feed(d["content"])
                changed = True
            if isinstance(ch.get("text"), str):
                ch["text"] = self.red.feed(ch["text"])
                changed = True
        if isinstance(obj.get("content"), str):
            obj["content"] = self.red.feed(obj["content"])
            changed = True
        self.last_obj = obj
        return b"data: " + json.dumps(obj).encode() if changed else line

    def _as_delta(self, text: str) -> dict:
        o = copy.deepcopy(self.last_obj)
        o.pop("usage", None)
        o.pop("timings", None)
        if o.get("choices"):
            ch = o["choices"][0]
            if "delta" in ch:
                ch["delta"] = {"content": text}
            if "text" in ch:
                ch["text"] = text
            ch["finish_reason"] = None
            o["choices"] = [ch]
        if "content" in o:
            o["content"] = text
        return o

import asyncio
import sys
import time
from copy import deepcopy
from typing import Optional

import httpx

from ..config import CLOUD_PROBE_TIMEOUT_S
from .models import CloudModel, _LLAMA_ONLY_KEYS

_CLIENTS: dict = {}
_BLOCKED_HEADERS = {
    "host", "connection", "keep-alive", "proxy-authorization", "proxy-connection",
    "te", "trailer", "transfer-encoding", "upgrade", "content-length", "cookie"
}


def check_provider_url(url: str) -> str:
    from ..net_guard import check_url, BlockedURLError
    if not str(url or "").lower().startswith("https://"):
        raise ValueError("provider base URL must be https://")
    try:
        return check_url(url)
    except BlockedURLError as e:
        raise ValueError(f"provider base URL rejected: {e}") from e


async def _guard_request(request: httpx.Request) -> None:
    await asyncio.get_running_loop().run_in_executor(None, check_provider_url, str(request.url))


def _client_for(cm: CloudModel) -> httpx.AsyncClient:
    c = _CLIENTS.get(cm.key)
    if c is None:
        c = httpx.AsyncClient(timeout=httpx.Timeout(cm.timeout_s, connect=20.0),
                              event_hooks={"request": [_guard_request]})
        _CLIENTS[cm.key] = c
    return c


class CloudClient:
    """Drop-in stand-in for state.client / small-model clients."""

    is_cloud = True

    def __init__(self, cm: CloudModel):
        self.cm = cm
        self.base_url = cm.key

    @property
    def ctx(self) -> int:
        """The model's window in tokens (provider config), which lane compaction budgets against."""
        return self.cm.ctx

    @property
    def label(self) -> str:
        return self.cm.label()

    def headers(self) -> dict:
        h = {"Content-Type": "application/json"}
        if self.cm.api_key:
            h["Authorization"] = f"Bearer {self.cm.api_key}"
        h.update(self.cm.extra_headers)
        h.setdefault("HTTP-Referer", "https://localhost/local-agent")
        h.setdefault("X-Title", "Local Agent")
        return h

    def _prepare(self, payload):
        if not isinstance(payload, dict):
            return payload
        p = dict(payload)
        for k in _LLAMA_ONLY_KEYS:
            p.pop(k, None)
        p["model"] = self.cm.model_id
        mt = p.get("max_tokens")
        try:
            if mt is None or int(mt) <= 0:
                p.pop("max_tokens", None)
        except (TypeError, ValueError):
            p.pop("max_tokens", None)
        if p.get("stream") and self.cm.stream_options:
            p.setdefault("stream_options", {"include_usage": True})
        for k, v in self.cm.extra_body.items():
            p.setdefault(k, v)
        from .. import pan
        if pan.enabled("pan_cloud_egress") and isinstance(p.get("messages"), list):
            p["messages"] = deepcopy(p["messages"])
            n = pan.mask_messages(p["messages"])
            if n:
                print(f"[cloud] masked {n} card number(s) before sending to {self.cm.key}",
                      file=sys.stderr)
        return p

    def _timeout(self, timeout):
        return self.cm.timeout_s if timeout is None else timeout

    def stream(self, method="POST", url="/v1/chat/completions", json=None, timeout=None, **kw):
        return _client_for(self.cm).stream(
            method, self.cm.endpoint(), json=self._prepare(json),
            headers=self.headers(), timeout=self._timeout(timeout), **kw)

    async def post(self, url="/v1/chat/completions", json=None, timeout=None, **kw):
        return await _client_for(self.cm).post(
            self.cm.endpoint(), json=self._prepare(json),
            headers=self.headers(), timeout=self._timeout(timeout), **kw)

    async def aclose(self):
        c = _CLIENTS.pop(self.cm.key, None)
        if c is not None:
            try:
                await c.aclose()
            except Exception:
                pass

    def __repr__(self) -> str:
        return f"<CloudClient {self.cm.key}>"


async def probe(cm: CloudModel, prompt: str = "ping") -> dict:
    """One tiny non-streaming completion - proves URL + key + model id work."""
    t0 = time.time()

    def _hint(status: int, body: str) -> str:
        b = (body or "").lower()
        if status in (401, 403):
            if ("referer" in b or "http-referer" in b or "unauthorized_client" in b or "user not found" in b):
                return ("  Hint: this gateway rejected the request headers - add "
                        "'HTTP-Referer' (+ 'X-Title') in the provider's Extra request headers field.")
            return "  Hint: bad/expired API key - press the eye button to reveal it."
        if status == 404 and ("model" in b or "not found" in b):
            return "  Hint: model id not accepted - check spelling / availability."
        return ""

    try:
        r = await _client_for(cm).post(
            cm.endpoint(),
            json={"model": cm.model_id,
                  "messages": [{"role": "user", "content": prompt}],
                  "max_tokens": 16, "temperature": 0},
            headers=CloudClient(cm).headers(),
            timeout=CLOUD_PROBE_TIMEOUT_S)
        ms = int((time.time() - t0) * 1000)
        if r.status_code != 200:
            body = r.text[:300]
            print(f"[cloud] probe {cm.key} -> {r.status_code}: {body}", file=sys.stderr)
            return {"ok": False, "ms": ms, "status": r.status_code,
                    "error": f"HTTP {r.status_code}" + _hint(r.status_code, body), "endpoint": cm.endpoint()}
        sample = ""
        try:
            sample = str(r.json()["choices"][0]["message"]["content"] or "")
        except Exception:
            pass
        return {"ok": True, "ms": ms, "sample": sample.strip()[:200],
                "provider": cm.provider, "model": cm.model_id,
                "endpoint": cm.endpoint()}
    except Exception as e:
        return {"ok": False, "ms": int((time.time() - t0) * 1000),
                "error": f"{type(e).__name__}: {e}", "endpoint": cm.endpoint()}

import asyncio
import re
import sys
from typing import Optional
from .backends import BACKENDS, _clean
from .cache import cache_get, cache_put
from .constants import MAX_RESULTS, RECENCY, _REWRITE_PROMPT, cfg
from .extraction import attach_passages
from .rerank import merge, rerank


async def rewrite_query(message: str, fallback: str) -> str:
    """Turn a conversational message into a search query -- the "Web search queries"
    job (core/lanes.py; the small executor by default, always local). Only used when
    that model is already running - never loads one just for this."""
    try:
        from .. import lanes
        route = lanes.targets("search_rewrite")
        t = route[0] if route else None
        if t is None or t.lane == "main" or not t.is_up():
            return fallback
        client = await t.client()
        r = await asyncio.wait_for(client.post("/v1/chat/completions", json={
            "messages": [{"role": "system", "content": _REWRITE_PROMPT},
                         {"role": "user", "content": message[:1500]}],
            "max_tokens": 40, "temperature": 0.0}), timeout=8)
        q = ((r.json().get("choices") or [{}])[0].get("message") or {}).get("content") or ""
        q = _clean(re.sub(r"<think>.*?</think>", "", q, flags=re.S)).strip("\"'` ")
        return q if 2 <= len(q) <= 200 else fallback
    except Exception:
        return fallback


async def search(query: str, recency: str = "", auto_fetch: Optional[int] = None) -> dict:
    """-> {query, engines, results, errors}. `query` must already be PAN-checked."""
    mod = sys.modules.get("core.web_search")
    cfg_fn = getattr(mod, "cfg", cfg)
    backends_dict = getattr(mod, "BACKENDS", BACKENDS)
    cache_get_fn = getattr(mod, "cache_get", cache_get)
    cache_put_fn = getattr(mod, "cache_put", cache_put)
    rerank_fn = getattr(mod, "rerank", rerank)
    merge_fn = getattr(mod, "merge", merge)
    attach_fn = getattr(mod, "attach_passages", attach_passages)

    recency = recency if recency in RECENCY else ""
    n_fetch = int(cfg_fn().get("auto_fetch_top", 3) if auto_fetch is None else auto_fetch)
    key = ("search", query.lower(), str(cfg_fn().get("region") or "bd-en"), recency, n_fetch)
    hit = cache_get_fn(key)
    if hit is not None:
        return hit
    order = [b for b in (cfg_fn().get("backends") or ["searxng", "duckduckgo", "bing"]) if b in backends_dict]
    if not cfg_fn().get("searxng_url"):
        order = [b for b in order if b != "searxng"]
    results = await asyncio.gather(*(asyncio.to_thread(backends_dict[b], query, recency) for b in order),
                                   return_exceptions=True)
    lists, errors = {}, {}
    for name, res in zip(order, results):
        if isinstance(res, Exception):
            errors[name] = f"{type(res).__name__}: {res}"[:200]
        elif res:
            lists[name] = res
    items = (await rerank_fn(query, merge_fn(lists)))[:MAX_RESULTS]
    await attach_fn(query, items, n_fetch)
    out = {"query": query, "engines": list(lists), "results": items, "errors": errors}
    if items:
        cache_put_fn(key, out)
    return out

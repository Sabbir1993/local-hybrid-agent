import asyncio
import re
import sys
from typing import Optional
from . import blocklist, health
from .backends import BACKENDS, _clean
from .cache import cache_get, cache_put
from .constants import MAX_RESULTS, RECENCY, _REWRITE_PROMPT, cfg
from .extraction import attach_passages
from .rerank import apply_domain_prefs, dedup_items, detect_recency, merge, rerank


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


MAX_EXTRA_QUERIES = 2


async def _run_backends(names, queries, recency, backends_dict, lists, errors) -> None:
    """Run every (backend, query) pair in parallel; results land in `lists` under "backend#i"
    (i = query index) so merge() fuses them; failures feed the circuit breaker."""
    pairs = [(b, i, q) for b in names for i, q in enumerate(queries)]
    results = await asyncio.gather(*(asyncio.to_thread(backends_dict[b], q, recency) for b, _, q in pairs),
                                   return_exceptions=True)
    failed = {}
    for (b, i, _), res in zip(pairs, results):
        if isinstance(res, Exception):
            failed[b] = failed.get(b, True)
            errors[b] = f"{type(res).__name__}: {res}"[:200]
        else:
            failed[b] = False
            if res:
                lists[f"{b}#{i}"] = res
    for b, bad in failed.items():
        health.record(b, ok=not bad)


async def search(query: str, recency: str = "", auto_fetch: Optional[int] = None,
                 extra_queries: Optional[list] = None) -> dict:
    """-> {query, engines, results, errors}. `query` (and any `extra_queries`, alternative
    phrasings fused with RRF) must already be PAN-checked.

    With a local SearXNG configured it is asked first; the scraped engines (DuckDuckGo, Bing) are
    only used when it is down or returns fewer than web.searxng_min_results (default 5) hits."""
    mod = sys.modules.get("core.web_search")
    cfg_fn = getattr(mod, "cfg", cfg)
    backends_dict = getattr(mod, "BACKENDS", BACKENDS)
    cache_get_fn = getattr(mod, "cache_get", cache_get)
    cache_put_fn = getattr(mod, "cache_put", cache_put)
    rerank_fn = getattr(mod, "rerank", rerank)
    merge_fn = getattr(mod, "merge", merge)
    attach_fn = getattr(mod, "attach_passages", attach_passages)

    recency = recency if recency in RECENCY else ""
    if not recency and cfg_fn().get("auto_recency", True):
        recency = detect_recency(query)
    queries = [query]
    for q in extra_queries or []:
        q = _clean(str(q))
        if q and q.lower() not in {x.lower() for x in queries} and len(queries) <= MAX_EXTRA_QUERIES:
            queries.append(q)
    n_fetch = int(cfg_fn().get("auto_fetch_top", 3) if auto_fetch is None else auto_fetch)
    key = ("search", tuple(q.lower() for q in queries), str(cfg_fn().get("region") or "bd-en"), recency, n_fetch)
    hit = cache_get_fn(key)
    if hit is not None:
        return hit
    order = [b for b in (cfg_fn().get("backends") or ["searxng", "duckduckgo", "bing"]) if b in backends_dict]
    if not cfg_fn().get("searxng_url"):
        order = [b for b in order if b != "searxng"]
    live = [b for b in order if not health.is_open(b)] or order   # all cooling down: try anyway
    lists, errors = {}, {}
    if "searxng" in live and len(live) > 1:
        await _run_backends(["searxng"], queries, recency, backends_dict, lists, errors)
        enough = int(cfg_fn().get("searxng_min_results", 5))
        have = {u["url"] for r in lists.values() for u in r}
        live = [] if len(have) >= enough else [b for b in live if b != "searxng"]
    if live:
        await _run_backends(live, queries, recency, backends_dict, lists, errors)
    items = await rerank_fn(query, merge_fn(lists))
    items = apply_domain_prefs(dedup_items(items), cfg_fn().get("deny_hosts"), cfg_fn().get("boost_hosts"))
    # drop sites known to refuse bots, unless that would leave nothing
    items = ([i for i in items if not blocklist.is_blocked(i["url"])] or items)[:MAX_RESULTS]
    await attach_fn(query, items, n_fetch)
    engines = list(dict.fromkeys(k.split("#")[0] for k in lists))
    out = {"query": query, "engines": engines, "results": items, "errors": errors}
    if items:
        cache_put_fn(key, out)
    return out

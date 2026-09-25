"""Web capability tools: web_search, web_fetch, web_search_images.

Keyless. The search pipeline (engines, merge, rerank, page extraction, cache) lives
in core/web_search.py; this module is the tool surface the model sees.
Config: config/app.json "web" block -
  region            bd-en (default) | bd-bn | us-en | uk-en | in-en | wt-wt
  auto_fetch_top    pages read per search for excerpts (default 3, 0 = snippets only)
  backends          order of engines (default ["searxng", "duckduckgo", "bing"])
  searxng_url       optional org-run SearXNG instance (JSON format enabled)
  redact_query_pii  strip emails / phone / account numbers from queries (default true)
"""

import asyncio
import json
import re
from urllib.parse import urlparse, quote_plus

import httpx

from . import web_search as ws
from .agent_tools import MAX_TOOL_OUTPUT
from .net_guard import BlockedURLError
from .registry import registry

UA = ws.UA
FETCH_TIMEOUT_S = 15
SEARCH_TIMEOUT_S = ws.SEARCH_TIMEOUT_S
MAX_RESULTS_CHARS = 12000


_PAN_EGRESS_ERROR = ("error: refusing to send a payment card number to an external site "
                     "(PCI DSS) - remove it from the {what}")


def _pan_egress(text: str) -> bool:
    """URLs and search queries leave for arbitrary third parties -- also the easiest
    exfiltration channel for a prompt-injected page -- so a PAN in them is refused."""
    from . import pan
    return pan.enabled("pan_cloud_egress") and pan.contains_pan(text)


def _query_arg(args) -> str:
    if isinstance(args, str):
        return args.strip()
    if isinstance(args, dict):
        return str(args.get("query") or args.get("q") or "").strip()
    return str(args or "").strip()


async def tool_web_fetch(args: dict) -> str:
    url = (args.get("url") or "").strip()
    focus = str(args.get("query") or "").strip()
    if not url:
        raise ValueError("url required")
    if _pan_egress(url):
        return _PAN_EGRESS_ERROR.format(what="URL")
    if not re.match(r"^https?://", url):
        url = "https://" + url
    parsed = urlparse(url)
    if not parsed.netloc:
        return f"error: invalid url: {args.get('url')}"
    page = ws.cache_get(("fetch", url))
    if page is None:
        try:
            # SSRF-guarded (core/net_guard.py): every redirect hop must resolve to a public address
            r = await asyncio.to_thread(ws.fetch_page, url, FETCH_TIMEOUT_S, ws.region()["accept"])
        except BlockedURLError as e:
            return f"error: {e}"
        except httpx.HTTPError as e:
            return f"error: fetch failed: {type(e).__name__}: {e}"
        if r.status_code >= 400:
            return f"error: HTTP {r.status_code} fetching {url}"
        page = {**ws.extract_response(r), "url": str(r.url)}
        ws.cache_put(("fetch", url), page)
    body = page["text"]
    budget = MAX_TOOL_OUTPUT - 2000
    if len(body) > budget:
        if focus:
            # long page + a stated goal: return the relevant passages, not just the top
            parts = await ws.best_passages(focus, body, k=max(3, budget // ws.PASSAGE_CHARS))
            body = (f"(long page, {len(page['text'])} chars - showing the passages most relevant to "
                    f"'{focus}')\n\n" + "\n\n...\n\n".join(parts))[:budget]
        else:
            body = body[:budget] + (f"\n... (truncated, {len(page['text'])} chars total - call web_fetch "
                                    "again with a 'query' to get the relevant parts)")
    out = f"# {page['title'] or parsed.netloc}\nSource: {page['url']}\n\n{body}"
    if page["links"]:
        out += "\n\n## Links\n" + "\n".join(f"- [{t}]({u})" for u, t in page["links"])
    return out[:MAX_TOOL_OUTPUT]


def _bing_images_search_sync(query: str, count: int = 8) -> list:
    """Bing image search results."""
    url = "https://www.bing.com/images/search?q=" + quote_plus(query)
    with httpx.Client(follow_redirects=True, timeout=SEARCH_TIMEOUT_S,
                      headers={"User-Agent": UA, "Accept-Language": ws.region()["accept"]}) as c:
        r = c.get(url)
    if r.status_code != 200:
        raise RuntimeError(f"Bing images search returned HTTP {r.status_code}")
    results = []
    matches = re.findall(r'class="iusc"[^>]*m="([^"]+)"', r.text)
    for m_str in matches:
        unescaped = m_str.replace('&quot;', '"')
        try:
            data = json.loads(unescaped)
            murl = data.get("murl")
            if not murl:
                continue
            turl = data.get("turl", "")
            title = data.get("t", "")
            purl = data.get("purl", "")
            results.append({
                "title": title or query,
                "image_url": murl,
                "thumbnail_url": turl,
                "source_url": purl,
            })
            if len(results) >= count:
                break
        except Exception:
            continue
    return results


async def tool_web_search(args) -> str:
    query = _query_arg(args)
    if not query:
        raise ValueError("query required")
    if _pan_egress(query):
        return _PAN_EGRESS_ERROR.format(what="search query")
    recency = str(args.get("recency") or "").lower() if isinstance(args, dict) else ""
    query, redacted = ws.redact_query(query)
    if not query:
        return "error: nothing left to search after removing personal data from the query"
    try:
        res = await ws.search(query, recency)
    except Exception as e:
        return f"error: search failed: {type(e).__name__}: {e}"
    if not res["results"]:
        errs = "; ".join(f"{k}: {v}" for k, v in res["errors"].items())
        return f"(no results for: {query})" + (f" [engine errors - {errs}]" if errs else "")

    names = {"duckduckgo": "DuckDuckGo", "bing": "Bing", "searxng": "SearXNG"}
    engines = " + ".join(names.get(e, e) for e in res["engines"])
    out = [f"Web results for: {query} (via {engines}" + (f", past {recency}" if recency else "") + ")"]
    if redacted:
        out.append(f"Note: removed {', '.join(redacted)} from the query before sending it to the "
                   "search engine (privacy).")
    out.append("")
    for i, r in enumerate(res["results"], 1):
        out.append(f"[{i}] {r['title']}\n    {r['url']}")
        if r.get("snippet"):
            out.append(f"    {r['snippet']}")
        room = 1200                                  # excerpt budget per source
        for p in r.get("passages") or []:
            if room <= 80:
                break
            p = p if len(p) <= room else p[:room].rsplit(" ", 1)[0] + " …"
            room -= len(p)
            out.append("    > " + p.replace("\n", "\n    > "))
        out.append("")
    out.append("Answer from these sources and cite them inline as [n](URL). Lines marked '>' are "
               "excerpts read from the page itself - prefer them over snippets. If they don't answer "
               "the question, call web_fetch on the most promising URL with a 'query'.")

    # If query specifically asks for a photo/image, also append top image suggestions
    lower_q = query.lower()
    if any(w in lower_q for w in ("photo", "picture", "image", "portrait", "look like")):
        try:
            img_results = await asyncio.to_thread(_bing_images_search_sync, query, 3)
            if img_results:
                out.append("\nImage Results:")
                for img in img_results:
                    out.append(f"- ![{img['title']}]({img['image_url']}) (Source: {img['source_url']})")
        except Exception:
            pass

    text = "\n".join(out)
    if len(text) > MAX_RESULTS_CHARS:
        text = text[:MAX_RESULTS_CHARS] + "\n... (truncated)"
    return text


async def tool_web_search_images(args) -> str:
    query = _query_arg(args)
    if not query:
        raise ValueError("query required")
    if _pan_egress(query):
        return _PAN_EGRESS_ERROR.format(what="search query")
    query, _ = ws.redact_query(query)
    try:
        results = await asyncio.to_thread(_bing_images_search_sync, query, 6)
    except Exception as e:
        return f"error: image search failed: {e}"

    if not results:
        return f"(no images found for: {query})"

    out = [f"Found {len(results)} image(s) for: {query}\n"]
    for i, r in enumerate(results, 1):
        out.append(f"{i}. **{r['title']}**\n   - Image URL: {r['image_url']}\n   - Thumbnail: {r['thumbnail_url']}\n   - Source: {r['source_url']}\n   - Markdown: ![{r['title']}]({r['image_url']})")
    return "\n".join(out)


def register_web_tools() -> None:
    """Register web tools into the global registry (idempotent)."""
    from .small_model import APP_CONFIG as _cfg
    if not _cfg.get("capabilities", {}).get("web", False):
        return
    registry.register(
        "web_fetch", tool_web_fetch,
        {"type": "function", "function": {
            "name": "web_fetch",
            "description": "Fetch a web page (or PDF) by URL and return its main readable text (menus, ads and cookie banners removed). Use for docs, articles, APIs. Give 'query' to get the relevant parts of long pages.",
            "parameters": {"type": "object",
                           "properties": {"url": {"type": "string", "description": "absolute URL, e.g. https://example.com/docs"},
                                          "query": {"type": "string", "description": "optional: what you are looking for on the page"}},
                           "required": ["url"]},
        }},
        source="web", meta={"label": "Web fetch"}, replace=True)
    registry.register(
        "web_search", tool_web_search,
        {"type": "function", "function": {
            "name": "web_search",
            "description": "Search the web (several engines, reranked) and return numbered sources with snippets plus excerpts read from the top pages. For news, prices, releases or other time-sensitive data set 'recency' and/or include the current year (from the CURRENT DATE in the system prompt) - not your training-cutoff year; leave both out for evergreen topics (company profiles, definitions, documentation). Keep queries short and specific; never include personal data.",
            "parameters": {"type": "object",
                           "properties": {"query": {"type": "string", "description": "search query (keywords)"},
                                          "recency": {"type": "string", "enum": ["day", "week", "month", "year"],
                                                      "description": "optional: only results from this recent period"}},
                           "required": ["query"]},
        }},
        source="web", meta={"label": "Web search"}, replace=True)
    registry.register(
        "web_search_images", tool_web_search_images,
        {"type": "function", "function": {
            "name": "web_search_images",
            "description": "Search the web for photos, portraits, pictures, diagrams, and images. Returns direct image URLs, thumbnails, and markdown image embed links.",
            "parameters": {"type": "object",
                           "properties": {"query": {"type": "string", "description": "image search query"}},
                           "required": ["query"]},
        }},
        source="web", meta={"label": "Web image search"}, replace=True)

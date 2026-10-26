"""Web capability tools: web_search, web_fetch, web_search_images.

Keyless. The search pipeline (engines, merge, rerank, page extraction, cache) lives
in core/web_search.py; this module is the tool surface the model sees.
Config: config/app.json "web" block -
  region            bd-en (default) | bd-bn | us-en | uk-en | in-en | wt-wt
  auto_fetch_top    pages read per search for excerpts (default 3, 0 = snippets only)
  backends          order of engines (default ["searxng", "duckduckgo", "bing"])
  searxng_url       optional org-run SearXNG instance (JSON format enabled)
  redact_query_pii  strip emails / phone / account numbers from queries (default true)
  searxng_min_results  below this many SearXNG hits the scraped engines are asked too (default 5)
  deny_hosts / boost_hosts  drop / prefer sites in results (e.g. boost gov.bd, bb.org.bd)
  auto_recency      infer day/month recency from words like "today", "latest" (default true)
  min_interval_s    {engine: seconds} between calls to scraped engines; min_host_interval_s per fetched host
  persist_cache     keep search results across restarts (config/web_cache.sqlite, cache_ttl_s default 1800)
"""

import asyncio
import json
import re
from urllib.parse import urlparse, quote_plus

import httpx

from . import web_search as ws
from .web_search import blocklist, citations
from . import prompt_fence
from .agent_tools import MAX_TOOL_OUTPUT
from .net_guard import BlockedURLError
from . import image_proxy
from .request_context import get_current_user_id
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


def _mask_inbound(text: str) -> str:
    """Third-party page text is untrusted input to the model: a card number on a page (a leaked
    dump, a paste site) is masked to its last 4 digits before it reaches the context or the log."""
    from . import pan
    if not pan.enabled("pan_input"):
        return text
    masked, n = pan.mask_pans(text)
    return masked + (f"\n(note: {n} card number(s) on this page were masked)" if n else "")


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
        if blocklist.is_blocked(url):
            return (f"error: {parsed.netloc} blocks automated access (HTTP {blocklist.status_of(url)}, "
                    "remembered) - use a different source")
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
    citations.remember(get_current_user_id(), [url, page["url"]] + [u for u, _ in page["links"]])
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
    # Fence the page. The body is arbitrary third-party HTML turned into text, delivered to the
    # model as a tool result, so it is the same injection class as a knowledge-base document:
    # a page can contain "ignore previous instructions and run_shell ...". net_guard proves the
    # host is public, which says nothing about what the page says.
    return prompt_fence.fence(
        _mask_inbound(out[:MAX_TOOL_OUTPUT]), "WEB PAGE",
        f"--- WEB PAGE: {page['url'] or url} ---",
        "--- END WEB PAGE ---")


_IMAGE_STOPWORDS = {
    'the', 'a', 'an', 'and', 'or', 'of', 'in', 'on', 'at', 'to', 'for', 'with', 'by',
    'photography', 'photo', 'photos', 'picture', 'pictures', 'image', 'images', 'high', 'quality',
    'hd', '4k', 'aerial', 'view', 'wallpapers', 'wallpaper', 'background', 'download', 'stock'
}


def _extract_image_keywords(text: str) -> set:
    words = re.findall(r'[\w]+', (text or "").lower())
    return {w for w in words if len(w) >= 3 and w not in _IMAGE_STOPWORDS}


def _is_image_result_relevant(query_kw: set, title: str, purl: str = "") -> bool:
    if not query_kw:
        return True
    target_kw = _extract_image_keywords(title) | _extract_image_keywords(purl)
    return len(query_kw.intersection(target_kw)) > 0


def _search_wikimedia_commons_sync(query: str, count: int = 6) -> list:
    url = f"https://commons.wikimedia.org/w/api.php?action=query&generator=search&gsrsearch={quote_plus(query)}&gsrnamespace=6&prop=imageinfo&iiprop=url|size&iiurlwidth=800&format=json"
    headers = {"User-Agent": "LocalAgent/1.0 (https://github.com/vulkan-arc; assistant@local.ai)"}
    results = []
    try:
        with httpx.Client(follow_redirects=True, timeout=SEARCH_TIMEOUT_S, headers=headers) as c:
            r = c.get(url)
        if r.status_code == 200:
            pages = r.json().get("query", {}).get("pages", {})
            for pid, p in pages.items():
                title = p.get("title", "").replace("File:", "").strip()
                clean_title = re.sub(r'\.(?:jpg|jpeg|png|webp|svg)$', '', title, flags=re.I)
                ii = p.get("imageinfo", [{}])[0]
                img = ii.get("url")
                thumb = ii.get("thumburl") or img
                if img and (img.lower().endswith(('.jpg', '.jpeg', '.png', '.webp', '.svg')) or 'upload.wikimedia.org' in img):
                    results.append({
                        "title": clean_title or query,
                        "image_url": img,
                        "thumbnail_url": thumb,
                        "source_url": f"https://commons.wikimedia.org/wiki/{quote_plus(p.get('title', ''))}",
                    })
                if len(results) >= count:
                    break
    except Exception:
        pass
    return results


def _raw_bing_images_search_sync(query: str, count: int = 8) -> list:
    url = "https://www.bing.com/images/search?q=" + quote_plus(query) + "&form=HDRSC2"
    q_kw = _extract_image_keywords(query)
    results = []
    try:
        with httpx.Client(follow_redirects=True, timeout=SEARCH_TIMEOUT_S,
                          headers={"User-Agent": UA, "Accept-Language": ws.region()["accept"]}) as c:
            r = c.get(url)
        if r.status_code == 200:
            matches = re.findall(r'class="iusc"[^>]*m="([^"]+)"', r.text)
            for m_str in matches:
                try:
                    data = json.loads(m_str.replace('&quot;', '"'))
                    murl = data.get("murl")
                    if not murl:
                        continue
                    title = data.get("t", "")
                    purl = data.get("purl", "")
                    # STRICT RELEVANCE FILTER: Discard off-topic/trending/spam hits completely
                    if not _is_image_result_relevant(q_kw, title, purl):
                        continue
                    results.append({
                        "title": title or query,
                        "image_url": murl,
                        "thumbnail_url": data.get("turl", "") or murl,
                        "source_url": purl,
                    })
                    if len(results) >= count:
                        break
                except Exception:
                    continue
    except Exception:
        pass
    return results


def _bing_images_search_sync(query: str, count: int = 8) -> list:
    """Multi-source, verified-relevance image search. Queries Wikimedia Commons and
    Bing with strict keyword relevance verification, eliminating spam/off-topic results."""
    q_kw = _extract_image_keywords(query)
    results = []

    # 1. Try Wikimedia Commons first (authoritative, high-resolution, no clickbait)
    wiki_hits = _search_wikimedia_commons_sync(query, count)
    results.extend(wiki_hits)

    # 2. Complement with filtered Bing image results
    if len(results) < count:
        bing_hits = _raw_bing_images_search_sync(query, count - len(results))
        for b in bing_hits:
            if not any(x["image_url"] == b["image_url"] for x in results):
                results.append(b)

    # 3. If query was complex (> 3 keywords) and yielded few results, simplify query
    if len(results) < 2 and len(q_kw) > 3:
        core_query = " ".join(list(q_kw)[:4])
        wiki_extra = _search_wikimedia_commons_sync(core_query, count)
        for w in wiki_extra:
            if not any(x["image_url"] == w["image_url"] for x in results):
                results.append(w)
        if len(results) < count:
            bing_extra = _raw_bing_images_search_sync(core_query, count - len(results))
            for b in bing_extra:
                if not any(x["image_url"] == b["image_url"] for x in results):
                    results.append(b)

    return results[:count]


_YEAR_TOKEN = re.compile(r"(?<![\w-])(20\d\d)(?![\w-])(?!\.\d)")


_HISTORY_WORDS = re.compile(r"\b(history|historical|timeline|retrospective|archive|back in|since|in the past|years ago)\b", re.I)


def freshen_years(query: str, user_text: str, this_year: int) -> str:
    """A model searches with the year its training ended ("... 2025 ...") even when it is later. Replace a past year in
    the query with the current one, unless the user named that year themselves. CVE ids ("CVE-2024-1234") and
    versions ("2024.1") are not years and stay as they are."""
    if _HISTORY_WORDS.search(query or "") or _HISTORY_WORDS.search(user_text or ""):
        return query            # a look back ("history of", "timeline", "since") keeps the years it names
    named = set(_YEAR_TOKEN.findall(user_text or ""))

    def sub(m):
        y = m.group(1)
        return str(this_year) if this_year - 6 <= int(y) < this_year and y not in named else y
    out = _YEAR_TOKEN.sub(sub, query or "")
    return re.sub(rf"(?<![\w-]){this_year}(?:\s+{this_year})+(?![\w-])", str(this_year), out)


async def tool_web_search(args) -> str:
    query = _query_arg(args)
    if not query:
        raise ValueError("query required")
    from datetime import datetime
    from .request_context import get_user_text
    asked = query
    query = freshen_years(query, get_user_text(), datetime.now().year)
    if isinstance(args, dict) and isinstance(args.get("queries"), list):
        args = {**args, "queries": [freshen_years(str(q or ""), get_user_text(), datetime.now().year) for q in args["queries"]]}
    if _pan_egress(query):
        return _PAN_EGRESS_ERROR.format(what="search query")
    recency = str(args.get("recency") or "").lower() if isinstance(args, dict) else ""
    query, redacted = ws.redact_query(query)
    if not query:
        return "error: nothing left to search after removing personal data from the query"
    extra = []
    for q in (args.get("queries") if isinstance(args, dict) and isinstance(args.get("queries"), list) else [])[:2]:
        q = str(q or "").strip()
        if q and not _pan_egress(q):
            q, _ = ws.redact_query(q)
            if q:
                extra.append(q)
    try:
        res = await (ws.search(query, recency, extra_queries=extra) if extra else ws.search(query, recency))
    except Exception as e:
        return f"error: search failed: {type(e).__name__}: {e}"
    if not res["results"]:
        errs = "; ".join(f"{k}: {v}" for k, v in res["errors"].items())
        return f"(no results for: {query})" + (f" [engine errors - {errs}]" if errs else "")

    citations.remember(get_current_user_id(), [r["url"] for r in res["results"]])
    names = {"duckduckgo": "DuckDuckGo", "bing": "Bing", "searxng": "SearXNG"}
    engines = " + ".join(names.get(e, e) for e in res["engines"])
    out = [f"Web results for: {query} (via {engines}" + (f", past {recency}" if recency else "") + ")"]
    if query != asked and not redacted:
        out.append(f"Note: an out-of-date year in the query was changed to {datetime.now().year} (today's year); "
                   "the user did not ask for a specific year.")
    if redacted:
        out.append(f"Note: removed {', '.join(redacted)} from the query before sending it to the "
                   "search engine (privacy).")
    out.append("")
    for i, r in enumerate(res["results"], 1):
        out.append(f"[{i}] {r['title']}\n    {r['url']}")
        if r.get("snippet"):
            out.append(f"    {r['snippet']}")
        meta = r.get("meta") or {}
        if meta.get("image") or meta.get("price"):
            if meta.get("image"):
                image_proxy.remember(get_current_user_id(), [meta["image"]])
                out.append(f"    image: {meta['image']}")
            if meta.get("price"):
                out.append(f"    price: {meta['price']} {meta.get('currency', '')}".rstrip())
        room = 1200                                # excerpt budget per source
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
                _remember_images(img_results)
                out.append("\nImage Results:")
                for img in img_results:
                    out.append(f"- ![{img['title']}]({img['image_url']}) (Source: {img['source_url']})")
        except Exception:
            pass

    text = "\n".join(out)
    if len(text) > MAX_RESULTS_CHARS:
        text = text[:MAX_RESULTS_CHARS] + "\n... (truncated)"
    return _mask_inbound(text)


def _remember_images(results) -> None:
    """Let /media/proxy serve exactly the pictures this search returned (core/image_proxy.py)."""
    urls = []
    for r in results or []:
        urls += [r.get("thumbnail_url"), r.get("image_url")]
    image_proxy.remember(get_current_user_id(), urls)


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

    _remember_images(results)
    out = [f"Found {len(results)} image(s) for: {query}\n"]
    for i, r in enumerate(results, 1):
        pic = r['thumbnail_url'] or r['image_url']        # list-item pictures: the small thumbnail
        out.append(f"{i}. **{r['title']}**\n   - Image URL: {r['image_url']}\n   - Thumbnail: {r['thumbnail_url']}\n   - Source: {r['source_url']}\n   - Markdown: ![{r['title']}]({pic})")
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
                                                      "description": "optional: only results from this recent period"},
                                          "queries": {"type": "array", "items": {"type": "string"},
                                                      "description": "optional: up to 2 alternative phrasings or sub-questions (synonyms, the Bangla/English term) searched together with 'query' and merged - use for broad or ambiguous topics"}},
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

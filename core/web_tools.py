"""Web capability tools: web_fetch (URL -> readable text) + web_search (DuckDuckGo).

Keyless by design; config slot capabilities.web_search_api_key reserved for a
future Brave/Tavily swap-in when DDG rate-limits.
"""

import asyncio
import json
import re
import sys
from html.parser import HTMLParser
from typing import Optional
from urllib.parse import urlparse, urljoin, quote_plus, unquote

import httpx

from .agent_tools import MAX_TOOL_OUTPUT
from .net_guard import BlockedURLError, guarded_get
from .registry import registry
from .small_model import APP_CONFIG

UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"
FETCH_TIMEOUT_S = 15
SEARCH_TIMEOUT_S = 12
MAX_SEARCH_RESULTS = 8
MAX_RESULTS_CHARS = 6000


class _TextExtractor(HTMLParser):
    """Strip scripts/styles/nav chrome, keep readable text + links."""
    SKIP = {"script", "style", "noscript", "svg", "iframe", "header", "footer", "nav", "aside", "form", "button"}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts = []
        self.links = []
        self._skip_depth = 0
        self._a_href = None
        self._a_text = []
        self._title = []
        self._in_title = False

    def handle_starttag(self, tag, attrs):
        if tag in self.SKIP:
            self._skip_depth += 1
        elif tag == "a":
            self._a_href = dict(attrs).get("href")
            self._a_text = []
        elif tag == "title":
            self._in_title = True
        elif tag in ("p", "br", "li", "tr", "h1", "h2", "h3", "h4", "h5", "h6", "div"):
            self.parts.append("\n")

    def handle_endtag(self, tag):
        if tag in self.SKIP and self._skip_depth > 0:
            self._skip_depth -= 1
        elif tag == "a":
            if self._a_href and self._a_text:
                self.links.append((self._a_href, " ".join("".join(self._a_text).split())))
            self._a_href = None
            self._a_text = []
        elif tag == "title":
            self._in_title = False
        elif tag in ("p", "li", "h1", "h2", "h3", "h4"):
            self.parts.append("\n")

    def handle_data(self, data):
        if self._skip_depth:
            return
        if self._in_title:
            self._title.append(data)
        if self._a_href is not None:
            self._a_text.append(data)
        self.parts.append(data)

    def text(self) -> str:
        out = re.sub(r"[ \t\r\f]+", " ", "".join(self.parts))
        out = re.sub(r"\n\s*\n+", "\n\n", out)
        return out.strip()

    def title(self) -> str:
        return " ".join("".join(self._title).split())


FETCH_MAX_BYTES = 5 * 1024 * 1024


def _fetch_sync(url: str, timeout: float):
    # SSRF-guarded (core/net_guard.py): the URL comes from the model, so every
    # redirect hop must resolve to a public address; body capped at 5 MB.
    return guarded_get(url, timeout,
                       headers={"User-Agent": UA, "Accept-Language": "en-US,en;q=0.9"},
                       max_bytes=FETCH_MAX_BYTES)


async def tool_web_fetch(args: dict) -> str:
    url = (args.get("url") or "").strip()
    if not url:
        raise ValueError("url required")
    if not re.match(r"^https?://", url):
        url = "https://" + url
    parsed = urlparse(url)
    if not parsed.netloc:
        return f"error: invalid url: {args.get('url')}"
    try:
        r = await asyncio.get_event_loop().run_in_executor(
            None, _fetch_sync, url, FETCH_TIMEOUT_S)
    except BlockedURLError as e:
        return f"error: {e}"
    except httpx.HTTPError as e:
        return f"error: fetch failed: {type(e).__name__}: {e}"
    if r.status_code >= 400:
        return f"error: HTTP {r.status_code} fetching {url}"
    ctype = r.headers.get("content-type", "")
    if "html" in ctype or "xml" in ctype or not ctype:
        p = _TextExtractor()
        try:
            p.feed(r.text)
        except Exception:
            return f"(non-parsable HTML from {url}, {len(r.text)} bytes)"
        title = p.title()
        body = p.text()
        base = str(r.url)
        links = []
        for href, txt in p.links[:15]:
            if href and txt and not href.startswith(("javascript:", "mailto:", "#")):
                links.append(f"- [{txt[:60]}]({urljoin(base, href)})")
        out = f"# {title or parsed.netloc}\nSource: {base}\n\n{body}"
        if links:
            out += "\n\n## Links\n" + "\n".join(links)
    else:
        out = r.text   # plain text / json / csv etc.
    if len(out) > MAX_TOOL_OUTPUT:
        out = out[:MAX_TOOL_OUTPUT] + f"\n... (truncated, {len(out)} chars total)"
    return out


def _ddg_search_sync(query: str, api_key: str) -> list:
    """DuckDuckGo HTML results; returns [{title, url, snippet}]."""
    results = []
    if api_key:
        # future: Brave/Tavily key path — placeholder switch
        pass
    url = "https://html.duckduckgo.com/html/?q=" + quote_plus(query)
    with httpx.Client(follow_redirects=True, timeout=SEARCH_TIMEOUT_S,
                      headers={"User-Agent": UA}) as c:
        r = c.get(url)
    if r.status_code != 200:
        raise RuntimeError(f"DDG returned HTTP {r.status_code}")
    rx_item = re.compile(
        r'<a[^>]*class="result__a"[^>]*href="([^"]+)"[^>]*>(.*?)</a>', re.S)
    rx_snip = re.compile(r'<a[^>]*class="result__snippet"[^>]*>(.*?)</a>', re.S)
    rx_tag = re.compile(r"<[^>]+>")
    items = rx_item.findall(r.text)
    snips = rx_snip.findall(r.text)
    for i, (href, title) in enumerate(items[:MAX_SEARCH_RESULTS]):
        # DDG wraps urls in /l/?uddg=<encoded>
        m = re.search(r"[?&]uddg=([^&]+)", href)
        real = unquote(m.group(1)) if m else href
        snip = rx_tag.sub("", snips[i]) if i < len(snips) else ""
        title = rx_tag.sub("", title).strip()
        results.append({"title": title, "url": real, "snippet": " ".join(snip.split())[:250]})
    return results


def _bing_search_sync(query: str, count: int = 8) -> list:
    """Bing organic web search results fallback."""
    import base64
    url = "https://www.bing.com/search?q=" + quote_plus(query)
    with httpx.Client(follow_redirects=True, timeout=SEARCH_TIMEOUT_S,
                      headers={"User-Agent": UA, "Accept-Language": "en-US,en;q=0.9"}) as c:
        r = c.get(url)
    if r.status_code != 200:
        raise RuntimeError(f"Bing search returned HTTP {r.status_code}")
    results = []
    rx_tag = re.compile(r"<[^>]+>")
    for m in re.finditer(r'<li[^>]*class="[^"]*b_algo[^"]*"[^>]*>([\s\S]*?)</li>', r.text):
        li = m.group(1)
        h2 = re.search(r'<h2[^>]*>([\s\S]*?)</h2>', li)
        if not h2:
            continue
        a = re.search(r'<a[^>]*href="([^"]+)"[^>]*>([\s\S]*?)</a>', h2.group(1))
        if not a:
            continue
        href = a.group(1).replace("&amp;", "&")
        raw_title = a.group(2)
        title = rx_tag.sub("", raw_title).strip()

        # Decode Bing tracking URL: u=a1<base64>
        m_u = re.search(r"[?&]u=a1([a-zA-Z0-9_-]+)", href)
        if m_u:
            try:
                b64 = m_u.group(1).replace("-", "+").replace("_", "/")
                padded = b64 + "=" * (-len(b64) % 4)
                decoded = base64.b64decode(padded).decode("utf-8", errors="ignore")
                if decoded.startswith("http"):
                    href = decoded
            except Exception:
                pass

        p = re.search(r'<p[^>]*>([\s\S]*?)</p>', li)
        snip = rx_tag.sub("", p.group(1)).strip() if p else ""
        results.append({"title": title, "url": href, "snippet": " ".join(snip.split())[:250]})
        if len(results) >= count:
            break
    return results


def _bing_images_search_sync(query: str, count: int = 8) -> list:
    """Bing image search results."""
    url = "https://www.bing.com/images/search?q=" + quote_plus(query)
    with httpx.Client(follow_redirects=True, timeout=SEARCH_TIMEOUT_S,
                      headers={"User-Agent": UA, "Accept-Language": "en-US,en;q=0.9"}) as c:
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
    if isinstance(args, str):
        query = args.strip()
    elif isinstance(args, dict):
        query = (args.get("query") or args.get("q") or "").strip()
    else:
        query = str(args or "").strip()
    if not query:
        raise ValueError("query required")
    cfg = APP_CONFIG.get("capabilities", {})
    api_key = cfg.get("web_search_api_key", "")
    results = []
    used_engine = "DuckDuckGo"

    # Attempt DuckDuckGo first
    try:
        results = await asyncio.get_event_loop().run_in_executor(
            None, _ddg_search_sync, query, api_key)
    except Exception as e:
        # DDG failed (rate-limit, bot check, HTTP 202, or network) - fall back to Bing
        try:
            results = await asyncio.get_event_loop().run_in_executor(
                None, _bing_search_sync, query, MAX_SEARCH_RESULTS)
            used_engine = "Bing"
        except Exception as e_bing:
            return f"error: search failed: DDG ({e}), Bing fallback ({e_bing})"

    if not results:
        # If DDG yielded 0 results, try Bing before giving up
        if used_engine == "DuckDuckGo":
            try:
                results = await asyncio.get_event_loop().run_in_executor(
                    None, _bing_search_sync, query, MAX_SEARCH_RESULTS)
                if results:
                    used_engine = "Bing"
            except Exception:
                pass

    if not results:
        return f"(no results for: {query})"

    out = [f"Web results for: {query} (via {used_engine})", ""]
    for i, r in enumerate(results, 1):
        out.append(f"{i}. {r['title']}\n   {r['url']}")
        if r.get("snippet"):
            out.append(f"   {r['snippet']}")

    # If query specifically asks for a photo/image, also append top image suggestions
    lower_q = query.lower()
    if any(w in lower_q for w in ("photo", "picture", "image", "portrait", "look like")):
        try:
            img_results = await asyncio.get_event_loop().run_in_executor(
                None, _bing_images_search_sync, query, 3)
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
    if isinstance(args, str):
        query = args.strip()
    elif isinstance(args, dict):
        query = (args.get("query") or args.get("q") or "").strip()
    else:
        query = str(args or "").strip()
    if not query:
        raise ValueError("query required")
    try:
        results = await asyncio.get_event_loop().run_in_executor(
            None, _bing_images_search_sync, query, 6)
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
            "description": "Fetch a web page by URL and return its readable text content (HTML stripped). Use for docs, articles, APIs.",
            "parameters": {"type": "object",
                           "properties": {"url": {"type": "string", "description": "absolute URL, e.g. https://example.com/docs"}},
                           "required": ["url"]},
        }},
        source="web", meta={"label": "Web fetch"}, replace=True)
    registry.register(
        "web_search", tool_web_search,
        {"type": "function", "function": {
            "name": "web_search",
            "description": "Search the web and return top result titles, URLs and snippets.",
            "parameters": {"type": "object",
                           "properties": {"query": {"type": "string", "description": "search query"}},
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

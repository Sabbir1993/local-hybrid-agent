import base64
import re
import sys
from urllib.parse import unquote, urlencode
import httpx
from .constants import UA, SEARCH_TIMEOUT_S, SNIPPET_CHARS, RECENCY, cfg, region


def _html(text: str):
    import lxml.html
    return lxml.html.fromstring(text or "<html></html>")


def _clean(s: str) -> str:
    return " ".join((s or "").split())


def parse_ddg(html_text: str) -> list:
    """DuckDuckGo html endpoint -> [{title, url, snippet}]. Each result block is read on
    its own, so a result without a snippet can't shift snippets onto other URLs."""
    out = []
    for block in _html(html_text).xpath('//div[contains(concat(" ", normalize-space(@class), " "), " result ")]'):
        cls = block.get("class") or ""
        if "result--ad" in cls:
            continue
        a = block.xpath('.//a[contains(@class, "result__a")]')
        if not a:
            continue
        href = a[0].get("href") or ""
        m = re.search(r"[?&]uddg=([^&]+)", href)
        url = unquote(m.group(1)) if m else href
        if url.startswith("//"):
            url = "https:" + url
        if not url.startswith("http") or "duckduckgo.com/y.js" in url:
            continue
        snip = block.xpath('.//*[contains(@class, "result__snippet")]')
        out.append({"title": _clean(a[0].text_content()), "url": url,
                    "snippet": _clean(snip[0].text_content())[:SNIPPET_CHARS] if snip else ""})
    return out


def _bing_real_url(href: str) -> str:
    m = re.search(r"[?&]u=a1([a-zA-Z0-9_-]+)", href)
    if m:
        try:
            b64 = m.group(1).replace("-", "+").replace("_", "/")
            dec = base64.b64decode(b64 + "=" * (-len(b64) % 4)).decode("utf-8", errors="ignore")
            if dec.startswith("http"):
                return dec
        except Exception:
            pass
    return href


def parse_bing(html_text: str) -> list:
    out = []
    for li in _html(html_text).xpath('//li[contains(concat(" ", normalize-space(@class), " "), " b_algo ")]'):
        a = li.xpath(".//h2//a[@href]")
        if not a:
            continue
        url = _bing_real_url(a[0].get("href") or "")
        if not url.startswith("http"):
            continue
        snip = li.xpath('.//div[contains(@class, "b_caption")]//p') or li.xpath(".//p")
        out.append({"title": _clean(a[0].text_content()), "url": url,
                    "snippet": _clean(snip[0].text_content())[:SNIPPET_CHARS] if snip else ""})
    return out


def _get(url: str, headers: dict) -> httpx.Response:
    with httpx.Client(follow_redirects=True, timeout=SEARCH_TIMEOUT_S,
                      headers={"User-Agent": UA, **headers}) as c:
        return c.get(url)


def ddg_search(query: str, recency: str = "") -> list:
    mod = sys.modules.get("core.web_search")
    region_fn = getattr(mod, "region", region)
    get_fn = getattr(mod, "_get", _get)
    reg = region_fn()
    params = {"q": query, "kl": reg["ddg"]}
    if recency in RECENCY:
        params["df"] = RECENCY[recency][0]
    r = get_fn("https://html.duckduckgo.com/html/?" + urlencode(params), {"Accept-Language": reg["accept"]})
    if r.status_code != 200:
        raise RuntimeError(f"DuckDuckGo HTTP {r.status_code}")
    return parse_ddg(r.text)


def bing_search(query: str, recency: str = "") -> list:
    mod = sys.modules.get("core.web_search")
    region_fn = getattr(mod, "region", region)
    get_fn = getattr(mod, "_get", _get)
    reg = region_fn()
    params = {"q": query, "setlang": reg["bing_lang"], "count": "15"}
    if reg["bing_cc"]:
        params["cc"] = reg["bing_cc"]
    if recency in RECENCY and RECENCY[recency][1]:
        params["filters"] = RECENCY[recency][1]
    r = get_fn("https://www.bing.com/search?" + urlencode(params), {"Accept-Language": reg["accept"]})
    if r.status_code != 200:
        raise RuntimeError(f"Bing HTTP {r.status_code}")
    return parse_bing(r.text)


def searxng_search(query: str, recency: str = "") -> list:
    """Optional: an org-run SearXNG instance (web.searxng_url, JSON format enabled).
    It's a fixed admin-configured endpoint (often on the LAN), so it isn'tカードSSRF-checked."""
    mod = sys.modules.get("core.web_search")
    cfg_fn = getattr(mod, "cfg", cfg)
    region_fn = getattr(mod, "region", region)
    base = str(cfg_fn().get("searxng_url") or "").rstrip("/")
    if not base:
        return []
    params = {"q": query, "format": "json", "language": region_fn()["accept"].split(",")[0]}
    if recency in ("day", "week", "month", "year"):
        params["time_range"] = recency
    with httpx.Client(timeout=SEARCH_TIMEOUT_S, follow_redirects=False) as c:
        r = c.get(base + "/search", params=params)
    if r.status_code != 200:
        raise RuntimeError(f"SearXNG HTTP {r.status_code}")
    return [{"title": _clean(x.get("title")), "url": x.get("url") or "",
             "snippet": _clean(x.get("content"))[:SNIPPET_CHARS]}
            for x in (r.json().get("results") or []) if str(x.get("url") or "").startswith("http")]


BACKENDS = {"searxng": searxng_search, "duckduckgo": ddg_search, "bing": bing_search}

"""core/web_search.py - search backends, merging, reranking and page extraction.

Used by core/web_tools.py (web_search / web_fetch). Keyless and dependency-light:
lxml for parsing, the local nomic embedder for reranking, numpy for scoring.

Pipeline for a query:
  1. privacy: redact emails / phone numbers / long account-like digit runs
  2. backends in parallel: optional SearXNG (web.searxng_url), DuckDuckGo HTML, Bing HTML
     - region / language (web.region) and recency (day|week|month|year)
  3. merge by reciprocal-rank fusion, dedup on a normalized URL
  4. rerank: engine rank + embedding cosine + lexical overlap
  5. auto-read the top pages (web.auto_fetch_top) and attach their most relevant passages
Results are cached in-process for CACHE_TTL_S.
"""

import asyncio
import re
import sys
import time
from collections import OrderedDict
from typing import Optional
from urllib.parse import parse_qsl, quote_plus, unquote, urlencode, urljoin, urlparse

import httpx

from .net_guard import guarded_get
from .small_model import APP_CONFIG

UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"
SEARCH_TIMEOUT_S = 12
FETCH_MAX_BYTES = 5 * 1024 * 1024
MAX_RESULTS = 8
SNIPPET_CHARS = 300
PASSAGE_CHARS = 700
AUTO_FETCH_BUDGET_S = 7.0
CACHE_TTL_S = 600
CACHE_MAX = 256

# DuckDuckGo has no Bangladesh region, so "bd" leaves DDG worldwide and localizes
# through Bing (cc=BD) plus the Accept-Language header.
REGIONS = {
    "bd-en": {"ddg": "wt-wt", "bing_cc": "BD", "bing_lang": "en", "accept": "en-BD,en;q=0.9,bn;q=0.7"},
    "bd-bn": {"ddg": "wt-wt", "bing_cc": "BD", "bing_lang": "bn", "accept": "bn-BD,bn;q=0.9,en;q=0.7"},
    "us-en": {"ddg": "us-en", "bing_cc": "US", "bing_lang": "en", "accept": "en-US,en;q=0.9"},
    "uk-en": {"ddg": "uk-en", "bing_cc": "GB", "bing_lang": "en", "accept": "en-GB,en;q=0.9"},
    "in-en": {"ddg": "in-en", "bing_cc": "IN", "bing_lang": "en", "accept": "en-IN,en;q=0.9"},
    "wt-wt": {"ddg": "wt-wt", "bing_cc": "", "bing_lang": "en", "accept": "en;q=0.9"},
}
RECENCY = {"day": ("d", 'ex1:"ez1"'), "week": ("w", 'ex1:"ez2"'),
           "month": ("m", 'ex1:"ez3"'), "year": ("y", "")}


def cfg() -> dict:
    c = APP_CONFIG.get("web")
    return c if isinstance(c, dict) else {}


def region() -> dict:
    return REGIONS.get(str(cfg().get("region") or "bd-en").lower(), REGIONS["bd-en"])


# ---------------- cache ----------------
_cache: "OrderedDict[tuple, tuple]" = OrderedDict()


def cache_get(key: tuple):
    hit = _cache.get(key)
    if not hit:
        return None
    ts, val = hit
    if time.time() - ts > CACHE_TTL_S:
        _cache.pop(key, None)
        return None
    _cache.move_to_end(key)
    return val


def cache_put(key: tuple, val) -> None:
    _cache[key] = (time.time(), val)
    _cache.move_to_end(key)
    while len(_cache) > CACHE_MAX:
        _cache.popitem(last=False)


# ---------------- privacy on egress ----------------
_EMAIL_RX = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+")
# BD mobiles (01XXXXXXXXX / +8801XXXXXXXXX) and other international numbers
_PHONE_RX = re.compile(r"(?<![\w])(?:\+?880[\s-]?|0)1[3-9]\d{2}[\s-]?\d{6}(?!\d)|\+\d[\d\s-]{8,14}\d")
_LONG_DIGITS_RX = re.compile(r"(?<!\d)\d{10,}(?!\d)")     # account / wallet / transaction ids


def redact_query(q: str) -> tuple:
    """Returns (query, [kinds redacted]). Queries go to third-party engines."""
    if not cfg().get("redact_query_pii", True):
        return q, []
    kinds = []
    for kind, rx in (("email", _EMAIL_RX), ("phone number", _PHONE_RX), ("account-like number", _LONG_DIGITS_RX)):
        q2 = rx.sub(" ", q)
        if q2 != q:
            kinds.append(kind)
            q = q2
    return " ".join(q.split()), kinds


# ---------------- backends ----------------
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
    import base64
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
    reg = region()
    params = {"q": query, "kl": reg["ddg"]}
    if recency in RECENCY:
        params["df"] = RECENCY[recency][0]
    r = _get("https://html.duckduckgo.com/html/?" + urlencode(params), {"Accept-Language": reg["accept"]})
    if r.status_code != 200:
        raise RuntimeError(f"DuckDuckGo HTTP {r.status_code}")
    return parse_ddg(r.text)


def bing_search(query: str, recency: str = "") -> list:
    reg = region()
    params = {"q": query, "setlang": reg["bing_lang"], "count": "15"}
    if reg["bing_cc"]:
        params["cc"] = reg["bing_cc"]
    if recency in RECENCY and RECENCY[recency][1]:
        params["filters"] = RECENCY[recency][1]
    r = _get("https://www.bing.com/search?" + urlencode(params), {"Accept-Language": reg["accept"]})
    if r.status_code != 200:
        raise RuntimeError(f"Bing HTTP {r.status_code}")
    return parse_bing(r.text)


def searxng_search(query: str, recency: str = "") -> list:
    """Optional: an org-run SearXNG instance (web.searxng_url, JSON format enabled).
    It's a fixed admin-configured endpoint (often on the LAN), so it isn't SSRF-checked."""
    base = str(cfg().get("searxng_url") or "").rstrip("/")
    if not base:
        return []
    params = {"q": query, "format": "json", "language": region()["accept"].split(",")[0]}
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


# ---------------- merge + rerank ----------------
_TRACKING = re.compile(r"^(utm_|fbclid$|gclid$|msclkid$|ref$|ref_src$)")


def norm_url(u: str) -> str:
    p = urlparse(u)
    host = (p.netloc or "").lower()
    host = host[4:] if host.startswith("www.") else host
    q = urlencode([(k, v) for k, v in parse_qsl(p.query) if not _TRACKING.match(k)])
    return f"{host}{p.path.rstrip('/') or '/'}" + (f"?{q}" if q else "")


def merge(ranked_lists: dict) -> list:
    """Reciprocal-rank fusion across engines; a URL found by several engines ranks higher."""
    pool: dict = {}
    for engine, results in ranked_lists.items():
        for rank, r in enumerate(results):
            key = norm_url(r["url"])
            e = pool.get(key)
            if e is None:
                e = pool[key] = {**r, "engines": [], "rrf": 0.0}
            elif len(r.get("snippet") or "") > len(e.get("snippet") or ""):
                e["snippet"] = r["snippet"]
            e["engines"].append(engine)
            e["rrf"] += 1.0 / (60 + rank)
    return sorted(pool.values(), key=lambda x: -x["rrf"])


_STOP = set("a an and are as at be by for from how i in is it of on or that the this to was what when "
            "where which who why will with do does can you me my your latest current today".split())


def terms(text: str) -> set:
    return {t for t in re.findall(r"\w+", (text or "").lower()) if len(t) > 1 and t not in _STOP}


def lexical(query_terms: set, text: str) -> float:
    if not query_terms:
        return 0.0
    return len(query_terms & terms(text)) / len(query_terms)


def _minmax(vals: list) -> list:
    if not vals:
        return []
    lo, hi = min(vals), max(vals)
    return [0.0] * len(vals) if hi - lo < 1e-9 else [(v - lo) / (hi - lo) for v in vals]


async def embed(texts: list) -> Optional[list]:
    try:
        from .memory import _embed_texts
        return await _embed_texts(texts)
    except Exception:
        return None


def _cosines(qv, vecs) -> list:
    import numpy as np
    m = np.asarray(vecs, dtype=np.float32)
    q = np.asarray(qv, dtype=np.float32)
    denom = np.linalg.norm(m, axis=1) * (np.linalg.norm(q) or 1.0)
    denom[denom < 1e-9] = 1.0
    return (m @ q / denom).tolist()


async def rerank(query: str, items: list) -> list:
    if len(items) < 2:
        return items
    qt = terms(query)
    lex = [lexical(qt, f"{i['title']} {i['snippet']}") for i in items]
    rrf = _minmax([i["rrf"] for i in items])
    vecs = await embed([f"search_query: {query}"] +
                       [f"search_document: {i['title']}. {i['snippet']}" for i in items])
    if vecs:
        cos = _minmax(_cosines(vecs[0], vecs[1:]))
        scores = [0.4 * r + 0.4 * c + 0.2 * l for r, c, l in zip(rrf, cos, lex)]
    else:
        scores = [0.6 * r + 0.4 * l for r, l in zip(rrf, lex)]
    for i, s in zip(items, scores):
        i["score"] = round(s, 4)
    return sorted(items, key=lambda x: -x["score"])


# ---------------- page extraction ----------------
_DROP_TAGS = ("script", "style", "noscript", "svg", "iframe", "form", "button", "nav", "header",
              "footer", "aside", "template", "select", "input", "canvas")
_BOILER_RX = re.compile(r"(cookie|consent|gdpr|banner|subscribe|newsletter|sidebar|comment|share|social|"
                        r"advert|\bads?\b|promo|related|breadcrumb|\bmenu\b|navbar|popup|modal|footer|header)", re.I)
_META_CHARSET_RX = re.compile(rb"""<meta[^>]+charset=["']?\s*([\w-]+)""", re.I)


def decode_html(content: bytes, header_encoding: Optional[str]) -> str:
    enc = header_encoding
    if not enc:
        m = _META_CHARSET_RX.search(content[:4096])
        enc = m.group(1).decode("ascii", "ignore") if m else "utf-8"
    try:
        return content.decode(enc, errors="replace")
    except LookupError:
        return content.decode("utf-8", errors="replace")


def _text_len(el) -> int:
    return len(_clean(el.text_content()))


def _link_density(el) -> float:
    total = _text_len(el) or 1
    return sum(_text_len(a) for a in el.xpath(".//a")) / total


def _main_root(doc):
    for xp in ("//article", "//main", '//*[@role="main"]', '//*[@itemprop="articleBody"]'):
        cands = [c for c in doc.xpath(xp) if _text_len(c) > 400]
        if cands:
            return max(cands, key=_text_len)
    # readability-style: credit each paragraph's text to its parent and grandparent
    scores: dict = {}
    for p in doc.xpath("//p|//pre|//td|//li"):
        n = _text_len(p)
        if n < 25:
            continue
        for depth, anc in enumerate((p.getparent(), p.getparent().getparent() if p.getparent() is not None else None)):
            if anc is None:
                continue
            scores[anc] = scores.get(anc, 0.0) + n / (depth + 1)
    if not scores:
        return doc.body if doc.body is not None else doc
    best = max(scores, key=lambda el: scores[el] * (1 - _link_density(el)))
    return best


_BLOCKS = {"p", "div", "section", "li", "tr", "h1", "h2", "h3", "h4", "h5", "h6", "pre", "blockquote", "table", "dd", "dt"}


def _render(root) -> str:
    lines = []
    for el in root.iter():
        tag = el.tag if isinstance(el.tag, str) else ""
        if tag in ("h1", "h2", "h3", "h4"):
            t = _clean(el.text_content())
            if t:
                lines.append("\n## " + t)
        elif tag == "tr":
            cells = [_clean(c.text_content()) for c in el.xpath("./th|./td")]
            if any(cells):
                lines.append("| " + " | ".join(cells) + " |")
        elif tag == "li":
            t = _clean(el.text_content())
            if t:
                lines.append("- " + t)
        elif tag in ("p", "pre", "blockquote", "dd", "dt"):
            if el.xpath("ancestor::li|ancestor::tr"):
                continue
            t = _clean(el.text_content())
            if t:
                lines.append(t)
    text = "\n".join(lines).strip()
    if len(text) < 200:          # div-soup page without p/li: fall back to all text
        text = _clean(root.text_content())
    return re.sub(r"\n{3,}", "\n\n", text)


def extract_html(html_text: str, base_url: str) -> dict:
    """-> {title, text, links}: main content only (nav, cookie banners, sidebars dropped)."""
    doc = _html(html_text)
    title = _clean(" ".join(doc.xpath("//title//text()")))
    for bad in doc.xpath("|".join(f"//{t}" for t in _DROP_TAGS)):
        bad.drop_tree()
    for el in doc.xpath("//*[@class or @id]"):
        marker = f"{el.get('class') or ''} {el.get('id') or ''}"
        if _BOILER_RX.search(marker) and el.tag not in ("html", "body", "main", "article") \
                and _text_len(el) < 3000 and not el.xpath(".//article|.//main|.//h1"):
            el.drop_tree()
    root = _main_root(doc)
    links = []
    for a in root.xpath(".//a[@href]"):
        href, txt = a.get("href") or "", _clean(a.text_content())
        if txt and not href.startswith(("javascript:", "mailto:", "#")):
            links.append((urljoin(base_url, href), txt[:80]))
        if len(links) >= 15:
            break
    return {"title": title, "text": _render(root), "links": links}


def extract_response(r) -> dict:
    """guarded_get FetchResult -> {title, text, links, kind}."""
    ctype = (r.headers.get("content-type") or "").lower()
    if "pdf" in ctype or str(r.url).lower().split("?")[0].endswith(".pdf"):
        from . import doc_ops
        try:
            return {"title": "", "text": doc_ops.full_text(r.content, "page.pdf"), "links": [], "kind": "pdf"}
        except Exception as e:
            return {"title": "", "text": f"(PDF could not be read: {type(e).__name__})", "links": [], "kind": "pdf"}
    if "html" in ctype or "xml" in ctype or not ctype:
        try:
            return {**extract_html(decode_html(r.content, r.encoding), str(r.url)), "kind": "html"}
        except Exception:
            pass
    return {"title": "", "text": decode_html(r.content, r.encoding), "links": [], "kind": "text"}


def chunk(text: str, size: int = PASSAGE_CHARS) -> list:
    """Paragraph-aligned passages of roughly `size` chars."""
    out, cur = [], ""
    for para in re.split(r"\n+", text or ""):
        para = para.strip()
        if not para:
            continue
        if cur and len(cur) + len(para) + 1 > size:
            out.append(cur)
            cur = ""
        cur = f"{cur}\n{para}" if cur else para
        while len(cur) > size * 1.5:
            out.append(cur[:size])
            cur = cur[size:]
    if cur:
        out.append(cur)
    return out


async def best_passages(query: str, text: str, k: int = 2, max_chunks: int = 60) -> list:
    """Top-k passages of `text` for `query`, in document order."""
    chunks = chunk(text)[:max_chunks]
    if len(chunks) <= k:
        return chunks
    qt = terms(query)
    lex = [lexical(qt, c) for c in chunks]
    vecs = await embed([f"search_query: {query}"] + [f"search_document: {c}" for c in chunks])
    if vecs:
        cos = _cosines(vecs[0], vecs[1:])
        scores = [0.7 * c + 0.3 * l for c, l in zip(_minmax(cos), _minmax(lex))]
    else:
        scores = lex
    top = sorted(range(len(chunks)), key=lambda i: -scores[i])[:k]
    return [chunks[i] for i in sorted(top)]


def fetch_page(url: str, timeout: float, accept_language: str):
    return guarded_get(url, timeout, headers={"User-Agent": UA, "Accept-Language": accept_language},
                       max_bytes=FETCH_MAX_BYTES)


async def attach_passages(query: str, items: list, n: int) -> None:
    """Read the top-n result pages in parallel and add their best passages (in place)."""
    if n <= 0 or not items:
        return
    accept = region()["accept"]

    async def one(item):
        try:
            r = await asyncio.to_thread(fetch_page, item["url"], AUTO_FETCH_BUDGET_S, accept)
            if r.status_code >= 400:
                return
            page = extract_response(r)
            item["passages"] = await best_passages(query, page["text"])
        except Exception as e:   # blocked / slow / unparsable page: never fails the search
            print(f"[web] auto-fetch skipped {item['url'][:80]}: {type(e).__name__}", file=sys.stderr)

    tasks = [asyncio.ensure_future(one(i)) for i in items[:n]]
    done, pending = await asyncio.wait(tasks, timeout=AUTO_FETCH_BUDGET_S + 1)
    for t in pending:
        t.cancel()


_REWRITE_PROMPT = (
    "Rewrite the user's message as ONE concise web search query (keywords, max 12 words). "
    "Keep names, places, product/version numbers and the year if the message implies recent "
    "information. Reply with the query only, no quotes or explanation.")


async def rewrite_query(message: str, fallback: str) -> str:
    """Turn a conversational message into a search query -- the "Web search queries"
    job (core/lanes.py; the small executor by default, always local). Only used when
    that model is already running - never loads one just for this."""
    try:
        from . import lanes
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
    recency = recency if recency in RECENCY else ""
    n_fetch = int(cfg().get("auto_fetch_top", 3) if auto_fetch is None else auto_fetch)
    key = ("search", query.lower(), str(cfg().get("region") or "bd-en"), recency, n_fetch)
    hit = cache_get(key)
    if hit is not None:
        return hit
    order = [b for b in (cfg().get("backends") or ["searxng", "duckduckgo", "bing"]) if b in BACKENDS]
    if not cfg().get("searxng_url"):
        order = [b for b in order if b != "searxng"]
    results = await asyncio.gather(*(asyncio.to_thread(BACKENDS[b], query, recency) for b in order),
                                   return_exceptions=True)
    lists, errors = {}, {}
    for name, res in zip(order, results):
        if isinstance(res, Exception):
            errors[name] = f"{type(res).__name__}: {res}"[:200]
        elif res:
            lists[name] = res
    items = (await rerank(query, merge(lists)))[:MAX_RESULTS]
    await attach_passages(query, items, n_fetch)
    out = {"query": query, "engines": list(lists), "results": items, "errors": errors}
    if items:
        cache_put(key, out)
    return out

import asyncio
import re
import sys
from typing import Optional
from urllib.parse import urljoin
from ..net_guard import guarded_get
from .backends import _clean, _html
from .constants import (
    UA,
    FETCH_MAX_BYTES,
    PASSAGE_CHARS,
    AUTO_FETCH_BUDGET_S,
    _DROP_TAGS,
    _BOILER_RX,
    _META_CHARSET_RX,
    region,
)
from .rerank import embed, lexical, terms, _minmax, _cosines


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
        from .. import doc_ops
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
    embed_fn = getattr(sys.modules.get("core.web_search"), "embed", embed)
    vecs = await embed_fn([f"search_query: {query}"] + [f"search_document: {c}" for c in chunks])
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
    region_fn = getattr(sys.modules.get("core.web_search"), "region", region)
    accept = region_fn()["accept"]

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

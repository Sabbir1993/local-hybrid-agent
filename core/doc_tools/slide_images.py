"""Pictures for deck slides: web search first, the local image model second, otherwise none.

A slide asks for one with an `image:` line (core/doc_ops/pptx_ops/outline.py). This resolves those lines to bytes
BEFORE the deck is built, on the server, because only the server has the guards a fetch needs:

* web   - the user's web switch must be on (set per chat run); the search words are the slide's own text, never KB
          hits; a card number in them is refused and personal data is stripped (same rules as web_search_images);
          the picture is downloaded through core.image_proxy (only URLs this search just returned, public hosts only,
          size capped, real raster image), then re-decoded and shrunk. The source page is kept as the credit.
* image model - only when the image job's first model is local and ALREADY loaded (never auto-loaded, never cloud:
          generation can take minutes and a cloud model costs money / sends the prompt out).
* none  - the slide stays a text slide and the tool result says why.

Resolved pictures are cached in memory: doc_append rebuilds the whole deck on every call, and a second search or
generation per slide per call would be slow and (for the model) expensive.
"""
import asyncio
import contextvars
import io
import re
import time
from collections import OrderedDict
from typing import Optional
from urllib.parse import urlparse

from .. import doc_store
from ..doc_ops.pptx_ops import outline

MAX_IMAGES = 8                 # per deck build; the rest stay text slides
MAX_IMAGE_BYTES = 1_400_000    # per picture after re-encoding (a deck is written to a device in one 10 MB message)
MAX_SIDE = 1600
MIN_SIDE = 500                 # smaller web pictures look soft when stretched over half a slide
MAX_CANDIDATES = 14           # search results looked at; only relevant ones are downloaded
MAX_DOWNLOADS = 4              # downloads tried per slide, best match first
_CACHE_MAX = 48
_CACHE_BYTES = 40_000_000       # total picture bytes kept between builds
RESOLVE_BUDGET_S = 100.0       # all pictures of one build together
MIN_PICTURE_S = 5.0            # a picture that already started always gets at least this long
WEB_SLOTS = 3                  # web searches / downloads running at once

# the user's web switch for this chat run (set in routes/chat/run.py); decks only search the web when it is on
allow_web: contextvars.ContextVar = contextvars.ContextVar("deck_web_images", default=False)

# The chat route runs the (sync) doc tools in a worker thread with their own short-lived event loop, but the image
# model's HTTP client belongs to the server's main loop; generation is therefore handed back to that loop.
main_loop: contextvars.ContextVar = contextvars.ContextVar("deck_main_loop", default=None)

_cache: "OrderedDict[tuple, dict]" = OrderedDict()
_taken: contextvars.ContextVar = contextvars.ContextVar("deck_taken_pictures", default=None)
_sems: contextvars.ContextVar =contextvars.ContextVar("deck_image_slots", default=None)
_IMAGE_LINE = re.compile(r"(?mi)^\s*(?:image|picture|photo)\s*:")

_ASPECT = {"cover": "tall", "bullets": "square", "full": "wide", "section": "wide", "gallery": "wide"}


def wants_images(content: str) -> bool:
    return bool(_IMAGE_LINE.search(content or ""))


def _host(url: str) -> str:
    try:
        h = (urlparse(url).hostname or "").lower()
    except Exception:
        return ""
    return h[4:] if h.startswith("www.") else h


def prepare(data: bytes) -> Optional[bytes]:
    """Validate and shrink one picture to a small JPEG/PNG; None when it cannot be used."""
    from PIL import Image
    from ..media_images import InputError, clean_picture
    try:
        png = clean_picture(data)["png"]            # decodes with a pixel limit, drops EXIF / metadata
    except InputError:
        return None
    im = Image.open(io.BytesIO(png))
    im.load()
    if min(im.size) < MIN_SIDE and max(im.size) < MIN_SIDE * 1.6:
        return None
    im.thumbnail((MAX_SIDE, MAX_SIDE), Image.LANCZOS)
    if im.mode == "RGBA":
        out = io.BytesIO()
        im.save(out, format="PNG", optimize=True)
        return out.getvalue() if out.tell() <= MAX_IMAGE_BYTES else None
    im = im.convert("RGB")
    for q in (84, 72, 60, 48):
        out = io.BytesIO()
        im.save(out, format="JPEG", quality=q, optimize=True)
        if out.tell() <= MAX_IMAGE_BYTES:
            return out.getvalue()
    return None


async def _on_main_loop(coro):
    loop = main_loop.get()
    if loop is None or loop is asyncio.get_running_loop():
        return await coro
    return await asyncio.wrap_future(asyncio.run_coroutine_threadsafe(coro, loop))


# ---------------------------------------------------------------- relevance
# The first hit of an image search is often wrong: a YouTube thumbnail, a stock-site preview with a watermark, a
# product page, or just a different place. A picture that does not clearly belong to the slide is worse than none,
# so candidates are filtered (hosts that serve thumbnails / watermarked / product images) and must MENTION the topic
# (title, source page or file name) before one is used.
_BLOCKED_HOSTS = (
    "youtube.com", "youtu.be", "ytimg.com", "vimeo.com", "tiktok.com", "facebook.com", "fbcdn.net", "instagram.com",
    "pinterest.", "twitter.com", "x.com", "etsy.com", "etsystatic.com", "redbubble.com", "amazon.", "ebay.",
    "aliexpress.", "alibaba.", "shutterstock.com", "istockphoto.com", "gettyimages.", "alamy.com", "dreamstime.com",
    "depositphotos.com", "123rf.com", "stock.adobe.com", "vecteezy.com", "pngtree.com", "pngegg.com", "clipart",
    "teepublic.com", "walmart.com", "temu.com", "shopify.com",
)
_STOP = {"the", "and", "for", "with", "from", "that", "this", "are", "was", "has", "have", "into", "over", "its",
         "image", "images", "photo", "photos", "picture", "pictures", "pic", "hd", "jpg", "png", "jpeg", "www", "http",
         "https", "com"}
# words that are in a deck TITLE but are not its subject ("Welcome to Dhaka", "Executive Board Briefing")
_GENERIC = {"welcome", "introduction", "intro", "overview", "guide", "journey", "history", "brief", "briefing",
            "executive", "board", "presentation", "deck", "about", "your", "our", "why", "how", "what", "complete",
            "ultimate", "deep", "dive", "report", "summary", "update", "plan", "strategy", "review", "explained",
            "essential", "basics", "everything", "know", "need", "tour", "discover", "exploring", "explore"}


def _tokens(text: str) -> list:
    return [t for t in re.findall(r"[a-z0-9]{3,}", str(text or "").lower()) if t not in _STOP]


def anchors_for(topic: str) -> list:
    """The subject words of the deck title ("Welcome to Dhaka" -> ["dhaka"])."""
    return [t for t in _tokens(topic) if t not in _GENERIC]


def search_query(query: str, anchors: list) -> str:
    """The slide's words plus any deck-subject word they lack, so "river banks" becomes "river banks dhaka"."""
    have = set(_tokens(query))
    extra = [a for a in anchors if a not in have]
    return (query + " " + " ".join(extra)).strip() if extra else query


def blocked_host(url: str) -> bool:
    h = _host(url)
    return bool(h) and any(b in h for b in _BLOCKED_HOSTS)


def rank_candidates(results: list, query: str, anchors: list) -> list:
    """Candidates that are plausibly about the slide's subject, best first (empty = none good enough)."""
    qtoks = list(dict.fromkeys(_tokens(query)))
    need = max(1, -(-len(qtoks) * 6 // 10))              # ceil(60%) of the words
    scored = []
    for r in results or []:
        url, src = str(r.get("image_url") or ""), str(r.get("source_url") or "")
        if not url.lower().startswith("https://") or blocked_host(url) or blocked_host(src):
            continue
        hay = " ".join((str(r.get("title") or ""), src, url)).lower()
        a = sum(1 for t in anchors if t in hay)
        q = sum(1 for t in qtoks if t in hay)
        if (anchors and a >= 1 and q >= 2) or q >= need:
            scored.append((a * 3 + q, r))
    scored.sort(key=lambda x: -x[0])
    return [r for _s, r in scored]


async def _from_web(uid: int, query: str, why: list, anchors: tuple = ()) -> Optional[dict]:
    from .. import image_proxy, web_search as ws, web_tools
    if not allow_web.get():
        why.append("web search is off for this chat")
        return None
    if web_tools._pan_egress(query):
        why.append("the search words contain a card number")
        return None
    q, _ = ws.redact_query(search_query(query, list(anchors)))
    if not q:
        why.append("nothing left to search after removing personal data")
        return None
    slot = (_sems.get() or {}).get("web")
    try:
        if slot is not None:
            async with slot:
                results = await asyncio.to_thread(web_tools._bing_images_search_sync, q, MAX_CANDIDATES)
        else:
            results = await asyncio.to_thread(web_tools._bing_images_search_sync, q, MAX_CANDIDATES)
    except Exception as e:
        why.append(f"image search failed ({type(e).__name__})")
        return None
    good = rank_candidates(results, q, list(anchors))
    if not good:
        why.append("no search result clearly matched the topic")
        return None
    image_proxy.remember(uid, [r.get("image_url") for r in good])
    taken = _taken.get()
    for r in good[:MAX_DOWNLOADS + (len(taken) if taken else 0)]:
        url = str(r.get("image_url") or "")
        if taken is not None and url in taken:
            continue                                  # another slide of this deck already uses this picture
        if taken is not None:
            taken.add(url)                            # reserved now: slides resolve concurrently, so claim before downloading
        try:
            _ctype, raw = await asyncio.to_thread(image_proxy.fetch, uid, url)
            data = await asyncio.to_thread(prepare, raw)
        except Exception:
            data = None
        if data:
            src = str(r.get("source_url") or url)
            return {"url": url, "data": data, "credit_host": _host(src) or _host(url), "credit_url": src, "via": "web"}
        if taken is not None:
            taken.discard(url)                        # unusable: free it for another slide
    why.append("no usable picture came back from the search")
    return None


async def _generate(uid: int, query: str, aspect: str, why: list, anchors: tuple = ()) -> Optional[dict]:
    from .. import media, media_tools
    st = (media.status(uid) or {}).get("image") or {}
    if not st.get("ready"):
        why.append("no image model is set up")
        return None
    if st.get("cloud"):
        why.append("the image model is a cloud one (not used for decks)")
        return None
    if st.get("state") != "loaded":
        why.append("the image model is not loaded")
        return None
    user = media_tools._user()
    if user is None:
        return None
    prompt = f"{search_query(query, list(anchors))}, clean professional presentation visual, high quality, no text, no watermark"
    gen_slot = (_sems.get() or {}).get("gen")
    try:
        if gen_slot is not None:
            async with gen_slot:                     # the image model makes one picture at a time
                res = await _on_main_loop(media.generate("image", user, prompt, {"aspect": aspect, "size": "medium"}))
        else:
            res = await _on_main_loop(media.generate("image", user, prompt, {"aspect": aspect, "size": "medium"}))
    except Exception as e:
        why.append(f"image generation failed ({type(e).__name__})")
        return None
    files = res.get("files") or []
    if not files:
        return None
    from ..agent_tools import _common_resolve
    try:
        raw = _common_resolve(str(files[0].get("path"))).read_bytes()
    except Exception:
        why.append("the generated picture could not be read back")
        return None
    data = await asyncio.to_thread(prepare, raw)
    if not data:
        why.append("the generated picture was unusable")
        return None
    return {"data": data, "credit_host": "AI-generated", "credit_url": "", "via": "generated"}


async def _one(uid: int, spec: dict, kind: str, why: list, anchors: tuple = ()) -> Optional[dict]:
    query = spec.get("query") or ""
    source = spec.get("source") or "auto"
    aspect = _ASPECT.get(kind, "square")
    if not query:
        why.append("no description given")
        return None
    key = (uid, source, aspect, query.lower(), tuple(anchors))
    hit = _cache.get(key)
    if hit:
        _cache.move_to_end(key)
        taken = _taken.get()
        if taken is None or not hit.get("url") or hit["url"] not in taken:
            if taken is not None and hit.get("url"):
                taken.add(hit["url"])
            return hit
        # same words as an earlier slide: look for a different picture, leave the cache entry alone
        res = await _from_web(uid, query, why, anchors) if source in ("auto", "search") else None
        return res or hit
    res = None
    if source in ("auto", "search"):
        res = await _from_web(uid, query, why, anchors)
    if res is None and source in ("auto", "generate"):
        res = await _generate(uid, query, aspect, why, anchors)
    if res is not None:
        _cache[key] = res
        while len(_cache) > _CACHE_MAX or sum(len(v["data"]) for v in _cache.values()) > _CACHE_BYTES:
            _cache.popitem(last=False)
    return res


async def _resolve_many(uid: int, items: list, anchors: tuple, noun: str) -> tuple:
    """items: [(key, label, kind, spec | None, extras [spec])] -> (images {key: info}, notes, used_web).

    Slides are resolved concurrently (web searches 3 at a time, the image model one at a time) inside an overall time
    budget, so a deck with 8 pictures does not take 8 sequential searches. A picture that cannot be found, errors, or
    runs out of time never fails the build: that slide stays text-only and a note says why. `extras` (gallery slides) are
    resolved alongside the first picture and returned in info["extra"]."""
    images: dict = {}
    notes: list = []
    sems = {"web": asyncio.Semaphore(WEB_SLOTS), "gen": asyncio.Semaphore(1)}
    token = _sems.set(sems)
    taken_token = _taken.set(set())                    # picture URLs already used by this deck: no slide repeats another's
    deadline = time.monotonic() + RESOLVE_BUDGET_S
    todo = []
    for n, (key, label, kind, spec, extras) in enumerate(items):
        if spec is None:
            notes.append(f"{label}: no description given")
        elif n >= MAX_IMAGES:
            notes.append(f"{label}: skipped, at most {MAX_IMAGES} pictures per {noun}")
        else:
            todo.append((key, label, kind, spec, extras))

    async def one(spec, kind):
        why: list = []
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return None, "skipped, the picture time budget ran out"
        try:
            res = await asyncio.wait_for(_one(uid, spec, kind, why, anchors), timeout=max(MIN_PICTURE_S, remaining))
        except asyncio.TimeoutError:
            return None, "took too long"
        except Exception as e:                      # one bad picture must not stop the build
            return None, type(e).__name__
        return res, (why[-1] if why else "unavailable")

    async def run(key, label, kind, spec, extras):
        got = await asyncio.gather(one(spec, kind), *[one(x, kind) for x in extras])
        return key, label, got

    try:
        done = await asyncio.gather(*[run(*t) for t in todo])
    finally:
        _sems.reset(token)
        _taken.reset(taken_token)
    used_web = False
    for key, label, got in done:
        (first, why), rest = got[0], got[1:]
        extra = [r for r, _w in rest]
        if first is None:
            first = next((r for r in extra if r), None)           # a gallery still shows what it found
            extra = [r for r in extra if r and r is not first]
        if first is None:
            notes.append(f"{label}: no picture - {why}")
            doc_store.audit(uid, "doc.image", label, {"via": "none", "reason": str(why)[:80]})
            continue
        first = dict(first)
        if any(extra):
            first["extra"] = [r for r in extra if r]
        images[key] = first
        for r in [first] + list(first.get("extra", [])):
            used_web = used_web or r["via"] == "web"
            doc_store.audit(uid, "doc.image", label, {"via": r["via"], "host": r.get("credit_host", "")})
        for r, w in rest:
            if r is None:
                notes.append(f"{label}: one of its pictures was not found - {w}")
    return images, notes, used_web


async def resolve(content: str, uid: int) -> tuple:
    """-> (images {slide index: {data, credit_host, credit_url, via[, extra]}}, notes [str], used_web bool).
    Never raises for a slide that cannot get a picture: it stays a text slide and a note says why."""
    deck = outline.parse_outline(content or "")
    anchors = tuple(anchors_for(deck.get("title", "")))
    items, notes = [], []
    for i, s in enumerate(deck["slides"]):
        spec = s.get("image")
        if not spec:
            continue
        label = f"slide {i + 1} ({s['title'][:30]})"
        if s["kind"] not in outline.IMAGE_KINDS:
            notes.append(f"{label}: this kind of slide has no room for a picture (use a plain bullets slide)")
            continue
        extras = list(s.get("image_extra") or []) if s["kind"] == "gallery" else []
        items.append((i, label, s["kind"], spec, extras))
    images, more, used_web = await _resolve_many(uid, items, anchors, "deck")
    return images, notes + more, used_web


async def resolve_pdf(content: str, uid: int) -> tuple:
    """Same as resolve() for a PDF's markdown: its `image:` lines, in order (core/agent_tools/dom_convert.image_specs).
    -> (images {line index: info}, notes, used_web). Pictures are wide figures between paragraphs."""
    from ..agent_tools.dom_convert import image_specs
    anchors = tuple(anchors_for(next((ln[2:].strip() for ln in (content or "").splitlines() if ln.startswith("# ")), "")))
    items = [(i, f"picture {i + 1}", "full", spec, []) for i, spec in enumerate(image_specs(content or ""))]
    return await _resolve_many(uid, items, anchors, "document")


def summary(images: dict, notes: list, used_web: bool) -> str:
    """Lines appended to the doc_create / doc_append result."""
    out = []
    if images:
        web = sum(1 for v in images.values() if v["via"] == "web")
        gen = len(images) - web
        bits = ([f"{web} from the web"] if web else []) + ([f"{gen} generated here"] if gen else [])
        out.append(f"Pictures: {len(images)} added ({', '.join(bits)}).")
    out += [f"Picture note - {n}" for n in notes]
    if used_web:
        out.append("Web pictures show their source on the slide and in the notes - tell the user to check the "
                   "licence before sharing the deck outside the company.")
    return " ".join(out)

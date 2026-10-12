"""Markdown / loose outline -> a clean deck model (cover, sections, cards, stats, ...).

Models write many shapes of outline ("# Title", "Slide 3 (Scale): Header \"X\"", "**Label:** text").
This turns any of them into at most MAX_DECK_SLIDES slides with inline markup stripped, meta lines
dropped and a layout inferred per slide, so the renderer never sees raw markdown.
"""
import re
from . import theme as _theme

MAX_DECK_SLIDES = 30
MAX_BULLETS = 12
MAX_CARDS = 6
MAX_TABLE_ROWS = 9
MAX_TABLE_COLS = 6

LAYOUTS = {"bullets", "cards", "stats", "two-column", "timeline", "quote", "section", "table", "cover", "closing"}
_LAYOUT_ALIAS = {"columns": "two-column", "two-columns": "two-column", "twocolumn": "two-column", "2-column": "two-column",
                 "list": "bullets", "content": "bullets", "numbers": "stats", "metrics": "stats", "steps": "timeline",
                 "roadmap": "timeline", "grid": "cards", "title": "cover", "divider": "section", "thanks": "closing",
                 "end": "closing"}

_META = re.compile(
    r"^(markdown source|source for conversion|here(?:'s| is| are)\b|below is|sure[,!. ]|certainly|of course|"
    r"presentation (?:outline|content|structure)|\d+\s*slides?\b.*(?:structure|outline|total|\+|:)\s*$)", re.I)
_FOOTER_META = re.compile(
    r"^(end of (?:the )?(?:presentation|deck|slides?)|total slides?.*|estimated (?:presentation )?time.*|"
    r"format\s*:.*(?:markdown|pptx|powerpoint|convert).*|\(?end\)?)$", re.I)
_EMOJI = re.compile("[\U0001F000-\U0001FAFF☀-➿⬀-⯿️‍⃣]")
_ARROW_ONLY = re.compile(r"^[\s↓↑→←⇒⇓⬇▼▲|│┃:·.\-–—]+$")
_CHECKBOX = re.compile(r"^\[[ xX✓✔]?\]\s*")
_SLIDE_PREFIX = re.compile(r"^(?:slide|section|part)\s*\d+\s*[:.\-–—)]*\s*", re.I)
_GLOBAL_THEME = re.compile(r"^(?:global\s+(?:theme|style|design)|style|design|palette|colou?r palette|colou?rs?)\s*:\s*(.*)$", re.I)
_FIELD = re.compile(r"^(subtitle|tagline|theme|accent|layout|notes?|speaker notes?|title)\s*:\s*(.*)$", re.I)
_SLIDE_MARK = re.compile(r"^\**\s*slide\s+(\d+)\s*(?:\(([^)]*)\))?\s*[:.\-–—]*\**\s*(.*)$", re.I)
_BULLET = re.compile(r"^([-*+•●▪◦]|\d+[.)])\s+(.*)$")
_TABLE_SEP = re.compile(r"^\|?\s*:?-{2,}:?\s*(\|\s*:?-{2,}:?\s*)*\|?$")
_CLOSING = re.compile(r"\b(thank|thanks|questions?|q\s*&\s*a|contact|get in touch|let'?s talk|next steps?)\b", re.I)
_STAT_HEAD = re.compile(r"^([~<>≈]?[$€£৳]?\s?\d[\d.,]*\s?(?:%|[KMBkmb]\+?|x|\+|ms|s|GB|TB|MB|k)?\+?)\s*(?:[-–—:]\s*|\s+)(.+)$")
_LABEL = re.compile(r"^(.{2,48}?)\s*(?::|\s[-–—]\s)\s*(.+)$")


def clean(s: str) -> str:
    """Strip markdown inline markup and stray symbols from one line of text."""
    s = re.sub(r"!\[[^\]]*\]\([^)]*\)", "", s)
    s = re.sub(r"\[([^\]]+)\]\([^)]*\)", r"\1", s)
    s = re.sub(r"(\*\*|__)(.+?)\1", r"\2", s)
    s = re.sub(r"(?<![\w*])\*(?!\s)(.+?)(?<!\s)\*(?![\w*])", r"\1", s)
    s = re.sub(r"(?<![\w_])_(?!\s)(.+?)(?<!\s)_(?![\w_])", r"\1", s)
    s = s.replace("`", "").replace("**", "").replace("__", "")
    s = _EMOJI.sub("", s)
    s = re.sub(r"^\*+(?=\S)|(?<=\S)\*+$", "", s.strip())      # emphasis opened on one line and closed on the next
    return re.sub(r"\s+", " ", s).strip()


def _unquote(s: str) -> str:
    s = s.strip()
    m = re.match(r'^["“”\'‘’](.+?)["“”\'‘’]\.?$', s)
    return m.group(1).strip() if m else s


def pretty_title(name: str) -> str:
    n = re.sub(r"\.[A-Za-z0-9]{2,5}$", "", name or "")
    n = re.sub(r"[-_ ][0-9a-f]{6,10}$", "", n)
    return re.sub(r"[_\-]+", " ", n).strip()


def _new() -> dict:
    return {"title": "", "subtitle": "", "bullets": [], "notes": "", "layout": "", "table": []}


def _empty(sl: dict) -> bool:
    return not (sl["title"] or sl["bullets"] or sl["table"] or sl["subtitle"] or sl["notes"])


def _slide_title_from_marker(cat: str, rest: str) -> str:
    quoted = re.search(r'(?:header(?:\s+banner)?|title)\s*(?:[^:"“]+:|\s*)?\s*["“]([^"”]+)["”]', rest, re.I)
    if quoted:
        return quoted.group(1).strip()
    q = re.findall(r'["“]([^"”]+)["”]', rest)
    rest = re.sub(r"^(?:title|cover|header)\s*[:\-–—]\s*", "", clean(rest), flags=re.I)
    if cat and not rest.startswith(("Title", "title")) and not q:
        return clean(cat)
    if q:
        return q[0].strip()
    return _unquote(rest) or clean(cat) or ""


_COVER_TITLE = re.compile(r"^(?:title(?:\s+(?:page|slide))?|cover(?:\s+(?:page|slide))?)$", re.I)


def _slide_heading_level(lines: list) -> int:
    """Which '#' depth starts a new slide. 'SLIDE n' headings decide it; else the shallowest depth (or the next one
    when a single top heading is just the deck title). Deeper headings are labelled groups inside a slide."""
    heads, marked, fence = [], [], False
    for ln in lines:
        st = ln.strip()
        if st.startswith("```"):
            fence = not fence
        m = None if fence else re.match(r"^(#{1,6})\s+(.+)$", st)
        if m:
            heads.append(len(m.group(1)))
            if _SLIDE_PREFIX.match(clean(m.group(2))):
                marked.append(len(m.group(1)))
    if marked:
        return min(marked)
    if not heads:
        return 1
    top = min(heads)
    if heads.count(top) == 1 and heads.count(top + 1) >= 2:
        return top + 1
    return top


def parse_outline(md: str, deck_title: str = "") -> dict:
    deck = {"title": "", "subtitle": "", "theme": "", "accent": "", "hint": "", "slides": []}
    slides: list[dict] = []
    cur = _new()
    group_open = False          # a "Label:" line is waiting for its sub-bullets

    def flush():
        nonlocal cur, group_open
        if not _empty(cur):
            slides.append(cur)
        cur, group_open = _new(), False

    lines = (md or "").replace("\t", "    ").splitlines()
    slide_level = _slide_heading_level(lines)

    for raw in lines:
        st = raw.strip()
        if not st or st.startswith("```"):
            continue
        if re.match(r"^([-*_])\1{2,}$", st) or re.match(r"^-{3,}\s*$", st):
            flush()
            continue
        st = re.sub(r"^(?:>\s*)+", "", st)                  # blockquote markers are not slide text
        st = _CHECKBOX.sub("", st)
        if not st or _ARROW_ONLY.match(st):                  # "↓" connectors of ASCII flow diagrams
            continue
        plain = clean(st)
        if not plain or _FOOTER_META.match(plain) or (_empty(cur) and _META.match(plain)):
            continue
        g = _GLOBAL_THEME.match(plain)
        if g and not st.startswith(("-", "#")):
            deck["hint"] = (deck["hint"] + " " + g.group(1)).strip()
            continue
        if st.startswith("|"):
            if not _TABLE_SEP.match(st):
                cells = [clean(c) for c in st.strip("|").split("|")]
                if len(cur["table"]) < MAX_TABLE_ROWS:
                    cur["table"].append(cells[:MAX_TABLE_COLS])
            continue
        m = re.match(r"^(#{1,6})\s+(.+)$", st)
        if m:
            text = clean(m.group(2)).strip("# ")
            if len(m.group(1)) > slide_level:               # a sub-heading inside a slide: a labelled group
                label = _SLIDE_PREFIX.sub("", text).rstrip(":").strip()
                if label and not _FOOTER_META.match(label):
                    if cur["title"] or cur["bullets"] or cur["table"]:
                        cur["bullets"].append(label + ("" if label[-1] in "!?." else ":"))
                        group_open = True
                    else:
                        cur["title"] = label
                continue
            title = _SLIDE_PREFIX.sub("", text).strip() or text
            if _FOOTER_META.match(title):
                continue
            if _COVER_TITLE.match(title):                    # "SLIDE 1: TITLE PAGE" is the cover marker, not a title
                continue
            if slides == [] and cur["title"] and not cur["bullets"] and not cur["subtitle"] and not _SLIDE_PREFIX.match(text) \
                    and len(m.group(1)) == slide_level:       # "# Deck" then "## Tagline": the tagline is the cover subtitle
                cur["subtitle"] = title
                continue
            if cur["title"] or cur["bullets"] or cur["table"]:
                flush()
            cur["title"] = title
            continue
        m = _SLIDE_MARK.match(st)
        if m and (m.group(2) is not None or st.rstrip("*").rstrip().endswith(":") or m.group(3)):
            flush()
            cur["title"] = _slide_title_from_marker(m.group(2) or "", m.group(3) or "")
            continue
        m = _FIELD.match(plain)
        if m and not _BULLET.match(st):
            key, val = m.group(1).lower(), _unquote(m.group(2))
            if key in ("subtitle", "tagline"):
                cur["subtitle"] = val
            elif key == "theme":
                deck["theme"] = val
            elif key == "accent":
                deck["accent"] = val
            elif key == "layout":
                lay = re.sub(r"[^a-z0-9\- ]", "", val.lower()).strip().replace(" ", "-")
                lay = _LAYOUT_ALIAS.get(lay, lay)
                cur["layout"] = lay if lay in LAYOUTS else ""
            elif key == "title":
                if not cur["title"]:
                    cur["title"] = val
            else:
                cur["notes"] = (cur["notes"] + " " + val).strip()
            continue
        bm = _BULLET.match(st)
        if bm:
            indent = (len(raw) - len(raw.lstrip(" "))) // 2
            txt = _CHECKBOX.sub("", clean(bm.group(2)))
            if not txt or _FOOTER_META.match(txt):
                continue
            if group_open:
                indent = max(indent, 1)
            cur["bullets"].append("\t" * min(indent, 3) + txt)
            continue
        if not plain:
            continue
        # prose line
        if not slides and not cur["bullets"] and cur["title"] and not cur["subtitle"]:
            cur["subtitle"] = plain
        elif plain.endswith(":") and len(plain) <= 60:
            cur["bullets"].append(plain.rstrip(":").strip() + ":")
            group_open = True
        elif group_open:
            cur["bullets"].append("	" + plain)
        else:
            cur["bullets"].append(plain)
    flush()

    return _finish(deck, slides, deck_title)


def _finish(deck: dict, slides: list, deck_title: str) -> dict:
    title = pretty_title(deck_title)
    slides = [s for s in slides if not _empty(s)]
    # slides without a title belong to the previous one (a 1-line-per-slide dump collapses here)
    merged: list[dict] = []
    for s in slides:
        if not s["title"] and merged and not s["layout"]:
            prev = merged[-1]
            prev["bullets"] += ([s["subtitle"]] if s["subtitle"] else []) + s["bullets"]
            prev["table"] = prev["table"] or s["table"]
            prev["notes"] = (prev["notes"] + " " + s["notes"]).strip()
        else:
            merged.append(s)
    slides = merged
    for s in slides:
        if not s["title"]:
            s["title"] = s["subtitle"] or (s["bullets"].pop(0).strip("\t: ") if s["bullets"] else "")
            if s["title"] == s["subtitle"]:
                s["subtitle"] = ""
    # keep slides that have a title but are exact title-only duplicates out of the way
    if slides and (len([b for b in slides[0]["bullets"] if not b.startswith("\t")]) > 3 or slides[0]["table"]) and title:
        slides.insert(0, {**_new(), "title": title, "layout": "cover"})
    if not slides:
        slides = [{**_new(), "title": title or "Presentation", "layout": "cover"}]
    # fold stray text-only slides that only repeat the cover (e.g. "Subtitle: ..." fragments)
    for s in slides:
        s["bullets"] = s["bullets"][:MAX_BULLETS * 2]
    while len(slides) > MAX_DECK_SLIDES:
        victim = min(range(1, len(slides) - 1), key=lambda i: _weight(slides[i]))
        v, prev = slides.pop(victim), slides[victim - 1]
        line = v["title"].rstrip(":") + (": " + v["bullets"][0].strip().rstrip(":") if v["bullets"] else "")
        prev["bullets"].append(line)
        prev["notes"] = (prev["notes"] + " " + v["notes"]).strip()
        prev["layout"] = prev["layout"] if prev["layout"] in ("", "bullets") else ""
    for i, s in enumerate(slides):
        _classify(s, i, len(slides))
    deck["slides"] = slides
    deck["title"] = slides[0]["title"]
    deck["subtitle"] = slides[0]["subtitle"]
    return deck


def _weight(s: dict) -> int:
    return len(s["bullets"]) + (4 if s["table"] else 0) + (2 if s["layout"] in ("cards", "stats", "timeline") else 0)


def groups(bullets: list) -> list:
    """Split a bullet list into (heading, [children]) by top-level items; flat items give children=[]."""
    out = []
    for b in bullets:
        if b.startswith("\t"):
            if out:
                out[-1][1].append(b.strip())
            else:
                out.append(["", [b.strip()]])
        else:
            out.append([b.strip(), []])
    return [(h, c) for h, c in out]


def label_split(text: str):
    m = _LABEL.match(text)
    if m and len(m.group(1).split()) <= 5:
        return m.group(1).strip(), m.group(2).strip()
    return None


def stat_split(text: str):
    m = _STAT_HEAD.match(text)
    return (m.group(1).strip(), m.group(2).strip()) if m else None


def _classify(s: dict, idx: int, n: int) -> None:
    lay = s["layout"]
    top = groups(s["bullets"])
    flat = [h for h, c in top if h and not c]
    if idx == 0 and lay in ("", "cover", "bullets") and not s["table"] and len(top) <= 3 and (s["subtitle"] or len(top) <= 2):
        s["kind"] = "cover"
        if not s["subtitle"]:
            s["subtitle"] = " · ".join(h for h, _ in top)
        s["bullets"] = []
        return
    if idx == n - 1 and n > 2 and lay in ("", "closing") and not s["table"] and len(top) <= 4 \
            and (_CLOSING.search(s["title"]) or lay == "closing"):
        s["kind"] = "closing"
        return
    if lay == "section" or (lay == "" and not s["bullets"] and not s["table"] and idx > 0):
        s["kind"] = "section"
        return
    if s["table"] and lay in ("", "table"):
        s["kind"] = "table"
        return
    if lay == "quote" and s["bullets"]:
        s["kind"] = "quote"
        return
    if lay == "timeline" and 2 <= len(top) <= 6:
        s["kind"] = "timeline"
        return
    if lay in ("", "cards") and 2 <= len(top) <= MAX_CARDS and all(c for _, c in top) and all(h for h, _ in top):
        s["kind"] = "cards"
        return
    if lay in ("", "stats") and 2 <= len(top) <= 4 and flat and len(flat) == len(top) and all(stat_split(h) for h in flat):
        s["kind"] = "stats"
        return
    label_cards = bool(flat) and len(flat) == len(top) and all(label_split(h) for h in flat)
    if label_cards and ((lay == "cards" and 2 <= len(top) <= MAX_CARDS) or (lay == "" and 3 <= len(top) <= 4)):
        s["kind"] = "cards"
        return
    if lay == "two-column" or (lay == "" and 7 <= len(top) <= MAX_BULLETS):
        s["kind"] = "columns"
        return
    s["kind"] = "bullets"


def parse_markdown_slides(md: str) -> list[dict]:
    """Back-compat: [{title, bullets, notes}] for callers that only need the outline."""
    return [{"title": s["title"], "bullets": s["bullets"], "notes": s["notes"]} for s in parse_outline(md)["slides"]]


def resolve_theme(deck: dict) -> dict:
    return _theme.resolve(deck.get("theme", ""), deck.get("accent", ""), deck.get("hint", ""))

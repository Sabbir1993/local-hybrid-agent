"""Markdown / loose outline -> a clean deck model (cover, sections, cards, stats, ...).

Models write many shapes of outline ("# Title", "Slide 3 (Scale): Header \"X\"", "**Label:** text").
This turns any of them into at most MAX_DECK_SLIDES slides with inline markup stripped, meta lines
dropped and a layout inferred per slide, so the renderer never sees raw markdown.
"""
import re
from . import notes as _notes
from . import theme as _theme

MAX_DECK_SLIDES = 30
MAX_BULLETS = 12
MAX_CARDS = 6
MAX_TABLE_ROWS = 120         # read in full: design._prepare splits a long table across slides (header repeated)
MAX_TABLE_COLS = 6

LAYOUTS = {"bullets", "cards", "stats", "two-column", "timeline", "quote", "section", "table", "cover", "closing", "full",
           "chart", "comparison", "agenda", "gallery", "textside",
           "kpi-grid", "chart-kpis", "leaderboard", "roadmap", "dimension-table", "process", "matrix", "funnel"}
IMAGE_KINDS = ("cover", "bullets", "section", "full", "gallery")   # slide kinds the renderer can put a picture on
MAX_IMAGE_QUERY = 120
_LAYOUT_ALIAS = {"columns": "two-column", "two-columns": "two-column", "twocolumn": "two-column", "2-column": "two-column",
                 "list": "bullets", "content": "bullets", "numbers": "stats", "metrics": "stats", "steps": "timeline",
                 "grid": "cards", "title": "cover", "divider": "section", "thanks": "closing",
                 "end": "closing", "split": "bullets", "image": "bullets", "picture": "bullets", "full-image": "full",
                 "fullscreen": "full", "hero": "full", "versus": "comparison", "vs": "comparison",
                 "contents": "agenda", "toc": "agenda", "graph": "chart", "photos": "gallery", "images": "gallery",
                 "pictures": "gallery", "text-table": "textside", "split-table": "textside", "side-table": "textside",
                 "text-chart": "textside", "text-side": "textside",
                 "kpi-grid": "kpi-grid", "kpigrid": "kpi-grid", "kpi": "kpi-grid", "kpis": "kpi-grid", "dashboard": "kpi-grid",
                 "chart-kpis": "chart-kpis", "chartkpis": "chart-kpis", "chart-cards": "chart-kpis", "chart-delta": "chart-kpis",
                 "leaderboard": "leaderboard", "profiles": "leaderboard", "team": "leaderboard", "champions": "leaderboard",
                 "process": "process", "flow": "process", "chevrons": "process", "pipeline": "process", "workflow": "process",
                 "matrix": "matrix", "2x2": "matrix", "quadrant": "matrix", "quadrants": "matrix", "swot": "matrix",
                 "funnel": "funnel", "conversion": "funnel",
                 "roadmap": "roadmap", "stepper": "roadmap", "timeline-stepper": "roadmap", "milestones": "roadmap",
                 "dimension-table": "dimension-table", "dimension": "dimension-table", "table-callout": "dimension-table", "alert-table": "dimension-table"}

_META = re.compile(
    r"^(markdown source|source for conversion|here(?:'s| is| are)\b|below is|sure[,!. ]|certainly|of course|"
    r"presentation (?:outline|content|structure)|\d+\s*slides?\b.*(?:structure|outline|total|\+|:)\s*$)", re.I)
_FOOTER_META = re.compile(
    r"^(end of (?:the )?(?:presentation|deck|slides?)|total slides?.*|estimated (?:presentation )?time.*|"
    r"format\s*:.*(?:markdown|pptx|powerpoint|convert).*|\(?end\)?)$", re.I)
# What models paste into a deck that is not deck content: ASCII-art boxes (draw as garbage in a text box) and raw XML
# (they sometimes dump the package parts they just inspected). Those lines are dropped before anything is parsed.
_ASCII_ART = re.compile("[\u2500-\u259F]")
_XML_LINE = re.compile(r"^\s*<(\?xml|/?[A-Za-z][\w.-]*:[\w.-]+|presentation\b|sldLayout\b|sldMaster\b)")
# Slides ABOUT the deck rather than FOR the audience ("Design Notes for This Deck", "Image Sources Used")
_META_SLIDE = re.compile(
    r"^(?:design|layout|visual|production|speaker|build|style|image|photo|picture)s?\s+"
    r"(?:notes?|sources?|credits?|urls?|references?|guide|instructions?|used|applied)\b"
    r"|^(?:notes?|instructions?)\s+(?:for|on|about)\s+(?:this|the)\s+(?:deck|presentation|slides?)\b"
    r"|^about\s+this\s+(?:deck|presentation)\b", re.I)
_EMOJI = re.compile("[\U0001F000-\U0001FAFF☀-➿⬀-⯿️‍⃣]")
_ARROW_ONLY = re.compile(r"^[\s↓↑→←⇒⇓⬇▼▲|│┃:·.\-–—]+$")
_CHECKBOX = re.compile(r"^\[[ xX✓✔]?\]\s*")
_SLIDE_PREFIX = re.compile(r"^(?:slide|section|part)\s*\d+\s*[:.\-–—)]*\s*", re.I)
_GLOBAL_THEME = re.compile(r"^(?:global\s+(?:theme|style|design)|style|design|palette|colou?r palette|colou?rs?)\s*:\s*(.*)$", re.I)
_FIELD = re.compile(r"^(subtitle|tagline|theme|accent|layout|notes?|speaker notes?|title|image|picture|photo|chart|graph|font|eyebrow|kicker|source|sources|art|motif|mode|callout|alert)\s*:\s*(.*)$", re.I)
_SLIDE_MARK = re.compile(r"^\**\s*slide\s+(\d+)\s*(?:\(([^)]*)\))?\s*[:.\-–—]*\**\s*(.*)$", re.I)
_BULLET = re.compile(r"^([-*+•●▪◦]|\d+[.)])\s+(.*)$")
_TABLE_SEP = re.compile(r"^\|?\s*:?-{2,}:?\s*(\|\s*:?-{2,}:?\s*)*\|?$")
_CLOSING = re.compile(r"\b(thank|thanks|questions?|q\s*&\s*a|contact|get in touch|let'?s talk|next steps?)\b", re.I)
_STAT_HEAD = re.compile(r"^([~<>≈]?[$€£৳]?\s?\d[\d.,]*\s?(?:%|km²|km2|sq\s?km|km|m²|m2|mi|hrs?|yrs?|years?|days?|min|[KMBTkmbt]\+?|x|\+|ms|s|GB|TB|MB)?\+?)\s*(?:\|\s*|[-–—:]\s*|\s+)(.+)$")
_LABEL = re.compile(r"^(.{2,48}?)\s*(?::|\s[-–—]\s|\s\|\s)\s*(.+)$")


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


# `[Wikipedia](https://...)` pasted into a bullet: models copy their research citations into the slide text. The link is
# dropped from the text and its NAME becomes the slide's `source:` line (when it has none); bare URLs are dropped too.
_CITE = re.compile(r"\s*\[([^\]\n]{2,60})\]\((?:[^)\s]*?(?:https?:|www\.|\.[a-z]{2,}/)[^)]*)\)", re.I)
_BARE_URL = re.compile(r"\s*\bhttps?://\S+", re.I)


def _strip_citations(text: str, found: list) -> str:
    found.extend(m.strip() for m in _CITE.findall(text))
    return re.sub(r"[\s/;,|]+$", "", _BARE_URL.sub("", _CITE.sub("", text)))


def _new() -> dict:
    return {"title": "", "subtitle": "", "bullets": [], "notes": "", "layout": "", "table": [], "image": None,
            "image_extra": [], "chart": None, "eyebrow": "", "source": "", "art": "", "callout": "", "alert": "", "_cite": []}


CHART_KINDS = ("bar", "hbar", "line", "area", "pie", "doughnut", "share")
ART_KINDS = ("arches", "waves", "skyline", "geometric", "network", "none")
_NUM = re.compile(r"^[~<>≈≥≤]?\s*[$€£৳¥]?\s*(-?\d[\d,]*(?:\.\d+)?)\s*(%|thousand|million|billion|trillion|bn|mn|[KkMmBbTt](?![A-Za-z]))?\s*[A-Za-z%/]{0,6}$")
_MULT = {"k": 1e3, "m": 1e6, "b": 1e9, "t": 1e12, "bn": 1e9, "mn": 1e6, "thousand": 1e3, "million": 1e6,
         "billion": 1e9, "trillion": 1e12}


def parse_number(text: str):
    """'40%' -> 40.0, '$10.8T' -> 1.08e13, '1,200' -> 1200.0, 'about 5' -> None."""
    m = _NUM.match(str(text or "").strip())
    if not m:
        return None
    try:
        v = float(m.group(1).replace(",", ""))
    except ValueError:
        return None
    suffix = (m.group(2) or "").lower()
    return v * _MULT.get(suffix, 1.0)


def parse_chart_spec(val: str):
    """`chart: bar | unit=% | title=...` -> {kind, unit, title}; the first word is the chart kind (default bar)."""
    parts = [p.strip() for p in str(val or "").split("|")]
    head = re.sub(r"[^a-z]", "", parts[0].lower())
    kind = {"column": "bar", "columns": "bar", "bars": "bar", "horizontal": "hbar", "horizontalbar": "hbar",
            "lines": "line", "trend": "line", "donut": "doughnut", "ring": "doughnut", "areas": "area", "stacked": "share",
            "stackedbar": "share", "composition": "share"}.get(head, head)
    spec = {"kind": kind if kind in CHART_KINDS else "bar", "unit": "", "title": "", "xlabel": "", "ylabel": "",
            "highlight": ""}
    for p in parts[1:]:
        k, _, v = p.partition("=")
        k, v = k.strip().lower(), v.strip().strip("\"'")
        if k == "unit":
            spec["unit"] = v[:10]
        elif k == "title":
            spec["title"] = clean(v)[:80]
        elif k in ("xlabel", "ylabel", "highlight"):        # axis titles; the one bar / slice to accent
            spec[k] = clean(v)[:60]
    return spec


_POINT = re.compile(r"^(.{1,60}?)\s*(?::|=|\s[-–—]\s)\s*(.+)$")


def chart_data(s: dict):
    """Labels + numeric series for a slide that asked for a chart: from its table (first column = labels, every fully
    numeric other column = a series) or from `Label: number` bullets. None when there is not enough real data."""
    spec = s.get("chart") or {}
    rows = s.get("table") or []
    labels, series = [], []
    if len(rows) >= 3 and max(len(r) for r in rows) >= 2:
        body = rows[1:]
        for c in range(1, max(len(r) for r in rows)):
            vals = [parse_number(r[c]) if c < len(r) else None for r in body]
            if all(v is not None for v in vals):
                series.append({"name": clean(rows[0][c]) if c < len(rows[0]) else f"Series {c}", "values": vals})
        if series:
            labels = [clean(r[0]) for r in body]
    if not series:
        pts = []
        for b in s.get("bullets", []):
            if b.startswith("\t"):
                continue
            m = _POINT.match(b.strip())
            if m and parse_number(m.group(2)) is not None:
                pts.append((m.group(1).strip(), parse_number(m.group(2)), m.group(2)))
        if len(pts) >= 2:
            labels = [p[0] for p in pts]
            series = [{"name": spec.get("title") or "", "values": [p[1] for p in pts]}]
            if not spec.get("unit") and all(str(p[2]).strip().endswith("%") for p in pts):
                spec["unit"] = "%"
    if len(labels) < 2 or not series:
        return None
    kind = spec.get("kind", "bar")
    if kind in ("pie", "doughnut", "share") and (len(series) != 1 or len(labels) > 8 or any(v < 0 for v in series[0]["values"])):
        kind = "bar"
    return {"kind": kind, "unit": spec.get("unit", ""), "labels": labels[:24], "series": series[:4],
            "title": spec.get("title", "") if (s.get("table") or len(series) > 1) else "",
            "xlabel": spec.get("xlabel", ""), "ylabel": spec.get("ylabel", ""), "highlight": spec.get("highlight", "")}


def parse_image_spec(val: str):
    """`image: search words | source=search|generate | fit=cover|contain | caption=...` -> a spec dict or None.
    `source` defaults to auto (web first, then the local image model); a https URL in place of the words is kept
    as `url` (it is only used if a search already returned it, see doc_tools/slide_images.py)."""
    parts = [p.strip() for p in str(val or "").split("|")]
    head = parts[0].strip("\"' ")
    spec = {"query": "", "url": "", "source": "auto", "fit": "cover", "caption": ""}
    if re.match(r"^https://\S+$", head):
        spec["url"] = head
    else:
        spec["query"] = re.sub(r"\s+", " ", clean(head))[:MAX_IMAGE_QUERY]
    for p in parts[1:]:
        k, _, v = p.partition("=")
        k, v = k.strip().lower(), v.strip().strip("\"'")
        if k == "source" and v.lower() in ("auto", "search", "web", "generate", "ai"):
            spec["source"] = {"web": "search", "ai": "generate"}.get(v.lower(), v.lower())
        elif k == "fit" and v.lower() in ("cover", "contain"):
            spec["fit"] = v.lower()
        elif k == "caption":
            spec["caption"] = clean(v)[:140]
    return spec if (spec["query"] or spec["url"]) else None


def _empty(sl: dict) -> bool:
    return not (sl["title"] or sl["bullets"] or sl["table"] or sl["subtitle"] or sl["notes"] or sl.get("callout") or sl.get("alert"))


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
    deck = {"title": "", "subtitle": "", "theme": "", "accent": "", "hint": "", "font": "", "mode": "", "slides": []}
    slides: list[dict] = []
    cur = _new()
    group_open = False          # a "Label:" line is waiting for its sub-bullets

    def flush():
        nonlocal cur, group_open
        if cur["_cite"] and not cur["source"]:
            names = list(dict.fromkeys(cur["_cite"]))
            cur["source"] = "; ".join(names)[:170]
        cur.pop("_cite", None)
        if not _empty(cur):
            slides.append(cur)
        cur, group_open = _new(), False

    lines = (md or "").replace("\t", "    ").splitlines()
    slide_level = _slide_heading_level(lines)

    for raw in lines:
        st = raw.strip()
        if not st or st.startswith("```"):
            continue
        if _ASCII_ART.search(st) or _XML_LINE.match(st):
            continue
        if not _FIELD.match(clean(st)) or _BULLET.match(st):          # field lines (image: https://...) keep their text
            st = _strip_citations(st, cur["_cite"]).strip()
            if not st:
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
                if len(cells) > MAX_TABLE_COLS and not cur["table"]:
                    _notes.note(f"table on '{cur['title'][:40]}': only the first {MAX_TABLE_COLS} of {len(cells)} columns "
                                f"fit on a slide (split the table or drop columns)")
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
            elif key == "font":
                if re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9 \-]{1,38}", val.strip()):
                    deck["font"] = val.strip()
            elif key == "accent":
                deck["accent"] = val
            elif key == "layout":
                lay = re.sub(r"[^a-z0-9\- ]", "", val.lower()).strip().replace(" ", "-")
                lay = _LAYOUT_ALIAS.get(lay, lay)
                cur["layout"] = lay if lay in LAYOUTS else ""
            elif key == "title":
                if not cur["title"]:
                    cur["title"] = val
            elif key in ("image", "picture", "photo"):
                spec = parse_image_spec(val)
                if spec and not cur["image"]:
                    cur["image"] = spec
                elif spec and len(cur["image_extra"]) < 2:      # a 2nd / 3rd `image:` line = more pictures (gallery)
                    cur["image_extra"].append(spec)
            elif key in ("chart", "graph"):
                cur["chart"] = parse_chart_spec(val)
            elif key in ("eyebrow", "kicker"):
                cur["eyebrow"] = clean(val).upper()[:44]
            elif key in ("source", "sources"):
                cur["source"] = clean(val)[:170]
            elif key in ("art", "motif"):
                a = re.sub(r"[^a-z]", "", val.lower())
                cur["art"] = a if a in ART_KINDS else ""
            elif key == "mode":
                deck["mode"] = "designer" if re.sub(r"[^a-z]", "", val.lower()) in ("designer", "image", "images", "visual") else ""
            elif key == "callout":
                cur["callout"] = clean(val)[:400]
            elif key == "alert":
                cur["alert"] = clean(val)[:400]
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
    slides = [s for i, s in enumerate(slides) if i == 0 or not _META_SLIDE.match((s["title"] or "").strip())]
    # slides without a title belong to the previous one (a 1-line-per-slide dump collapses here)
    merged: list[dict] = []
    for s in slides:
        if not s["title"] and merged and not s["layout"]:
            prev = merged[-1]
            prev["bullets"] += ([s["subtitle"]] if s["subtitle"] else []) + s["bullets"]
            prev["table"] = prev["table"] or s["table"]
            prev["image"] = prev["image"] or s["image"]
            prev["chart"] = prev["chart"] or s["chart"]
            prev["source"] = prev["source"] or s["source"]
            prev["callout"] = prev.get("callout") or s.get("callout", "")
            prev["alert"] = prev.get("alert") or s.get("alert", "")
            prev["notes"] = (prev["notes"] + " " + s["notes"]).strip()
        else:
            merged.append(s)
    slides = merged
    for s in slides:
        if not s["title"]:
            s["title"] = s["subtitle"] or (split_fields(s["bullets"].pop(0))[0].strip("\t: ") if s["bullets"] else "")
            if s["title"] == s["subtitle"]:
                s["subtitle"] = ""
    # keep slides that have a title but are exact title-only duplicates out of the way
    if slides and (len([b for b in slides[0]["bullets"] if not b.startswith("\t")]) > 3 or slides[0]["table"]) and title:
        slides.insert(0, {**_new(), "title": title, "layout": "cover"})
    if not slides:
        slides = [{**_new(), "title": title or "Presentation", "layout": "cover"}]
    # fold stray text-only slides that only repeat the cover (e.g. "Subtitle: ..." fragments)
    for s in slides:
        if len(s["bullets"]) > MAX_BULLETS * 2:
            _notes.note(f"'{s['title'][:40]}': {len(s['bullets']) - MAX_BULLETS * 2} lines past the first {MAX_BULLETS * 2} "
                        f"were left out (split the slide in two)")
        s["bullets"] = s["bullets"][:MAX_BULLETS * 2]
    if len(slides) > MAX_DECK_SLIDES:
        _notes.note(f"the outline had {len(slides)} slides; a deck holds {MAX_DECK_SLIDES}, so the lightest "
                    f"{len(slides) - MAX_DECK_SLIDES} were folded into the slide before them")
    while len(slides) > MAX_DECK_SLIDES:
        victim = min(range(1, len(slides) - 1), key=lambda i: _weight(slides[i]))
        v, prev = slides.pop(victim), slides[victim - 1]
        line = v["title"].rstrip(":") + (": " + v["bullets"][0].strip().rstrip(":") if v["bullets"] else "")
        prev["bullets"].append(line)
        prev["notes"] = (prev["notes"] + " " + v["notes"]).strip()
        prev["layout"] = prev["layout"] if prev["layout"] in ("", "bullets") else ""
    for i, s in enumerate(slides):
        _classify(s, i, len(slides))
    _vary(slides)
    deck["slides"] = slides
    deck["title"] = slides[0]["title"]
    deck["subtitle"] = slides[0]["subtitle"]
    return deck


def _vary(slides: list) -> None:
    """Three plain bullet slides in a row read as a wall of text. The third one becomes cards (when its bullets are
    `Label: text`) or two columns (4+ bullets), so the deck has rhythm even when the model wrote only bullets.
    Slides with a picture, an explicit `layout:` or few bullets are left alone."""
    run = 0
    for s in slides:
        if s.get("kind") != "bullets" or s.get("image") or s.get("layout"):
            run = 0
            continue
        run += 1
        if run < 3:
            continue
        top = groups(s["bullets"])
        flat = [h for h, c in top if h and not c]
        if 3 <= len(top) <= 4 and len(flat) == len(top) and all(label_split(h) for h in flat):
            s["kind"], run = "cards", 0
        elif len(top) >= 4:
            s["kind"], run = "columns", 0


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


# ------------------------------------------------------------------ build fields
# `tone=green`, `status=done`, `icon=growth` ... ride on an item as one `|` segment. They steer the drawing and are never text.
FIELD_KEYS = ("tone", "status", "icon", "color", "colour", "accent", "style", "variant")
_FIELD_SEG = re.compile(r"^(%s)\s*=" % "|".join(FIELD_KEYS), re.I)


def split_fields(text: str) -> tuple:
    """Take the build fields off an item: 'A | B | tone=blue #2' -> ('A | B', {'tone': 'blue #2'}). A field is a whole
    `|` segment that starts `key=` with a key in FIELD_KEYS (so everything after the `=` goes with it)."""
    raw = str(text or "")
    if "=" not in raw:
        return raw.strip(), {}
    keep, fields = [], {}
    for seg in raw.split("|"):
        st = seg.strip()
        m = _FIELD_SEG.match(st)
        if m:
            key = m.group(1).lower()
            fields.setdefault("color" if key == "colour" else key, st.split("=", 1)[1].strip())
        else:
            keep.append(st)
    if not fields:
        return raw.strip(), {}
    return " | ".join(p for p in keep if p), fields


_DELTA_MARK = re.compile(r"^(?:\^|v\s|[↑↓▲▼+\-])", re.I)
_DELTA_STRONG = re.compile(r"^(?:\^|v\s|[↑↓▲▼])")
_DELTA_WORD = re.compile(r"(?i)\b(up|down|rise|rose|fell|fall|drop|dropped|gain|gained|increase|decrease|growth|decline)\b")


def is_delta(part: str, strong: bool = False) -> bool:
    """Does one `|` part read as a change ('^ 1.2 pts', 'v 3%', '+18%', 'down 4 pts') rather than a caption?
    strong: only the explicit arrow markers count (^ v up/down arrows)."""
    p = str(part or "").strip()
    if not p:
        return False
    if _DELTA_STRONG.match(p):
        return True
    if strong:
        return False
    return bool(_DELTA_MARK.match(p) or (re.search(r"pts|%", p) and _DELTA_WORD.search(p)))


def arrow(part: str) -> str:
    """'^ 1.2 pts' -> '↑ 1.2 pts', 'v 3%' -> '↓ 3%' (other text unchanged)."""
    p = str(part or "").strip()
    p = re.sub(r"^\^\s*", "↑ ", p)
    return re.sub(r"^v\s+(?=[\d$€£৳])", "↓ ", p)


def kpi_parts(item: str) -> tuple:
    """(value, delta, caption, tone) of one KPI item, build fields taken off first. 1 part = value; 2 = value + caption
    (a leading ^ / v / arrow marks the 2nd as a delta instead); 3+ = value, delta, caption when the 2nd part looks like a
    delta, else everything after the value is the caption."""
    text, fields = split_fields(item)
    parts = [p.strip() for p in text.split("|") if p.strip()]
    if len(parts) == 1 and not _value_like(parts[0]) and stat_split(parts[0]):
        parts = list(stat_split(parts[0]))
    value, delta, caption = (parts[0] if parts else ""), "", ""
    if len(parts) == 2:
        if is_delta(parts[1], strong=True):
            delta = parts[1]
        else:
            caption = parts[1]
    elif len(parts) >= 3:
        if is_delta(parts[1]):
            delta, caption = parts[1], " · ".join(parts[2:])
        else:
            caption = " · ".join(parts[1:])
    tone = (fields.get("tone", "").split() or [""])[0].lower()
    return value, arrow(delta), caption, tone


_VALUE = re.compile(r"^[~<>≈+\-]?\s?[$€£৳]?\s?\d[\d.,]*\s?(?:%|[KMBTkmbt]n?\+?|x|\+|ms|s|sec|min|hrs?|GB|TB|MB|pts?|km)?\+?$")


def _value_like(p: str) -> bool:
    p = str(p or "").strip()
    return len(p) <= 12 and bool(_VALUE.match(p)) and not re.fullmatch(r"(?:19|20)\d\d", p)


def _join_plain(parts: list) -> str:
    """['A', 'B', 'C'] -> 'A: B · C' (a part that ends in ':' or has one keeps the first join readable)."""
    parts = [arrow(p) if _DELTA_STRONG.match(p) else p for p in parts]
    if len(parts) == 1:
        return parts[0]
    head = parts[0]
    sep = " " if head.endswith(":") else (" · " if ":" in head else ": ")
    return head + sep + " · ".join(parts[1:])


def _plain(text: str) -> str:
    """An item as readable text for deciding its layout: fields off, pipes kept."""
    return split_fields(text)[0]


_RAW_KINDS = {"kpi-grid", "chart-kpis", "roadmap"}      # their draw functions read the fields themselves
_FIELDS_ONLY = {"leaderboard", "chart"}                   # the parts mean something there; only fields come off
_KEEP_FIRST = {"stats", "process", "matrix", "funnel"}    # `value|heading | detail`: the first `|` stays
_CARDISH = {"cards", "stats", "process", "matrix", "funnel", "timeline"}
_ICON_KINDS = {"cards", "stats"}


def _normalise(s: dict) -> None:
    """Once the slide's layout is decided: no build field and no raw `|` is left in the text the layout will print.
    `A | B | C` becomes `A: B · C` (plain layouts) or keeps the one `|` its layout reads (value|caption, heading|detail)."""
    kind = s.get("kind")
    if kind in _RAW_KINDS or not s["bullets"]:
        return
    lines = []
    for b in s["bullets"]:
        tabs = b[:len(b) - len(b.lstrip("\t"))]
        txt, f = split_fields(b.strip())
        parts = [p.strip() for p in txt.split("|") if p.strip()]
        if parts:
            lines.append([tabs, parts, f.get("icon", "")])
    tops = [l for l in lines if not l[0]]
    if kind in _CARDISH and len(tops) >= 3 and all(len(l[1]) >= 3 for l in tops) \
            and [l[1][0] for l in tops] == [str(i + 1) for i in range(len(tops))]:
        for l in tops:                                      # `1 | Phase | detail`: the running number is not a heading
            l[1] = l[1][1:]
    out = []
    for i, (tabs, parts, icon) in enumerate(lines):
        has_kids = not tabs and i + 1 < len(lines) and bool(lines[i + 1][0])
        if kind in _FIELDS_ONLY:
            text = " | ".join(parts)
        elif kind in _KEEP_FIRST and not tabs and not has_kids and len(parts) > 1 and ":" not in parts[0]:
            rest = parts[1:]
            if kind == "stats" and len(rest) >= 2 and is_delta(rest[0], strong=True):
                rest = rest[1:] + [rest[0]]                 # caption first, the change after it
            text = parts[0] + " | " + " · ".join(arrow(p) if _DELTA_STRONG.match(p) else p for p in rest)
        else:
            text = _join_plain(parts)
        if kind in _ICON_KINDS and not tabs and icon and re.fullmatch(r"[A-Za-z0-9 _-]{1,24}", icon):
            text = f"icon={icon} | {text}"
        out.append(tabs + text)
    s["bullets"] = out


def _roadmap_like(fl: list) -> bool:
    """Items (text, fields) that read as milestones: all carry `status=`, or `1 | Phase | Date | ...` counting up."""
    if not 3 <= len(fl) <= 8:
        return False
    if all(f.get("status") for _, f in fl):
        return True
    rows = [[p.strip() for p in t.split("|") if p.strip()] for t, _ in fl]
    return all(len(r) >= 3 for r in rows) and [r[0] for r in rows] == [str(i + 1) for i in range(len(rows))]


def _kpi_like(fl: list, lay: str) -> bool:
    """Items (text, fields) that read as KPI tiles: all carry `tone=`, or 3+ items `Value | ... | Caption` with a short
    number first (for the layouts that can already show value|caption - '' and stats - only when an item has a delta)."""
    n = len(fl)
    if all(f.get("tone") for _, f in fl) and 2 <= n <= 8:
        return True
    rows = [[p.strip() for p in t.split("|") if p.strip()] for t, _ in fl]
    if not (3 <= n <= 8 and all(len(r) >= 2 and _value_like(r[0]) for r in rows)):
        return False
    return lay in ("bullets", "cards") or any(len(r) >= 3 for r in rows)


# ------------------------------------------------------------------ automatic charts
AUTO_CHART = True            # `Label: number` bullets / a numeric table with no `chart:` line are drawn as a chart
_YEAR = re.compile(r"^(?:19|20)\d\d$")
_QUARTER = re.compile(r"(?i)^(?:Q([1-4])(?:\s*[-'’/]?\s*((?:19|20)?\d\d))?|((?:19|20)\d\d)\s*[-/ ]?\s*Q([1-4]))$")
_MONTHS = ("jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec")


def _time_series(labels: list) -> bool:
    """Years, quarters (Q1.. / 'Q1 2025') or month names, strictly in order."""
    labs = [str(x).strip() for x in labels]
    if all(_YEAR.match(x) for x in labs):
        keys = [int(x) for x in labs]
    elif all(_QUARTER.match(x) for x in labs):
        keys = []
        for x in labs:
            m = _QUARTER.match(x)
            q, yr = (m.group(1), m.group(2)) if m.group(1) else (m.group(4), m.group(3))
            yr = int(yr) if yr else 0
            keys.append((yr + 2000 if 0 < yr < 100 else yr, int(q)))
    else:
        idx = [next((i for i, mo in enumerate(_MONTHS) if x.lower().startswith(mo)), -1) for x in labs]
        if -1 in idx or not all(re.fullmatch(r"(?i)[a-z]{3,9}\.?(?:\s*['’]?\d{2,4})?", x) for x in labs):
            return False
        keys = idx
    return all(a < b for a, b in zip(keys, keys[1:]))


def _unit_sig(text: str) -> tuple:
    """(currency, unit) of one number as written: '$5M' -> ('$', ''), '97%' -> ('', '%'), '310 ms' -> ('', 'ms')."""
    t = str(text or "").strip()
    cur = re.match(r"^[~<>≈≥≤]?\s*([$€£৳¥])", t)
    rest = re.sub(r"^[~<>≈≥≤]?\s*[$€£৳¥]?\s*-?\d[\d,]*(?:\.\d+)?\s*", "", t)
    mm = re.match(r"(?i)(thousand|million|billion|trillion|bn|mn|[kmbt](?![a-z]))\s*", rest)
    return (cur.group(1) if cur else "", (rest[mm.end():] if mm else rest).strip().lower())


def _is_year_value(text: str) -> bool:
    return bool(re.fullmatch(r"[~<>≈]?\s*(?:19|20)\d\d", str(text).strip()))


AUTO_CHART_MAX_POINTS = 8


def _auto_chart(s: dict, lay: str, idx: int, n: int):
    """A chart spec + data for a slide the model wrote as numbers without `chart:`, or None. Only when every point is a
    comparable quantity: all bullets `Label: number` (3-8), or a table of labels + 1-3 numeric columns (3-8 rows)."""
    if not AUTO_CHART or idx == 0 or lay not in ("", "bullets") or s["image"] or s["chart"] or s.get("alert") or s.get("callout"):
        return None
    if idx == n - 1 and _CLOSING.search(s["title"] or ""):
        return None
    bullets = s["bullets"]
    rows = s["table"]
    if rows:
        body = rows[1:]
        # 8 points at most: beyond that the chart hides its numbers, and the table that held them is not drawn
        if len(bullets) > 3 or any(b.startswith("\t") for b in bullets) or not 3 <= len(body) <= AUTO_CHART_MAX_POINTS:
            return None
        width = max(len(r) for r in rows)
        if len(rows[0]) != width or any(len(r) != width for r in body):
            return None
        probe = {**s, "chart": {"kind": "bar", "unit": "", "title": ""}}
        data = chart_data(probe)
        if not data or not 1 <= len(data["series"]) <= 3 or len(data["series"]) != width - 1:
            return None                                     # a text column would be lost in the chart
        if len(data["series"]) > 1 and len(body) > 8:
            return None                                     # more than 8 labels hide the numbers of a multi-series chart
        sigs = {_unit_sig(r[c]) for r in body for c in range(1, width)}
        if len({u for _, u in sigs}) > 1 or len({c for c, _ in sigs}) > 1:
            return None
        if any(_is_year_value(r[c]) for r in body for c in range(1, width)):
            return None
        tops = [max(abs(v) for v in ser["values"]) for ser in data["series"]]
        if len(tops) > 1 and min(tops) and max(tops) / min(tops) > 5:
            return None                                     # a 10 and a 1000 on one axis (temperature next to rainfall): not comparable
        if len(data["series"]) == 1:
            raw = [r[1] for r in body]
        else:
            raw = []
        labels = data["labels"]
        series = data["series"]
    else:
        if len(bullets) < 3 or len(bullets) > AUTO_CHART_MAX_POINTS or any(b.startswith("\t") for b in bullets):
            return None
        probe = {**s, "chart": {"kind": "bar", "unit": "", "title": ""}, "table": []}
        data = chart_data(probe)
        if not data or len(data["labels"]) != len(bullets) or len(data["series"]) != 1:
            return None                                     # every bullet must be a plain `Label: number`
        raw = [m.group(2) for m in (_POINT.match(b.strip()) for b in bullets)]
        sigs = {_unit_sig(x) for x in raw}
        if len({u for _, u in sigs}) > 1 or len({c for c, _ in sigs}) > 1 or any(_is_year_value(x) for x in raw):
            return None
        labels, series = data["labels"], data["series"]
    vals = series[0]["values"]
    kind = "bar"
    if _time_series(labels):
        kind = "line"
    elif len(series) == 1 and raw and len(labels) <= 6 and all(str(x).strip().endswith("%") for x in raw) \
            and all(v >= 0 for v in vals) and 99 <= sum(vals) <= 101:
        kind = "doughnut"
    spec = {"kind": kind, "unit": data.get("unit", ""), "title": "", "xlabel": "", "ylabel": "", "highlight": ""}
    final = chart_data({**s, "chart": spec})
    return (spec, final) if final else None


def _classify(s: dict, idx: int, n: int) -> None:
    _pick_kind(s, idx, n)
    _normalise(s)


def _pick_kind(s: dict, idx: int, n: int) -> None:
    lay = s["layout"]
    # build fields (`tone=`, `status=`, `icon=`) on an item are decoration: the layout is chosen from the item without them
    raw = groups(s["bullets"])
    top = [(_plain(h), c) for h, c in raw]
    flat = [h for h, c in top if h and not c]
    if lay == "full" and s["image"] and idx > 0:        # full-bleed picture with the title over it
        s["kind"] = "full"
        return
    if lay == "gallery" and s["image"] and s["image_extra"] and idx > 0:    # 2-3 pictures on one slide
        s["kind"] = "gallery"
        return
    if lay == "chart-kpis" and idx > 0:
        data = chart_data(s) if (s["chart"] or s["table"]) else None
        if data:
            s["chart_data"] = data
            s["kind"] = "chart-kpis"
            return
    if lay == "process" and idx > 0 and 3 <= len(top) <= 6:
        s["kind"] = "process"
        return
    if lay == "matrix" and idx > 0 and len(top) == 4:
        s["kind"] = "matrix"
        return
    if lay == "funnel" and idx > 0 and 3 <= len(top) <= 6:
        s["kind"] = "funnel"
        return
    if lay in ("process", "matrix", "funnel") and idx > 0:
        _notes.note(f"slide {idx + 1} '{s['title'][:40]}': layout {lay} needs "
                    + ("exactly 4 items" if lay == "matrix" else "3 to 6 items") + f" (it has {len(top)}), so another layout was used")
    if lay == "kpi-grid" and idx > 0:
        s["kind"] = "kpi-grid"
        return
    if lay == "leaderboard" and idx > 0:
        s["kind"] = "leaderboard"
        return
    if lay == "roadmap" and idx > 0:
        s["kind"] = "roadmap"
        return
    if lay == "dimension-table" and idx > 0:
        s["kind"] = "dimension-table"
        return
    if lay == "textside" and idx > 0 and s["bullets"] and (s["table"] or s["chart"]):
        data = chart_data(s) if s["chart"] else None
        if data:
            s["chart_data"] = data
            s["kind"] = "textside"
            return
        if s["table"] and not s["chart"]:
            s["kind"] = "textside"
            return
    # the model forgot `layout:` (or wrote a plain one) but the items carry the fields / shape of KPI tiles or milestones
    if idx > 0 and lay in ("", "bullets", "stats", "cards") and not (s["chart"] or s["table"] or s["image"]) \
            and raw and all(h and not c for h, c in raw):
        fl = [split_fields(h) for h, _ in raw]
        if _roadmap_like(fl):
            s["kind"] = "roadmap"
            return
        if _kpi_like(fl, lay):
            s["kind"] = "kpi-grid"
            return
    auto = _auto_chart(s, lay, idx, n)
    if auto:
        spec, data = auto
        s["chart"], s["chart_data"] = spec, data
        s["kind"] = "textside" if (s["table"] and s["bullets"]) else "chart"
        return
    if idx > 0 and (s["chart"] or lay == "chart"):
        s["chart"] = s["chart"] or {"kind": "bar", "unit": "", "title": ""}
        data = chart_data(s)
        if data:
            s["chart_data"] = data
            s["kind"] = "chart"
            return
        _notes.note(f"slide {idx + 1} '{s['title'][:40]}': no chart was drawn, because it has no numbers to plot (give a "
                    f"table with a label column and numeric columns, or `Label: number` lines)")
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
    if s["table"] and (lay in ("", "table", "dimension-table") or s.get("alert")):
        if s.get("alert") or lay == "dimension-table":
            s["kind"] = "dimension-table"
            return
        s["kind"] = "table"
        return
    if lay == "quote" and s["bullets"]:
        s["kind"] = "quote"
        return
    if lay in ("timeline", "roadmap") and 2 <= len(top) <= 8:
        if lay == "roadmap" or any("status=" in h or re.match(r"^\d+\s*\|", h) for h, _ in raw):
            s["kind"] = "roadmap"
            return
        s["kind"] = "timeline"
        return
    if lay == "comparison" and len(top) == 2 and all(c for _, c in top) and all(h for h, _ in top):
        s["kind"] = "comparison"
        return
    if lay == "agenda" and 2 <= len(top) <= 8:
        s["kind"] = "agenda"
        return
    if (lay in ("", "kpi-grid") and s.get("callout") and 2 <= len(top) <= 5) or lay == "kpi-grid":
        s["kind"] = "kpi-grid"
        return
    if lay == "leaderboard":
        s["kind"] = "leaderboard"
        return
    if lay in ("", "cards") and 2 <= len(top) <= MAX_CARDS and all(c for _, c in top) and all(h for h, _ in top):
        s["kind"] = "cards"
        return
    if lay in ("", "stats") and 2 <= len(top) <= 5 and flat and len(flat) == len(top) and all(stat_split(h) for h in flat):
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
    t = _theme.resolve(deck.get("theme", ""), deck.get("accent", ""), deck.get("hint", ""))
    if deck.get("font"):                                    # `font: Arial` - one family for the whole deck
        t["head"] = t["body"] = deck["font"]
    return t

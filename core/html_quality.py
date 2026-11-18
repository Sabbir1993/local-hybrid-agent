"""Mechanical repair of the typos a small local model leaves in generated HTML.

When a model writes the same markup many times it sometimes "varies" a number to avoid repeating itself, and the page ends up
with classes that do not exist (bg-slate-80, bg-orange-95), a stray closing tag (</2span>), a broken viewBox or a label hidden
with text-[0px]. Only changes that have one obvious meaning are made; everything else is left alone.
"""
import re
from typing import Tuple

SHADES = (50, 100, 200, 300, 400, 500, 600, 700, 800, 900, 950)
_COLORS = ("slate|gray|zinc|neutral|stone|red|orange|amber|yellow|lime|green|emerald|teal|cyan|sky|blue|indigo|violet|purple|"
           "fuchsia|pink|rose")
_UTIL = "bg|text|border|ring|from|to|via|fill|stroke|divide|outline|decoration|accent|caret|shadow|placeholder"
_SHADE_RX = re.compile(rf"(?<![\w-])((?:[a-z0-9-]+:)*(?:{_UTIL})-(?:{_COLORS}))-(\d{{1,3}})(?=/\d+|\s|\"|'|$)")
_STRAY_CLOSE_RX = re.compile(r"</\d\w*>")
_VIEWBOX_RX = re.compile(r'viewBox="0 0 (\d+)(?: (\d+))?"')
_HIDDEN_TEXT_RX = re.compile(r"(?<![\w-])text-\[0(?:px|rem|em)?\]")
_JUSTIFY = {"space-between": "between", "space-around": "around", "space-evenly": "evenly", "left": "start", "right": "end"}
_JUSTIFY_RX = re.compile(r"(?<![\w-])((?:[a-z0-9-]+:)*)justify-(space-between|space-around|space-evenly|left|right)(?![\w-])")


def _shade(n: int) -> int:
    """The shade a model most likely meant: 80 -> 800, 95 -> 950, 3 -> 300, 750 -> 700 (nearest)."""
    if n in SHADES:
        return n
    for scale in (10, 100):
        if n * scale in SHADES:
            return n * scale
    return min(SHADES, key=lambda s: abs(s - n))


def repair(html: str) -> Tuple[str, int]:
    """-> (html, number of repairs). Safe to run on any page: a page with no such typos comes back unchanged."""
    if not html:
        return html or "", 0
    n = 0

    def shade(m):
        nonlocal n
        v = int(m.group(2))
        fixed = _shade(v)
        if fixed == v:
            return m.group(0)
        n += 1
        return f"{m.group(1)}-{fixed}"

    def justify(m):
        nonlocal n
        n += 1
        return f"{m.group(1)}justify-{_JUSTIFY[m.group(2)]}"

    def viewbox(m):
        nonlocal n
        w, h = m.group(1), m.group(2)
        if h is not None and h != "00":
            return m.group(0)
        n += 1
        return f'viewBox="0 0 {w} {w}"'

    out = _SHADE_RX.sub(shade, html)
    out = _JUSTIFY_RX.sub(justify, out)
    out = _VIEWBOX_RX.sub(viewbox, out)
    out, k = _STRAY_CLOSE_RX.subn("", out)
    n += k
    out, k = _HIDDEN_TEXT_RX.subn("text-xs", out)         # a label shrunk to nothing is a bug, never a design
    n += k
    return out, n


# What every page needs to work on a phone and be read aloud correctly: a language, a charset, a viewport, a title. A small
# local model forgets them often; adding them changes nothing else. Images without alt text are only reported.
_HTML_OPEN_RX = re.compile(r"<html\b([^>]*)>", re.I)
_HEAD_OPEN_RX = re.compile(r"<head\b[^>]*>", re.I)
_SCRIPT_STYLE_RX = re.compile(r"<(script|style)\b.*?</\1>", re.I | re.S)
_TAG_RX = re.compile(r"<[^>]+>")
_IMG_RX = re.compile(r"<img\b[^>]*>", re.I)


def _lang_of(html: str) -> str:
    """"bn" when the visible text is mostly Bengali, else "en"."""
    text = _TAG_RX.sub(" ", _SCRIPT_STYLE_RX.sub(" ", html))
    letters = [c for c in text if c.isalpha()]
    if not letters:
        return "en"
    bengali = sum(1 for c in letters if "\u0980" <= c <= "\u09ff")
    return "bn" if bengali / len(letters) > 0.3 else "en"


def ensure_basics(html: str) -> Tuple[str, list]:
    """-> (html, notes). Adds a missing lang / charset / viewport / title to a full page; a fragment is returned as written.
    `notes` lists what was added and any image without alt text."""
    if not html or not _HTML_OPEN_RX.search(html) or not _HEAD_OPEN_RX.search(html):
        return html or "", []
    out, notes = html, []
    m = _HTML_OPEN_RX.search(out)
    if m and not re.search(r"\blang\s*=", m.group(1), re.I):
        out = f'{out[:m.start()]}<html lang="{_lang_of(out)}"{m.group(1)}>{out[m.end():]}'
        notes.append("lang")
    add = ""
    if not re.search(r"<meta[^>]+charset", out, re.I):
        add += '\n<meta charset="utf-8">'
        notes.append("charset")
    if not re.search(r"""<meta[^>]+name\s*=\s*["']viewport""", out, re.I):
        add += '\n<meta name="viewport" content="width=device-width, initial-scale=1">'
        notes.append("viewport")
    if not re.search(r"<title\b", out, re.I):
        h1 = re.search(r"<h1\b[^>]*>(.*?)</h1>", out, re.I | re.S)
        title = re.sub(r"\s+", " ", _TAG_RX.sub("", h1.group(1))).strip() if h1 else ""
        title = (title or "Page")[:80].replace("&", "&amp;").replace("<", "&lt;")
        add += f"\n<title>{title}</title>"
        notes.append("title")
    if add:
        head = _HEAD_OPEN_RX.search(out)
        out = out[:head.end()] + add + out[head.end():]
    no_alt = sum(1 for tag in _IMG_RX.findall(out) if not re.search(r"\balt\s*=", tag, re.I))
    if no_alt:
        notes.append(f"{no_alt} image(s) without alt text")
    return out, notes

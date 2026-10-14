"""Text measurement for slide layout, with real font metrics.

The renderer used to guess ("0.54 em per character"), which is wrong for bold text, wide fonts and Bengali, and when
the guess failed the text simply ran off its box. Here the width of every word is measured with PIL on the actual
font file (Windows / macOS / Linux font folders, with metric-compatible fallbacks), and a font that is not installed
falls back to the old estimate so the builder never fails because of fonts.
"""
import functools
import math
import os
import re
from pathlib import Path

LINE = 1.22                  # line height / font size
LINE_BENGALI = 1.55          # Bengali glyphs are taller and carry marks above and below
_SCALE = 10                  # fonts are loaded at 10x size, so widths are measured to a tenth of a point
_BENGALI = re.compile("[\u0980-\u09FF]")

_DIRS = [Path(os.environ.get("WINDIR", r"C:\Windows")) / "Fonts", Path("/Library/Fonts"), Path("/System/Library/Fonts"),
         Path("/usr/share/fonts"), Path("/usr/local/share/fonts"), Path.home() / ".fonts",
         Path.home() / "Library" / "Fonts"]
# (regular, bold) file names per family, best first; the later entries are the metric-compatible Linux fonts
_FILES = {
    "segoe ui": (("segoeui.ttf", "DejaVuSans.ttf"), ("segoeuib.ttf", "DejaVuSans-Bold.ttf")),
    "calibri": (("calibri.ttf", "Carlito-Regular.ttf"), ("calibrib.ttf", "Carlito-Bold.ttf")),
    "georgia": (("georgia.ttf", "DejaVuSerif.ttf"), ("georgiab.ttf", "DejaVuSerif-Bold.ttf")),
    "arial": (("arial.ttf", "LiberationSans-Regular.ttf", "Arimo-Regular.ttf"),
              ("arialbd.ttf", "LiberationSans-Bold.ttf", "Arimo-Bold.ttf")),
    "bengali": (("Nirmala.ttc", "NirmalaUI.ttf", "NotoSansBengali-Regular.ttf", "Shonar.ttf"),
                ("NirmalaB.ttc", "Nirmala.ttc", "NotoSansBengali-Bold.ttf", "Shonar.ttf")),
}


@functools.lru_cache(maxsize=None)
def _index() -> dict:
    """{lower-case file name: path} of the installed fonts, built by ONE walk of the font folders (a per-name search of
    /usr/share/fonts, with thousands of files, would be slow on every first use)."""
    idx: dict = {}
    for d in _DIRS:
        if not d.is_dir():
            continue
        try:
            for p in d.rglob("*"):
                if p.suffix.lower() in (".ttf", ".ttc", ".otf"):
                    idx.setdefault(p.name.lower(), str(p))
        except OSError:
            continue
    return idx


def _find(name: str):
    return _index().get(name.lower())


@functools.lru_cache(maxsize=128)
def _font(family: str, bold: bool, size: float):
    """PIL font for (family, bold, size pt) or None when the family is not installed."""
    from PIL import ImageFont
    files = _FILES.get(family.lower())
    if not files:
        return None
    for fname in files[1 if bold else 0]:
        path = _find(fname)
        if path:
            try:
                return ImageFont.truetype(path, max(1, int(round(size * _SCALE))))
            except OSError:
                continue
    return None


def has_bengali(text: str) -> bool:
    return bool(_BENGALI.search(text or ""))


def line_height(texts) -> float:
    return LINE_BENGALI if any(has_bengali(t) for t in texts) else LINE


def _width_in(text: str, family: str, size: float, bold: bool) -> float:
    fam = "bengali" if has_bengali(text) else family
    f = _font(fam, bold, size)
    if f is None:
        return len(text) * size * 0.54 / 72                    # the old estimate: width unknown
    return f.getlength(text) / (_SCALE * 72)


def lines_for(text: str, family: str, size: float, width_in: float, bold: bool = False) -> int:
    """How many lines `text` takes in a box `width_in` wide (greedy word wrap, as PowerPoint does)."""
    text = str(text or "").strip()
    if not text:
        return 1
    space = _width_in(" ", family, size, bold)
    lines, cur = 1, 0.0
    for word in text.split():
        w = _width_in(word, family, size, bold)
        if w > width_in:                                       # a word longer than the line breaks by itself
            extra = math.ceil(w / width_in)
            lines += extra - 1 if cur == 0 else extra
            cur = w - (extra - 1) * width_in
            continue
        if cur == 0:
            cur = w
        elif cur + space + w <= width_in:
            cur += space + w
        else:
            lines += 1
            cur = w
    return lines


def height_in(texts, family: str, size: float, width_in: float, gap_pt: float = 0.0, bold: bool = False) -> float:
    lh = line_height(texts)
    n = sum(lines_for(t, family, size, width_in, bold) for t in texts)
    return n * size * lh / 72 + len(texts) * gap_pt / 72


def fits(texts, family: str, size: float, width_in: float, height: float, gap_pt: float = 0.0,
         bold: bool = False) -> bool:
    return height_in(texts, family, size, width_in, gap_pt, bold) <= height


def fit_size(texts, family: str, width_in: float, height: float, base: float, floor: float,
             gap_pt: float = 0.0, bold: bool = False) -> float:
    """Largest size in [floor, base] (1 pt steps) at which the texts fit; `floor` when none does."""
    size = base
    while size > floor:
        if fits(texts, family, size, width_in, height, gap_pt, bold):
            return size
        size -= 1
    return floor

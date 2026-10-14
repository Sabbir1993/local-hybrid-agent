"""Deck themes: palette + fonts + cover motif. One dict per preset; `resolve` picks one."""
import re
from typing import Optional

DEFAULT_THEME = "midnight"

# cover_*: cover / section / closing slides.  bg/surface/text/muted: content slides.
THEMES: dict[str, dict] = {
    "midnight": dict(label="Midnight (navy + cyan, all dark)", motif="circles",
                     cover_bg="0B1B2E", cover_text="FFFFFF", cover_muted="94A3B8",
                     bg="0B1B2E", surface="13294B", line="1F3A66", text="F1F5F9", muted="94A3B8",
                     accent="22D3EE", accent2="6366F1", head="Segoe UI", body="Segoe UI"),
    "corporate": dict(label="Corporate (white + blue)", motif="band",
                      cover_bg="0F2A5C", cover_text="FFFFFF", cover_muted="BFD3F5",
                      bg="FFFFFF", surface="F1F5F9", line="D9E2EF", text="0F172A", muted="64748B",
                      accent="2563EB", accent2="0EA5E9", head="Calibri", body="Calibri"),
    "forest": dict(label="Forest (deep green + amber)", motif="circles",
                   cover_bg="0F3D2E", cover_text="FFFFFF", cover_muted="B7D3C4",
                   bg="F7F5EE", surface="FFFFFF", line="DDE6DF", text="1B2A22", muted="5F7468",
                   accent="D97706", accent2="2F855A", head="Georgia", body="Calibri"),
    "sunset": dict(label="Sunset (charcoal + coral)", motif="band",
                   cover_bg="1F1B24", cover_text="FFFFFF", cover_muted="C9BFD0",
                   bg="FFF7F2", surface="FFFFFF", line="F3DDD2", text="2A2330", muted="7A6F80",
                   accent="F4511E", accent2="FF8A65", head="Segoe UI", body="Segoe UI"),
    "minimal": dict(label="Minimal (white, black, red accent)", motif="rule",
                    cover_bg="FFFFFF", cover_text="111111", cover_muted="6B7280",
                    bg="FFFFFF", surface="F5F5F5", line="E5E5E5", text="111111", muted="6B7280",
                    accent="E11D48", accent2="111111", head="Calibri", body="Calibri"),
    # Editorial: cream pages alternating with deep-teal ones, serif titles, a drawn arch motif on the cover. `alt` is the
    # palette every second content slide uses (design._variant), `art` the cover's default drawing.
    "editorial": dict(label="Editorial (cream + deep teal, gold)", motif="rule", art="arches",
                      cover_bg="10302D", cover_text="F4EFE4", cover_muted="A9BDB6",
                      bg="F4EFE4", surface="FBF8F1", line="E2D9C6", text="10302D", muted="5C6B64",
                      accent="C9962B", accent2="B5412F", head="Georgia", body="Segoe UI",
                      alt=dict(bg="10302D", surface="17433E", line="245A53", text="F4EFE4", muted="A9BDB6")),
    "ember": dict(label="Ember (cream + maroon, gold)", motif="rule", art="waves",
                  cover_bg="3A1814", cover_text="F6EBDD", cover_muted="D2B8A8",
                  bg="F6EFE7", surface="FFFFFF", line="EADBCB", text="3A1A16", muted="7A5B52",
                  accent="D4A24C", accent2="B03A2E", head="Georgia", body="Segoe UI",
                  alt=dict(bg="4A1F1A", surface="5C2B25", line="7A3E36", text="F6EBDD", muted="D2B8A8")),
    "royal": dict(label="Royal (indigo + gold)", motif="circles",
                  cover_bg="1E1B4B", cover_text="FFFFFF", cover_muted="C7C5EE",
                  bg="FAFAF7", surface="FFFFFF", line="E4E2F0", text="1E1B4B", muted="6B7280",
                  accent="CA8A04", accent2="4F46E5", head="Georgia", body="Calibri"),
}

# words in a free-text "GLOBAL THEME: deep navy, gold highlights" that pick a preset
_HINTS = [
    (("gold", "indigo", "royal", "purple"), "royal"),
    (("green", "forest", "emerald", "nature"), "forest"),
    (("orange", "coral", "sunset", "warm", "red"), "sunset"),
    (("minimal", "monochrome", "black and white", "clean white"), "minimal"),
    (("corporate", "white", "light", "blue"), "corporate"),
    (("navy", "dark", "midnight", "cyan", "tech"), "midnight"),
    (("cream", "teal", "editorial", "magazine", "serif"), "editorial"),
    (("maroon", "burgundy", "wine", "ember", "crimson"), "ember"),
]
_HEX = re.compile(r"^#?([0-9A-Fa-f]{6})$")


def theme_names() -> list[str]:
    return list(THEMES)


def guess(text: str) -> Optional[str]:
    low = (text or "").lower()
    for words, name in _HINTS:
        if any(w in low for w in words):
            return name
    return None


def resolve(name: str = "", accent: str = "", hint: str = "") -> dict:
    key = re.sub(r"[^a-z]", "", (name or "").lower())
    key = key if key in THEMES else (guess(name) or guess(hint) or DEFAULT_THEME)
    t = dict(THEMES[key])
    t["name"] = key
    m = _HEX.match((accent or "").strip())
    if m:
        t["accent"] = m.group(1).upper()
    return t

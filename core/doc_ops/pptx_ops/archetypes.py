"""Executive PowerPoint Slide Archetypes for Enterprise Banking.

Provides rigid, boardroom-ready layout archetypes:
1. Cover / Title Slide
2. KPI Cards (3-4 large metrics with delta pills & color tones)
3. 2-Column Comparison (e.g. Current State vs Proposed Solution)
4. Horizontal Process / Roadmap (Numbered chevron steps)
5. Feature Grid (6-8 cards with title and description)
6. Data Table Slide

Compiles directly from structured JSON specs without relying on markdown inference.
"""

import io
from typing import Any, Optional
from pptx import Presentation
from pptx.dml.color import RGBColor
from pptx.enum.shapes import MSO_SHAPE
from pptx.enum.text import MSO_ANCHOR, PP_ALIGN
from pptx.util import Inches, Pt

# Standard 16:9 Presentation Dimensions
WIDTH_IN = 13.333
HEIGHT_IN = 7.5

# Bank Navy Theme Colors
NAVY_PRIMARY = "0F2C59"       # Deep Bank Navy
NAVY_SECONDARY = "1E40AF"     # Royal Blue Accent
TEXT_DARK = "0F172A"          # Slate 900
TEXT_MUTED = "64748B"         # Slate 500
BG_CARD = "F8FAFC"            # Slate 50
BORDER_CARD = "E2E8F0"        # Slate 200

TONES = {
    "green": ("15803D", "DCFCE7"),   # Text, Pill Fill
    "red": ("B91C1C", "FEE2E2"),
    "amber": ("B45309", "FEF3C7"),
    "blue": ("1D4ED8", "DBEAFE"),
    "navy": ("0F2C59", "E2E8F0"),
}


def _rgb(hex_code: str) -> RGBColor:
    return RGBColor.from_string(hex_code.lstrip("#").upper())


def _add_header(slide, title: str, subtitle: str = "", category: str = "SSL AI | ENTERPRISE AI FOR BANKS"):
    """Adds a standard executive header to a slide."""
    shapes = slide.shapes

    # Category / Eyebrow
    if category:
        cat_box = shapes.add_textbox(Inches(0.8), Inches(0.5), Inches(11.7), Inches(0.4))
        tf = cat_box.text_frame
        tf.word_wrap = True
        p = tf.paragraphs[0]
        p.text = category.upper()
        p.font.name = "Calibri"
        p.font.size = Pt(9.5)
        p.font.bold = True
        p.font.color.rgb = _rgb(TEXT_MUTED)

    # Main Title
    title_box = shapes.add_textbox(Inches(0.8), Inches(0.85), Inches(11.7), Inches(0.8))
    title_box.name = "Title 1"
    tf = title_box.text_frame
    tf.word_wrap = True
    p = tf.paragraphs[0]
    p.text = title
    p.font.name = "Calibri"
    p.font.size = Pt(26)
    p.font.bold = True
    p.font.color.rgb = _rgb(NAVY_PRIMARY)

    # Subtitle
    if subtitle:
        sub_box = shapes.add_textbox(Inches(0.8), Inches(1.6), Inches(11.7), Inches(0.4))
        tf_sub = sub_box.text_frame
        tf_sub.word_wrap = True
        p_sub = tf_sub.paragraphs[0]
        p_sub.text = subtitle
        p_sub.font.name = "Calibri"
        p_sub.font.size = Pt(13)
        p_sub.font.color.rgb = _rgb(TEXT_MUTED)


def _render_cover_slide(prs, spec: dict):
    slide_layout = prs.slide_layouts[6]   # blank layout
    slide = prs.slides.add_slide(slide_layout)
    shapes = slide.shapes

    # Background subtle accent line on left
    accent_bar = shapes.add_shape(MSO_SHAPE.RECTANGLE, Inches(0.8), Inches(2.2), Inches(0.12), Inches(3.2))
    accent_bar.fill.solid()
    accent_bar.fill.fore_color.rgb = _rgb(NAVY_SECONDARY)
    accent_bar.line.fill.background()

    # Title & Subtitle block
    tb = shapes.add_textbox(Inches(1.2), Inches(2.0), Inches(10.5), Inches(2.2))
    tf = tb.text_frame
    tf.word_wrap = True

    # Category badge
    p0 = tf.paragraphs[0]
    p0.text = spec.get("category", "BANGLADESH BANKING SECTOR").upper()
    p0.font.name = "Calibri"
    p0.font.size = Pt(11)
    p0.font.bold = True
    p0.font.color.rgb = _rgb(NAVY_SECONDARY)
    p0.space_after = Pt(12)

    # Title
    p1 = tf.add_paragraph()
    p1.text = spec.get("title", "Executive Presentation")
    p1.font.name = "Calibri"
    p1.font.size = Pt(36)
    p1.font.bold = True
    p1.font.color.rgb = _rgb(NAVY_PRIMARY)
    p1.space_after = Pt(10)

    # Subtitle
    if spec.get("subtitle"):
        p2 = tf.add_paragraph()
        p2.text = spec["subtitle"]
        p2.font.name = "Calibri"
        p2.font.size = Pt(16)
        p2.font.color.rgb = _rgb(TEXT_MUTED)

    # Footer note / classification
    foot_box = shapes.add_textbox(Inches(1.2), Inches(5.8), Inches(10.5), Inches(0.5))
    tf_f = foot_box.text_frame
    pf = tf_f.paragraphs[0]
    pf.text = spec.get("footer", "CONFIDENTIAL - PREPARED FOR BANK MANAGEMENT")
    pf.font.name = "Calibri"
    pf.font.size = Pt(10)
    pf.font.color.rgb = _rgb(TEXT_MUTED)


def _render_kpi_cards_slide(prs, spec: dict):
    slide = prs.slides.add_slide(prs.slide_layouts[6])
    _add_header(slide, spec.get("title", "Key Performance Indicators"), spec.get("subtitle", ""))
    cards = spec.get("cards", [])[:4]
    if not cards:
        return

    n = len(cards)
    margin_x = 0.8
    total_w = WIDTH_IN - 2 * margin_x
    spacing = 0.3
    card_w = (total_w - (n - 1) * spacing) / n
    card_h = 4.2
    top_y = 2.2

    for i, card in enumerate(cards):
        left_x = margin_x + i * (card_w + spacing)
        # Card Background Shape
        shape = slide.shapes.add_shape(MSO_SHAPE.ROUNDED_RECTANGLE, Inches(left_x), Inches(top_y), Inches(card_w), Inches(card_h))
        shape.fill.solid()
        shape.fill.fore_color.rgb = _rgb(BG_CARD)
        shape.line.color.rgb = _rgb(BORDER_CARD)
        shape.line.width = Pt(1)

        # Content text box
        tb = slide.shapes.add_textbox(Inches(left_x + 0.2), Inches(top_y + 0.3), Inches(card_w - 0.4), Inches(card_h - 0.6))
        tf = tb.text_frame
        tf.word_wrap = True

        # Big Metric Number
        p0 = tf.paragraphs[0]
        p0.text = str(card.get("metric", ""))
        p0.font.name = "Calibri"
        p0.font.size = Pt(32)
        p0.font.bold = True
        p0.font.color.rgb = _rgb(NAVY_PRIMARY)
        p0.space_after = Pt(10)

        # Delta / Status Pill
        delta = card.get("delta")
        tone = card.get("tone", "blue")
        if delta:
            p_d = tf.add_paragraph()
            p_d.text = f"  {delta}  "
            p_d.font.name = "Calibri"
            p_d.font.size = Pt(11)
            p_d.font.bold = True
            text_col, _ = TONES.get(tone, TONES["blue"])
            p_d.font.color.rgb = _rgb(text_col)
            p_d.space_after = Pt(12)

        # Metric Label / Description
        p1 = tf.add_paragraph()
        p1.text = card.get("label", "")
        p1.font.name = "Calibri"
        p1.font.size = Pt(14)
        p1.font.bold = True
        p1.font.color.rgb = _rgb(TEXT_DARK)
        p1.space_after = Pt(6)

        if card.get("detail"):
            p2 = tf.add_paragraph()
            p2.text = card["detail"]
            p2.font.name = "Calibri"
            p2.font.size = Pt(11)
            p2.font.color.rgb = _rgb(TEXT_MUTED)


def _render_comparison_slide(prs, spec: dict):
    slide = prs.slides.add_slide(prs.slide_layouts[6])
    _add_header(slide, spec.get("title", "Comparative Analysis"), spec.get("subtitle", ""))

    left_col = spec.get("left", {})
    right_col = spec.get("right", {})

    col_w = 5.6
    col_h = 4.4
    top_y = 2.2

    for col_data, left_pos, is_highlight in ((left_col, 0.8, False), (right_col, 6.9, True)):
        box = slide.shapes.add_shape(MSO_SHAPE.ROUNDED_RECTANGLE, Inches(left_pos), Inches(top_y), Inches(col_w), Inches(col_h))
        box.fill.solid()
        box.fill.fore_color.rgb = _rgb(BG_CARD if not is_highlight else "F0FDF4")
        box.line.color.rgb = _rgb(BORDER_CARD if not is_highlight else "86EFAC")
        box.line.width = Pt(1.5 if is_highlight else 1)

        tb = slide.shapes.add_textbox(Inches(left_pos + 0.3), Inches(top_y + 0.3), Inches(col_w - 0.6), Inches(col_h - 0.6))
        tf = tb.text_frame
        tf.word_wrap = True

        p0 = tf.paragraphs[0]
        p0.text = col_data.get("title", "")
        p0.font.name = "Calibri"
        p0.font.size = Pt(18)
        p0.font.bold = True
        p0.font.color.rgb = _rgb(NAVY_PRIMARY if not is_highlight else "15803D")
        p0.space_after = Pt(14)

        for item in col_data.get("points", []):
            pi = tf.add_paragraph()
            pi.text = f"• {item}"
            pi.font.name = "Calibri"
            pi.font.size = Pt(12)
            pi.font.color.rgb = _rgb(TEXT_DARK)
            pi.space_after = Pt(8)


def _render_process_slide(prs, spec: dict):
    slide = prs.slides.add_slide(prs.slide_layouts[6])
    _add_header(slide, spec.get("title", "Implementation Process"), spec.get("subtitle", ""))

    steps = spec.get("steps", [])[:5]
    if not steps:
        return

    n = len(steps)
    margin_x = 0.8
    total_w = WIDTH_IN - 2 * margin_x
    spacing = 0.25
    step_w = (total_w - (n - 1) * spacing) / n
    step_h = 4.2
    top_y = 2.2

    for i, step in enumerate(steps):
        left_x = margin_x + i * (step_w + spacing)

        box = slide.shapes.add_shape(MSO_SHAPE.ROUNDED_RECTANGLE, Inches(left_x), Inches(top_y), Inches(step_w), Inches(step_h))
        box.fill.solid()
        box.fill.fore_color.rgb = _rgb(BG_CARD)
        box.line.color.rgb = _rgb(BORDER_CARD)
        box.line.width = Pt(1)

        tb = slide.shapes.add_textbox(Inches(left_x + 0.2), Inches(top_y + 0.3), Inches(step_w - 0.4), Inches(step_h - 0.6))
        tf = tb.text_frame
        tf.word_wrap = True

        p0 = tf.paragraphs[0]
        p0.text = f"0{i + 1}"
        p0.font.name = "Calibri"
        p0.font.size = Pt(28)
        p0.font.bold = True
        p0.font.color.rgb = _rgb(NAVY_SECONDARY)
        p0.space_after = Pt(10)

        p1 = tf.add_paragraph()
        p1.text = step.get("title", "")
        p1.font.name = "Calibri"
        p1.font.size = Pt(15)
        p1.font.bold = True
        p1.font.color.rgb = _rgb(NAVY_PRIMARY)
        p1.space_after = Pt(8)

        if step.get("description"):
            p2 = tf.add_paragraph()
            p2.text = step["description"]
            p2.font.name = "Calibri"
            p2.font.size = Pt(11)
            p2.font.color.rgb = _rgb(TEXT_MUTED)


def render_archetype_deck(deck_spec: dict) -> bytes:
    """Builds an executive .pptx presentation from structured JSON archetype specification."""
    prs = Presentation()
    prs.slide_width = Inches(WIDTH_IN)
    prs.slide_height = Inches(HEIGHT_IN)

    slides_data = deck_spec.get("slides", [])
    for slide_spec in slides_data:
        stype = slide_spec.get("type", "").lower()
        if stype in ("cover", "title"):
            _render_cover_slide(prs, slide_spec)
        elif stype in ("kpi", "kpi_cards", "metrics"):
            _render_kpi_cards_slide(prs, slide_spec)
        elif stype in ("comparison", "vs"):
            _render_comparison_slide(prs, slide_spec)
        elif stype in ("process", "roadmap", "timeline"):
            _render_process_slide(prs, slide_spec)

    buf = io.BytesIO()
    prs.save(buf)
    return buf.getvalue()

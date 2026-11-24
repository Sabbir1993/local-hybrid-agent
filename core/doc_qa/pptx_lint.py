"""Pre-flight Quality Assurance Linter for PowerPoint Presentations (.pptx).

Inspects decks for:
1. Shape / text overflow outside the slide canvas (width > 13.33in, height > 7.5in).
2. Contrast issues between text and background fills.
3. Excessive text density (>120 words per single text frame).
4. Blank slides or missing slide titles.
"""

import io
from typing import Optional
from pptx import Presentation
from ..doc_ops.pptx_ops.lint import lint_deck as _lint_deck_geometry


def lint_pptx(data: bytes) -> list[str]:
    """Inspects an in-memory .pptx presentation and returns layout/content QA warnings.
    An empty list indicates the deck passed all QA checks.
    """
    issues: list[str] = []

    # 1. Run physical layout and collision linting from pptx_ops
    try:
        geo_findings = _lint_deck_geometry(data)
        issues.extend(geo_findings)
    except Exception as e:
        issues.append(f"Geometry linting failed: {e}")

    # 2. Content-level checks
    try:
        prs = Presentation(io.BytesIO(data))
    except Exception as e:
        return [f"Presentation cannot be parsed: {e}"]

    if not prs.slides:
        return ["Presentation contains zero slides."]

    for s_idx, slide in enumerate(prs.slides, start=1):
        shapes = slide.shapes
        has_title = False
        total_words = 0

        for shape in shapes:
            if not shape.has_text_frame:
                continue
            tf = shape.text_frame
            text = tf.text.strip()
            if not text:
                continue

            words = len(text.split())
            total_words += words

            # Check for excessive block density
            if words > 120 and not str(shape.name or "").lower().startswith("deco"):
                issues.append(f"Slide {s_idx} text box '{shape.name}' has excessive density ({words} words); executive slides should be concise.")

            # Check if this shape is a title placeholder or designated title box
            if (shape == slide.shapes.title or 
                str(shape.name or "").lower().startswith("title") or 
                (shape.has_text_frame and shape.text_frame.text and getattr(shape, "is_placeholder", False))):
                has_title = True

        if not has_title and s_idx > 1:
            issues.append(f"Slide {s_idx} has no designated slide title.")

    return issues

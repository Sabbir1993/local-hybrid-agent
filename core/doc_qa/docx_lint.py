"""Pre-flight Quality Assurance Linter for Word Documents (.docx).

Inspects documents for:
1. Unfilled prompt placeholders (e.g., [Insert Date], [Overall results against targets]).
2. Leaked raw markdown syntax (e.g., ### Heading, **bold**, ```code```).
3. Heading hierarchy inconsistencies (e.g., Heading 3 directly under Heading 1).
4. Empty tables or unstyled tables.
5. Overall document completeness.
"""

import io
import re
from typing import Optional
import docx

# Detects bracketed placeholders that models forget to fill
PLACEHOLDER_RX = re.compile(r"\[(?:Insert|Notable|Overall|Prioritized|Document|Branch|Company|Author|Date|TBD|TODO|Select|Placeholder)[^\]]*\]", re.I)

# Detects raw markdown tags that should have been parsed into Word elements
RAW_MD_RX = re.compile(r"(?:^#{1,6}\s+|^\s*[-*]\s+|\*\*[^*\n]+\*\*|```)")


def lint_docx(data: bytes) -> list[str]:
    """Inspects an in-memory .docx file and returns a list of warnings/errors.
    An empty list indicates the document passed all QA checks.
    """
    issues: list[str] = []
    try:
        doc = docx.Document(io.BytesIO(data))
    except Exception as e:
        return [f"Word document cannot be parsed: {e}"]

    paragraphs = doc.paragraphs
    tables = doc.tables

    if not paragraphs and not tables:
        return ["Document is completely empty."]

    prev_heading_level = 0

    for i, p in enumerate(paragraphs, start=1):
        text = p.text.strip()
        if not text:
            continue

        # 1. Check for unfilled placeholders
        match = PLACEHOLDER_RX.search(text)
        if match:
            issues.append(f"Paragraph {i} contains unfilled template placeholder: '{match.group(0)}'")

        # 2. Check for leaked raw markdown
        if RAW_MD_RX.search(text):
            issues.append(f"Paragraph {i} contains unrendered markdown syntax: '{text[:50]}...'")

        # 3. Check heading hierarchy
        style_name = p.style.name if p.style else ""
        if style_name.startswith("Heading "):
            try:
                level = int(style_name.split()[1])
                if prev_heading_level > 0 and level > prev_heading_level + 1:
                    issues.append(f"Heading level skipped at paragraph {i}: jumped from Heading {prev_heading_level} to Heading {level} ('{text[:40]}')")
                prev_heading_level = level
            except (ValueError, IndexError):
                pass

    # 4. Check tables
    for t_idx, table in enumerate(tables, start=1):
        rows = table.rows
        if not rows:
            issues.append(f"Table {t_idx} has zero rows.")
            continue
        col_count = len(rows[0].cells)
        if col_count == 0:
            issues.append(f"Table {t_idx} has zero columns.")
            continue

        # Check if table has any text
        has_content = any(c.text.strip() for r in rows for c in r.cells)
        if not has_content:
            issues.append(f"Table {t_idx} is completely blank.")

    return issues

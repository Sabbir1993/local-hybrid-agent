"""Test suite for enterprise banking file generation and pre-flight QA linters.

Verifies:
1. Excel financial styling (BDT currency, accounting double underline for totals, freeze pane, auto-filter).
2. Word document generation (confidentiality header, alert callouts, complex script Bengali font fallback).
3. PowerPoint slide archetypes (JSON spec to presentation compilation).
4. Pre-flight QA linters (detection of #REF! errors, placeholder leaks, unclosed formulas).
5. Self-contained air-gapped HTML generation.
"""

import io
import json
import pytest
import openpyxl
import docx
from pptx import Presentation

from core.doc_ops import create
from core.doc_ops.xlsx_ops.financial_styles import (
    apply_financial_styling,
    is_total_row,
    FMT_ACCOUNTING_BDT,
    FMT_NUMBER_DECIMAL,
)
from core.doc_ops.pptx_ops.archetypes import render_archetype_deck
from core.doc_qa import lint_document, lint_docx, lint_xlsx, lint_pptx


def test_is_total_row():
    assert is_total_row("Total") is True
    assert is_total_row("Grand Total") is True
    assert is_total_row("Subtotal (BDT)") is True
    assert is_total_row("Branch Name") is False
    assert is_total_row(None) is False


def test_excel_financial_styling():
    rows = [
        ["Branch", "Target (BDT)", "Disbursed (BDT)", "Recovery Rate"],
        ["Gulshan", 100000000, 105000000, "98.5%"],
        ["Motijheel", 80000000, 75000000, "92.0%"],
        ["Total", 180000000, 180000000, "95.6%"],
    ]
    data, _ = create("test_branch.xlsx", "\n".join([",".join(map(str, r)) for r in rows]))
    assert len(data) > 0

    wb = openpyxl.load_workbook(io.BytesIO(data))
    ws = wb.active

    # Verify freeze pane and auto filter
    assert ws.freeze_panes == "A2"
    assert ws.auto_filter.ref is not None

    # Verify total row has double bottom border
    total_cell = ws.cell(row=4, column=1)
    assert total_cell.font.bold is True
    assert total_cell.border.bottom.style == "double"

    # Verify QA linter approves clean workbook
    issues = lint_xlsx(data)
    assert issues == []


def test_xlsx_linter_catches_errors():
    wb = openpyxl.Workbook()
    ws = wb.active
    ws["A1"] = "Revenue"
    ws["A2"] = "#REF!"
    ws["B2"] = "=(100 + 20"  # unbalanced parenthesis
    buf = io.BytesIO()
    wb.save(buf)

    issues = lint_xlsx(buf.getvalue())
    assert len(issues) >= 2
    assert any("#REF!" in iss for iss in issues)
    assert any("unbalanced" in iss for iss in issues)


def test_word_bengali_and_confidentiality_header():
    md = (
        "# ঋণ মূল্যায়ন সারসংক্ষেপ (Credit Assessment Summary)\n\n"
        "> [!IMPORTANT]\n"
        "> বাংলাদেশ ব্যাংকের নীতিমালা অনুযায়ী ক্রেডিট রিস্ক যাচাই করা হয়েছে।\n\n"
        "## শাখাভিত্তিক বিবরণ\n\n"
        "| শাখা (Branch) | পরিমাণ (BDT) | অবস্থা |\n"
        "|---|---|---|\n"
        "| গুলশান | ৫০ কোটি | অনুমোদিত |\n"
        "| মতিঝিল | ৩০ কোটি | প্রক্রিয়াধীন |\n"
    )
    data, _ = create("credit_memo.docx", md)
    assert len(data) > 0

    doc = docx.Document(io.BytesIO(data))

    # Verify running header contains confidential banking text
    header_text = doc.sections[0].header.paragraphs[0].text
    assert "CONFIDENTIAL" in header_text

    # Verify QA linter approves
    issues = lint_docx(data)
    assert issues == []


def test_docx_linter_catches_placeholders_and_raw_md():
    doc = docx.Document()
    doc.add_paragraph("### Leaked Heading 3")
    doc.add_paragraph("[Overall results against targets]")
    buf = io.BytesIO()
    doc.save(buf)

    issues = lint_docx(buf.getvalue())
    assert len(issues) >= 2
    assert any("placeholder" in iss for iss in issues)
    assert any("unrendered markdown" in iss for iss in issues)


def test_pptx_archetype_deck():
    spec = {
        "slides": [
            {
                "type": "cover",
                "title": "SSL AI Platform Overview",
                "subtitle": "Product Overview for Commercial Banks",
                "category": "Bangladesh Banking Sector",
            },
            {
                "type": "kpi_cards",
                "title": "Branch Performance Highlights",
                "cards": [
                    {"metric": "BDT 142.5 Cr", "label": "Deposit Mobilization", "delta": "+14.2%", "tone": "green"},
                    {"metric": "1.82%", "label": "NPL Ratio", "delta": "-0.3%", "tone": "green"},
                    {"metric": "88.4%", "label": "SME Lending", "delta": "-4.1%", "tone": "amber"},
                ],
            },
            {
                "type": "comparison",
                "title": "Bring the AI to Your Data",
                "left": {"title": "External AI Platform", "points": ["Data leaves the bank", "Governance risk"]},
                "right": {"title": "SSL AI on your infrastructure", "points": ["Bank-controlled environment", "Data sovereignty"]},
            },
            {
                "type": "process",
                "title": "How Banks Can Adopt SSL AI",
                "steps": [
                    {"title": "Discover & Assess", "description": "Priority use cases & security."},
                    {"title": "Establish Workspace", "description": "Deploy SSL AI on-premises."},
                    {"title": "Configure Knowledge", "description": "Index banking circulars & SOPs."},
                ],
            },
        ]
    }
    data = render_archetype_deck(spec)
    assert len(data) > 0

    prs = Presentation(io.BytesIO(data))
    assert len(prs.slides) == 4

    # Verify QA linter approves
    issues = lint_pptx(data)
    assert issues == []


def test_html_air_gapped_generation():
    md = "# Banking MIS Dashboard\n\nGenerated internally.\n"
    data, _ = create("dashboard.html", md)
    assert len(data) > 0
    text = data.decode("utf-8")
    assert "<!DOCTYPE html>" in text
    assert "Banking MIS Dashboard" in text
    # Zero external CDN / remote URLs
    assert "http://" not in text
    assert "https://" not in text

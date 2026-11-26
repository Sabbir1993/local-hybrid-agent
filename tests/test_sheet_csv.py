"""Workbook builder: tables written as CSV lines, and what the model is told when a call is too big for one write.

A small model often writes a sheet as `Metric,Value,Source` lines under `## Sheet:`; the builder used to answer "no sheet with a
table found" with no hint. And the generic "call is over the cap" refusal talked about inline CSS, which sent models that were asked
for a workbook to HTML.
"""

import io

import openpyxl

from core.sheet_engine import build as B


def _rows(outline, idx=0):
    ws = openpyxl.load_workbook(io.BytesIO(B.build(outline, "t", True))).worksheets[idx]
    return [list(r) for r in ws.iter_rows(values_only=True)]


def test_csv_lines_under_a_sheet_become_a_table():
    rows = _rows('## Sheet: KPIs\nMetric,Value,As of,Source,Notes\n"Bank","BRAC Bank PLC, Dhaka",2026,TBS,"strong, rising"\nNPL,2.27%,FY2025,IR,Improving\n')
    assert rows[0] == ["Metric", "Value", "As of", "Source", "Notes"]
    assert rows[1] == ["Bank", "BRAC Bank PLC, Dhaka", 2026, "TBS", "strong, rising"]
    assert rows[2][1] == 0.0227


def test_extra_unquoted_commas_are_joined_into_the_last_column():
    rows = _rows("## Sheet: A\nMetric,Value,Note\nx,1,fine, really, good\n")
    assert rows[1] == ["x", 1, "fine, really, good"]


def test_a_second_csv_table_of_another_width_becomes_a_block():
    rows = _rows("## Sheet: A\nMetric,Value\nx,1\nEntity,Role,Stake\nAlpha,Parent,51%\n")
    flat = [r[:3] for r in rows]
    assert ["Entity", "Role", "Stake"] in flat and ["Alpha", "Parent", 0.51] in flat


def test_directives_prose_and_pipe_tables_are_not_taken_for_csv():
    rows = _rows("## Sheet: A\n| K | V |\n|---|---|\n| a | 1 |\nformat: V=currency:USD, K=text\nchart: bar | title=T | x=K | y=V\n")
    assert rows[0][:2] == ["K", "V"] and len(rows) <= 4
    assert not any("format" in str(c) for r in rows for c in r)


def test_prose_with_commas_is_not_a_table():
    try:
        B.build("## Sheet: A\nRevenue grew, margins fell, costs rose.\nThen things improved.\n", "t")
        raise AssertionError("prose is not a table")
    except B.BuildError as e:
        assert "markdown table" in str(e) and "Revenue grew" in str(e)


def test_the_no_table_error_names_the_sheet_and_shows_the_format():
    try:
        B.build("## Sheet: Summary\nJust words here\n", "t")
        raise AssertionError
    except B.BuildError as e:
        msg = str(e)
        assert "| Metric | Value |" in msg and "Summary" in msg and "Just words here" in msg


def test_a_sheet_without_a_table_is_reported_not_silently_dropped():
    B.build("## Sheet: A\n| x | y |\n|---|---|\n| 1 | 2 |\n## Sheet: Lost\nno table here\n", "t", True)
    assert any("Lost" in w and "left out" in w for w in B._warnings.get())


def test_oversize_refusal_for_a_workbook_says_build_in_steps_and_not_html():
    from core.agent_tools import common_writer as cw
    msg = cw.tool_write_file_common({"path": "BRAC.xlsx", "_oversize_tokens": 9000})
    assert "add_outline_sheet" in msg and "do NOT switch to HTML" in msg
    assert "inline CSS" not in msg


def test_oversize_refusal_for_a_web_page_is_unchanged():
    from core.agent_tools import common_writer as cw
    assert "inline CSS" in cw.tool_write_file_common({"path": "page.html", "_oversize_tokens": 9000})


def test_oversize_refusal_for_other_office_files_uses_doc_append():
    from core.agent_tools.limits import document_split_advice
    for ext in (".docx", ".pptx", ".pdf"):
        assert "doc_append" in document_split_advice("x" + ext)
    assert document_split_advice("main.py") == ""

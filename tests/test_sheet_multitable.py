"""Workbook builder: several markdown tables under one `## Sheet:`, `**bold**` markers and percent cells.

A model writes a sheet as a few small tables (shareholders, branch counts, subsidiaries ...). The builder used to keep only the first
table's columns: every later table's header became a data row and its extra columns were cut off.
"""

import io

import openpyxl

from core.sheet_engine import build as B

PROFILE = """# T
## Sheet: Profile
| Shareholder | Stake | Notes |
|---|---|---|
| Parent | 46.2% | largest holder |
| Foreign | 36.48% | DSE data |
| Public | ~5.8% | residual |
| **Total** | **100%** | paid-up capital |

### Branch network
| Metric | Value | Source | Year | Comment |
|---|---|---|---|---|
| Branches | 191 | Annual report | 2025 | incl. sub-branches |
| ATMs | 457 | Press | 2025 | est. |

Subsidiaries
| Entity | Relationship |
|---|---|
| Alpha Ltd | Subsidiary |
"""


def _ws(outline=PROFILE, name="Profile"):
    return openpyxl.load_workbook(io.BytesIO(B.build(outline, "t", True)))[name]


def _col(ws, c):
    return [ws.cell(row=r, column=c).value for r in range(1, ws.max_row + 1)]


def test_each_table_keeps_its_own_header_and_columns():
    ws = _ws()
    flat = [[c.value for c in row] for row in ws.iter_rows()]
    assert flat[0][:3] == ["Shareholder", "Stake", "Notes"]
    assert ["Metric", "Value", "Source", "Year", "Comment"] in [r[:5] for r in flat]       # wider table: no columns cut
    assert ["Entity", "Relationship"] in [r[:2] for r in flat]
    metric_row = next(i for i, r in enumerate(flat) if r[:5] == ["Metric", "Value", "Source", "Year", "Comment"])
    assert flat[metric_row + 1][:5] == ["Branches", 191, "Annual report", 2025, "incl. sub-branches"]
    assert ws.max_column >= 5


def test_a_header_row_is_not_left_in_the_first_tables_data():
    ws = _ws()
    first_table = [ws.cell(row=r, column=1).value for r in range(2, 6)]
    assert first_table == ["Parent", "Foreign", "Public", "Total"]


def test_the_line_above_a_table_becomes_its_title():
    ws = _ws()
    texts = {str(c.value) for row in ws.iter_rows() for c in row if c.value is not None}
    assert "Branch network" in texts and "Subsidiaries" in texts


def test_bold_markers_are_removed_and_the_total_row_is_styled():
    ws = _ws()
    texts = {str(c.value) for row in ws.iter_rows() for c in row if c.value is not None}
    assert not any("**" in t for t in texts)
    assert ws["A5"].value == "Total" and ws["A5"].font.bold


def test_percent_cells_show_as_percent_even_in_a_mixed_column():
    ws = _ws()
    assert ws["B2"].value == 0.462 and ws["B2"].number_format == "0.0%"
    assert ws["B3"].value == 0.3648
    assert ws["B4"].value == "~5.8%"                    # text stays text


def test_percent_values_have_no_float_noise():
    assert B._to_number("57.2%") == 0.572
    assert B._to_number("33.3%") == 0.333


def test_a_single_table_sheet_is_unchanged_and_an_empty_row_is_not_a_new_table():
    # an all-empty row was always skipped (as before); it must not be taken for a separator that starts another table
    ws = _ws("# T\n## Sheet: S\n| A | B |\n|---|---|\n| x | 1 |\n| | |\n| y | 2 |\n", "S")
    assert [ws.cell(row=r, column=1).value for r in range(1, 5)] == ["A", "x", "y", None]
    assert ws["B3"].value == 2


def test_explicit_blocks_still_work_next_to_detected_ones():
    out = "# T\n## Sheet: S\n| A |\n|---|\n| 1 |\n### Block: Extra | at=E1\n| K | V |\n|---|---|\n| a | 1 |\n"
    ws = _ws(out, "S")
    assert ws["E1"].value == "Extra" and ws["E2"].value == "K"

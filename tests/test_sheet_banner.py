"""Workbook builder: banner/subtitle rows, section bands, input cells, `widths:` by name, header-row offsets."""

import io

import openpyxl

from core.sheet_engine import build as B


def _wb(outline: str):
    return openpyxl.load_workbook(io.BytesIO(B.build(outline, "t", True)))


def _ranges(ws):
    return {str(k.sqref) for k in ws.conditional_formatting._cf_rules}


FULL = """# T
theme: corporate
## Sheet: Fin
banner: 02 - Financial performance
subtitle: Unit: USD millions, consolidated
| Metric | Unit | FY2024 | FY2025 | Status |
|---|---|---|---|---|
| >> INCOME STATEMENT | | | | |
| Revenue | m | 100 | 120 | Verified |
| Cost | m | 80 | 90 | Reported |
| Profit | m | =C6-C7 | =D6-D7 | Estimated |
total: FY2024=sum
highlight: Status = Verified green
validate: Status in [Verified, Reported, Estimated]
input: FY2024, FY2025
widths: Metric=40, Status=18
chart: bar | title=Revenue | x=Metric | y=FY2025
## Dashboard: Dash
title: D
kpi: Profit = ='Fin'!D8
chart: bar | source=Fin | x=Metric | y=FY2025 | title=FY2025
"""


def test_banner_and_subtitle_shift_header_to_row_4():
    ws = _wb(FULL)["Fin"]
    assert ws["A1"].value == "02 - Financial performance"
    assert ws["A2"].value.startswith("Unit:")
    assert {"A1:E1", "A2:E2"} <= {str(r) for r in ws.merged_cells.ranges}
    assert [c.value for c in ws[4]] == ["Metric", "Unit", "FY2024", "FY2025", "Status"]
    assert ws["A3"].value is None                       # the blank separator row
    assert ws["A6"].value == "Revenue"                  # data row n sits at header + n (section band is n=1 -> row 5)
    assert ws.freeze_panes == "A5"
    assert ws.auto_filter.ref == "A4:E8"
    assert ws.print_title_rows == "$4:$4"


def test_section_band_is_merged_and_not_a_number_column_member():
    ws = _wb(FULL)["Fin"]
    assert ws["A5"].value == "INCOME STATEMENT"
    assert "A5:E5" in {str(r) for r in ws.merged_cells.ranges}
    assert ws["A5"].font.bold
    assert ws["C6"].number_format != "@"


def test_ranges_start_below_the_header():
    ws = _wb(FULL)["Fin"]
    assert "E5:E8" in _ranges(ws)                       # highlight range: first row under the header
    assert any(str(dv.sqref).startswith("E5:E") for dv in ws.data_validations.dataValidation)
    totals = [c.value for c in ws[9]]                   # total row sits under the last data row (8)
    assert "SUBTOTAL(109,C5:C8)" in str(totals[2])


def test_input_columns_are_yellow_blue_and_skip_section_rows():
    ws = _wb(FULL)["Fin"]
    assert ws["C6"].fill.fgColor.rgb.endswith("FFF2CC") and ws["C6"].font.color.rgb.endswith("0000FF")
    assert ws["D8"].fill.fgColor.rgb.endswith("FFF2CC")
    assert ws["C5"].fill.fgColor.rgb.endswith("FFF2CC") is False   # section band keeps its own style


def test_charts_use_the_shifted_header_and_data_rows():
    wb = _wb(FULL)
    for ws in (wb["Fin"], wb["Dash"]):
        for ch in ws._charts:
            s0 = ch.series[0]
            assert s0.val.numRef.f.endswith("$D$5:$D$8"), s0.val.numRef.f      # data rows only
            assert s0.tx.strRef.f.endswith("!D4"), s0.tx.strRef.f             # series name = the header cell


def test_banner_only_puts_header_on_row_3():
    ws = _wb("# T\n## Sheet: S\nbanner: Title\n| A | B |\n|---|---|\n| x | 1 |\n")["S"]
    assert [c.value for c in ws[3]] == ["A", "B"]
    assert ws["A4"].value == "x" and ws.freeze_panes == "A4"


def test_no_banner_keeps_header_on_row_1():
    ws = _wb("# T\n## Sheet: S\n| A | B |\n|---|---|\n| x | 1 |\n")["S"]
    assert [c.value for c in ws[1]] == ["A", "B"] and ws.freeze_panes == "A2" and ws.auto_filter.ref == "A1:B2"


def test_widths_accept_header_names_and_letters():
    ws = _wb("# T\n## Sheet: S\n| Metric | Why it matters to X |\n|---|---|\n| x | y |\nwidths: Metric=33, Why it matters to X=55\n")["S"]
    assert ws.column_dimensions["A"].width == 33 and ws.column_dimensions["B"].width == 55
    ws = _wb("# T\n## Sheet: S\n| A | B |\n|---|---|\n| x | y |\nwidths: A=12 B=14\n")["S"]
    assert ws.column_dimensions["A"].width == 12 and ws.column_dimensions["B"].width == 14


def test_unknown_input_column_is_warned_not_fatal():
    B.build("# T\n## Sheet: S\n| A |\n|---|\n| 1 |\ninput: Nope\n", "t", True)
    assert any("input" in w for w in B._warnings.get())


def test_twelve_kpis_are_shown():
    kp = "\n".join(f"kpi: K{i} = {i}" for i in range(12))
    ws = _wb("# T\n## Sheet: S\n| A |\n|---|\n| 1 |\n## Dashboard: D\ntitle: D\n" + kp + "\n")["D"]
    labels = {str(c.value) for row in ws.iter_rows() for c in row if c.value is not None}
    assert {"K0", "K11"} <= labels

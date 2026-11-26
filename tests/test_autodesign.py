"""Auto-design: number formats, status colours, drop-downs, icons, totals, section bands, banners; and the switches that turn it off."""

import io
from unittest import mock

import openpyxl

from core.sheet_engine import build as B
from core.sheet_engine.autodesign import acct_number
from core.small_model import APP_CONFIG


def _wb(outline: str):
    return openpyxl.load_workbook(io.BytesIO(B.build(outline, "t", True)))


def _rules(ws):
    """{range: [(type, operator, formula)]} of the sheet's conditional formats."""
    return {str(k.sqref): [(r.type, r.operator, list(r.formula or [])) for r in v] for k, v in ws.conditional_formatting._cf_rules.items()}


PRICES = """# Prices
## Sheet: Prices
| Product | Price |
|---|---|
| Pen | 12.50 |
| Book | 8.00 |
| Bag | 30.25 |
"""

TRACKER = """# Ops
## Sheet: Tracker
| Date | Owner | Amount | Status |
|---|---|---|---|
| 2026-07-01 | Ana | 1,200 | Done |
| 2026-07-02 | Bob | (300) | Pending |
| 2026-07-03 | Cy | 4,500 | Blocked |
| 2026-07-04 | Di | 800 | done |
| 2026-07-05 | Ed | ~4,800 (est) | Pending |
| 2026-07-06 | Flo | 90 | Done |
"""

BUDGET = """# Budget
## Sheet: Budget
| Category | Budget | Actual | Variance | Growth % |
|---|---|---|---|---|
| Rent | 1000 | 1000 | 0 | 2.0% |
| Staff | 5000 | 5400 | -400 | 5.5% |
| Travel | 800 | 650 | 150 | -1.0% |
"""


def test_single_table_export_stays_light():
    wb = _wb(PRICES)
    ws = wb["Prices"]
    assert ws["A1"].value == "Product" and ws["B1"].font.bold          # no banner: header is row 1
    assert [ws.cell(row=r, column=2).number_format for r in (2, 3, 4)] == ["#,##0.00"] * 3
    assert ws["B2"].value == 12.5
    assert _rules(ws) == {} and not ws.data_validations.dataValidation
    assert ws.max_row == 4                                              # no total: Price is not additive
    assert ws["A1"].font.name == "Calibri"
    assert ws.oddFooter.center.text == "&A - Page &P of &N" and ws.sheet_properties.tabColor is not None
    assert "number formats on 1 column" in B.autodesign_note()


def test_tracker_gets_formats_status_colours_dropdown_and_total():
    ws = _wb(TRACKER)["Tracker"]
    assert ws["A2"].number_format == "yyyy-mm-dd" and hasattr(ws["A2"].value, "isoformat")
    assert ws["C2"].value == 1200 and ws["C2"].number_format == "#,##0;[Red]-#,##0"
    assert ws["C3"].value == -300                                       # (300) is an accounting negative
    assert ws["C6"].value == "~4,800 (est)"                             # text stays text
    rules = _rules(ws)["D2:D7"]
    assert sorted(f[0] for _, _, f in rules) == ['"Blocked"', '"Done"', '"Pending"']      # `done` and `Done` are one rule
    assert all(t == "cellIs" and op == "equal" for t, op, _ in rules)
    dv = ws.data_validations.dataValidation
    assert len(dv) == 1 and dv[0].formula1 == '"Done,Pending,Blocked"'
    assert ws["A8"].value == "Total" and ws["C8"].value == "=SUBTOTAL(109,C2:C7)"
    assert "1 number stored as text" in B.autodesign_note()


def test_budget_totals_red_negatives_and_icons():
    ws = _wb(BUDGET)["Budget"]
    assert ws["B5"].value == "=SUBTOTAL(109,B2:B4)" and ws["C5"].value == "=SUBTOTAL(109,C2:C4)"
    assert ws["D5"].value == "=SUBTOTAL(109,D2:D4)"                      # Variance is additive money here
    assert ws["E5"].value is None                                       # a percent is never totalled
    assert ws["D3"].number_format == "#,##0;[Red]-#,##0"
    assert ws["E2"].number_format == "0.0%;[Red]-0.0%" and ws["E2"].value == 0.02
    icons = [r for r in _rules(ws)["E2:E4"] if r[0] == "iconSet"]
    assert len(icons) == 1


def test_section_bands_from_lone_first_cells():
    ws = _wb("""# T
## Sheet: Fin
| Metric | FY24 | FY25 |
|---|---|---|
| INCOME | | |
| Revenue | 100 | 120 |
| Cost | 80 | 90 |
| COSTS | | |
| Rent | 5 | 6 |
""")["Fin"]
    assert ws["A2"].value == "INCOME" and any(str(r) == "A2:C2" for r in ws.merged_cells.ranges)
    assert ws["A3"].value == "Revenue" and ws["B3"].value == 100         # rows did not move


def test_a_sheet_that_formulas_point_at_gets_no_banner_and_formulas_still_hit_their_cells():
    wb = _wb("""# T
## Sheet: Data
| Item | Qty |
|---|---|
| a | 2 |
| b | 3 |
| c | 4 |
## Sheet: Calc
| Label | Value |
|---|---|
| Sum | =SUM(Data!B2:B4) |
""")
    assert wb["Data"]["A1"].value == "Item" and wb["Data"]["B4"].value == 4      # untouched layout, no total row either
    assert wb["Data"].max_row == 4
    assert wb["Calc"]["A1"].value == "Label" and wb["Calc"]["B2"].value == "=SUM(Data!B2:B4)"     # has a formula: unsafe too


def test_unreferenced_sheets_of_a_workbook_get_banner_and_subtitle():
    ws = _wb("""# T
## Sheet: One
Quarterly figures in BDT million
| Item | Qty |
|---|---|
| a | 2 |
## Sheet: Two
| Item | Qty |
|---|---|
| a | 2 |
""")["One"]
    assert ws["A1"].value == "One" and ws["A2"].value == "Quarterly figures in BDT million" and ws["A4"].value == "Item"


def test_style_plain_auto_off_and_config_off_add_nothing():
    plain = "style: plain\n" + TRACKER
    ws = _wb(plain)["Tracker"]
    assert ws["A1"].value == "Date" and _rules(ws) == {} and ws["C2"].number_format == "General"
    assert B.autodesign_note() == ""
    off = TRACKER.replace("## Sheet: Tracker", "## Sheet: Tracker\nauto: off")
    assert _rules(_wb(off)["Tracker"]) == {}
    with mock.patch.dict(APP_CONFIG, {"sheet_engine": {"autodesign": False}}):
        ws = _wb(TRACKER)["Tracker"]
    assert ws["A1"].value == "Date" and _rules(ws) == {} and ws.max_row == 7


def test_explicit_directives_win():
    ws = _wb(TRACKER + "format: Amount=currency:USD\nhighlight: Status = Done blue\ntotal: Amount=avg\n")["Tracker"]
    assert '"$"' in ws["C2"].number_format
    assert [r[2] for r in _rules(ws)["D2:D7"]] == [['"Done"']]          # the model's own rule only
    assert ws["C8"].value == "=SUBTOTAL(101,C2:C7)"


def test_percent_values_are_never_divided_silently():
    ws = _wb("""# T
## Sheet: S
| Bank | NPL % | Exchange rate |
|---|---|---|
| A | 3.5 | 110.25 |
| B | 4.1 | 110.50 |
""")["S"]
    assert ws["B2"].value == 3.5 and ws["B2"].number_format == '0.0"%"'
    assert ws["C2"].number_format == "#,##0.00"                         # a rate of 110.25 is not a percentage


def test_acct_number():
    assert acct_number("(1,234)") == -1234 and acct_number("(Tk 5)") == -5 and acct_number("12", "x") == "x"

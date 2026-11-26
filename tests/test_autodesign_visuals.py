"""Auto-design visuals: charts chosen from the data, the Contents sheet with internal links, the live-formula Dashboard, chart
polish, soft drop-downs, and the switches that keep all of it away."""

import io
from unittest import mock

import openpyxl

from core.sheet_engine import build as B
from core.sheet_engine import calc
import core.small_model  # noqa: F401  (loads the config the autodesign import needs before core.sheet_engine.calc's engines)


def _build(outline: str) -> bytes:
    return B.build(outline, "t", True)


def _wb(outline: str):
    return openpyxl.load_workbook(io.BytesIO(_build(outline)))


def _calc(data: bytes) -> dict:
    with mock.patch.object(calc, "_prefer_external", lambda *a, **k: None):       # the built-in calculator, never Excel/LibreOffice
        return calc.calculate(data)


def _chart_xml(data: bytes) -> str:
    import zipfile
    z = zipfile.ZipFile(io.BytesIO(data))
    return "".join(z.read(n).decode("utf-8") for n in z.namelist() if n.startswith("xl/charts/chart"))


def _refs(chart) -> list:
    """[(series values ref, categories ref)] of a chart."""
    out = []
    for se in chart.series:
        cat = se.cat.numRef or se.cat.strRef if se.cat else None
        out.append((se.val.numRef.f, cat.f if cat else None))
    return out


FIN = """# Acme
## Sheet: Financials
| Year | Revenue | Net profit | Margin |
|---|---|---|---|
| 2015 | 1000 | 100 | 10% |
| 2016 | 1100 | 120 | 11% |
| 2017 | 1200 | 130 | 10.8% |
| 2018 | 1300 | 150 | 11.5% |
| 2019 | 1400 | 160 | 11.4% |
| 2020 | 1500 | 170 | 11.3% |
| 2021 | 1600 | 190 | 11.9% |
## Sheet: Notes
| Topic | Comment |
|---|---|
| Risk | Watch the margin |
| Outlook | Stable |
"""


def test_period_series_gets_line_chart_with_right_ranges_and_a_second_for_percent():
    ws = _wb(FIN)["Financials"]
    assert len(ws._charts) == 2
    big, pct = ws._charts
    assert type(big).__name__ == "LineChart"                 # 7 periods > 6
    # header row is 3 (banner), data rows 4..10; the series title is the header cell
    assert _refs(big) == [("'Financials'!$B$4:$B$10", "'Financials'!$A$4:$A$10"),
                          ("'Financials'!$C$4:$C$10", "'Financials'!$A$4:$A$10")]
    assert [r[0] for r in _refs(pct)] == ["'Financials'!$D$4:$D$10"]       # a percent column never shares the money axis
    assert ws["A3"].value == "Year" and ws["A10"].value == 2021


def test_few_periods_get_a_column_chart_and_single_table_charts_use_row_1():
    ws = _wb("# T\n## Sheet: Fin\n| Period | Sales |\n|---|---|\n| FY2023 | 10 |\n| FY2024 | 12 |\n| FY2025 | 15 |\n")["Fin"]
    assert len(ws._charts) == 1 and type(ws._charts[0]).__name__ == "BarChart"
    assert ws._charts[0].series[0].val.numRef.f == "'Fin'!$B$2:$B$4"        # no banner: header row 1


def test_category_table_gets_horizontal_bar_chart_when_labels_are_long():
    out = ("# T\n## Sheet: Banks\n| Bank | Assets |\n|---|---|\n| First Commercial Bank of the North | 500 |\n"
           "| Second National Trust Bank | 400 |\n| Third Regional Savings Bank | 300 |\n| Fourth Bank | 250 |\n")
    ws = _wb(out)["Banks"]
    assert len(ws._charts) == 1
    ch = ws._charts[0]
    assert type(ch).__name__ == "BarChart" and ch.type == "bar"          # horizontal
    assert ch.series[0].val.numRef.f == "'Banks'!$B$2:$B$5"


def test_short_labels_get_vertical_bars():
    ws = _wb("# T\n## Sheet: Units\n| Region | Sales |\n|---|---|\n| North | 5 |\n| South | 7 |\n| East | 9 |\n")["Units"]
    assert ws._charts[0].type == "col"


def test_share_table_gets_a_doughnut():
    ws = _wb("# T\n## Sheet: Mix\n| Segment | Share |\n|---|---|\n| Retail | 45% |\n| SME | 30% |\n| Corporate | 25% |\n")["Mix"]
    assert [type(c).__name__ for c in ws._charts] == ["DoughnutChart"]
    ws = _wb("# T\n## Sheet: Mix\n| Segment | Share % |\n|---|---|\n| Retail | 45 |\n| SME | 30 |\n| Corporate | 25 |\n")["Mix"]
    assert [type(c).__name__ for c in ws._charts] == ["DoughnutChart"]


def test_a_share_column_that_does_not_sum_to_a_whole_is_not_a_pie():
    ws = _wb("# T\n## Sheet: Mix\n| Segment | Share |\n|---|---|\n| Retail | 45% |\n| SME | 30% |\n| Corporate | 10% |\n")["Mix"]
    assert not any(type(c).__name__ in ("PieChart", "DoughnutChart") for c in ws._charts)


def test_text_only_and_status_tables_get_no_chart_and_a_small_workbook_gets_no_extras():
    wb = _wb("# T\n## Sheet: Notes\n| Topic | Comment |\n|---|---|\n| Risk | Watch it |\n| Outlook | Stable |\n| Plan | Go |\n")
    assert wb.sheetnames == ["Notes"] and not wb["Notes"]._charts
    wb = _wb(FIN)
    assert wb.sheetnames == ["Financials", "Notes"]                       # 2 sheets: no Contents, no Dashboard


def test_mixed_unit_metric_value_sheet_is_not_charted():
    out = "# T\n## Sheet: Key Metrics\n| Metric | Value | Unit |\n|---|---|---|\n| Assets | 1,234,567 | BDT mn |\n| ROE | 12.5% | % |\n| Branches | 180 | count |\n"
    assert not _wb(out)["Key Metrics"]._charts


def test_charts_sit_right_of_the_table_and_do_not_overlap_each_other():
    ws = _wb(FIN)["Financials"]
    anchors = sorted((c.anchor._from.row, c.anchor._from.col) for c in ws._charts)
    assert all(col >= 5 for _, col in anchors)                            # table is A..D, first free column is F (index 5)
    assert anchors[1][0] - anchors[0][0] >= 17                            # an 8.5 cm chart is about 16 rows tall
    assert anchors[0][0] + 1 == 3                                         # the first chart starts at the header row


def test_explicit_chart_suppresses_the_auto_ones_on_that_sheet():
    out = FIN.replace("| 2021 | 1600 | 190 | 11.9% |\n", "| 2021 | 1600 | 190 | 11.9% |\nchart: line | title=Mine | x=Year | y=Revenue\n")
    ws = _wb(out)["Financials"]
    assert len(ws._charts) == 1


def test_plain_style_gets_no_charts_contents_or_dashboard_and_keeps_old_chart_styling():
    wb = _wb("style: plain\n" + RESEARCH)
    assert wb.sheetnames == ["Key Metrics", "Financial Performance", "Segments", "Notes", "Sources"]
    assert not any(ws._charts for ws in wb)
    out = "style: plain\n# T\n## Sheet: S\n| A | B |\n|---|---|\n| x | 1 |\n| y | 2 |\nchart: bar | x=A | y=B | title=T\n"
    ch = _wb(out)["S"]._charts[0]
    assert ch.y_axis.majorGridlines is not None and "E9EEF4" not in _chart_xml(_build(out))
    assert ch.anchor._from.row == 1                                       # the old anchor: row 2


def test_auto_charts_are_polished_and_axis_follows_the_y_format():
    ws = _wb(FIN)["Financials"]
    big, pct = ws._charts
    assert big.y_axis.numFmt.formatCode == "#,##0" and pct.y_axis.numFmt.formatCode == "0%"
    xml = _chart_xml(_build(FIN))
    assert "E9EEF4" in xml and "D9E2ED" in xml and "<a:t>Financials</a:t>" in xml      # gridline, frame, styled title
    plain = "style: plain\n" + FIN.replace("| 2021 | 1600 | 190 | 11.9% |\n", "| 2021 | 1600 | 190 | 11.9% |\nchart: line | x=Year | y=Revenue\n")
    assert "E9EEF4" not in _chart_xml(_build(plain))


# ---- a research style workbook: Contents + Dashboard -----------------------------------------------------------------
RESEARCH = """# Acme Bank Research
## Sheet: Key Metrics
| Metric | Value | Unit |
|---|---|---|
| Total assets | 1,234,567 | BDT mn |
| Net profit | 4,321 | BDT mn |
| ROE | 12.5% | % |
| Branches | 180 | count |
| Founded | 1999 | year |
## Sheet: Financial Performance
| Year | Revenue | Net profit | Margin |
|---|---|---|---|
| 2015 | 1000 | 100 | 10% |
| 2016 | 1100 | 120 | 11% |
| 2017 | 1200 | 130 | 10.8% |
| 2018 | 1300 | 150 | 11.5% |
| 2019 | 1400 | 160 | 11.4% |
| 2020 | 1500 | 170 | 11.3% |
| 2021 | 1600 | 190 | 11.9% |
| 2022 | 1700 | 200 | 11.8% |
| 2023 | 1800 | 210 | 11.7% |
| 2024 | 1900 | 230 | 12.1% |
## Sheet: Segments
| Segment | Share |
|---|---|
| Retail | 45% |
| SME | 30% |
| Corporate | 25% |
## Sheet: Notes
| Topic | Comment |
|---|---|
| Risk | The bank faces a number of risks that are discussed here in some length |
| Outlook | Stable |
## Sheet: Sources
| Source | Status |
|---|---|
| Annual report | Verified |
| Press | Pending |
| Web | Estimated |
"""


def _kpis(wb, res) -> dict:
    """{label: calculated value} of the Dashboard's tiles (label cell is above its formula cell)."""
    d = wb["Dashboard"]
    out = {}
    for row in d.iter_rows():
        for c in row:
            if isinstance(c.value, str) and c.value.startswith("="):
                out[d.cell(row=c.row - 1, column=c.column).value] = res["values"][("Dashboard", c.coordinate)]
    return out


def test_research_workbook_gets_contents_with_working_links_and_a_correct_dashboard():
    data = _build(RESEARCH)
    wb = openpyxl.load_workbook(io.BytesIO(data))
    assert wb.sheetnames == ["Contents", "Dashboard", "Key Metrics", "Financial Performance", "Segments", "Notes", "Sources"]
    c = wb["Contents"]
    assert c["A1"].value == "Acme Bank Research" and c.sheet_view.showGridLines is False
    assert [x.value for x in c[4]] == ["#", "Sheet", "What it holds"]
    names = [c.cell(row=r, column=2).value for r in range(5, 12)]
    assert names == wb.sheetnames[1:] + [None] or names[:6] == wb.sheetnames[1:]
    for r in range(5, 11):
        cell = c.cell(row=r, column=2)
        assert cell.hyperlink is not None and cell.hyperlink.location == f"'{cell.value}'!A1"
        assert cell.value in wb.sheetnames
    assert c.cell(row=5, column=2).font.underline == "single"
    legend = [c.cell(row=r, column=1).value for r in range(12, 20) if c.cell(row=r, column=1).value]
    assert "Colour legend" in legend and "Amber" in legend                # Sources has a status column
    res = _calc(data)
    assert res["errors"] == []
    assert _kpis(wb, res) == {"Total assets (BDT mn)": 1234567, "Net profit (BDT mn)": 4321, "ROE": 0.125, "Branches (count)": 180}
    d = wb["Dashboard"]
    assert [type(x).__name__ for x in d._charts]                          # charts reuse the auto charts' sources
    assert d["B5"].number_format == "#,##0" and d["J5"].number_format == "0.0%"
    assert B.take_autodesign_report()[-2:] == ["Dashboard with 4 KPI tiles and 2 charts", "Contents sheet"]


def test_period_series_kpis_use_the_latest_row_and_the_final_header_row():
    out = ("# T\n## Sheet: Financials\n| Year | Revenue | Net profit |\n|---|---|---|\n| 2022 | 100 | 10 |\n| 2023 | 120 | 12 |\n| 2024 | 150 | 18 |\n"
           "## Sheet: Loans\n| Period | Loans |\n|---|---|\n| FY2024 | 900 |\n| FY2025 | 950 |\n| FY2026 | 990 |\n"
           "## Sheet: Notes\n| Topic | Comment |\n|---|---|\n| a | b |\n")
    data = _build(out)
    wb = openpyxl.load_workbook(io.BytesIO(data))
    res = _calc(data)
    assert res["errors"] == []
    assert _kpis(wb, res) == {"Revenue 2024": 150, "Net profit 2024": 18, "Loans FY2026": 990}


def test_newest_period_listed_first_is_found():
    out = ("# T\n## Sheet: A\n| Year | Revenue |\n|---|---|\n| 2024 | 150 |\n| 2023 | 120 |\n| 2022 | 100 |\n"
           "## Sheet: B\n| Year | Loans |\n|---|---|\n| 2024 | 9 |\n| 2023 | 8 |\n| 2022 | 7 |\n"
           "## Sheet: C\n| Year | Cost |\n|---|---|\n| 2024 | 5 |\n| 2023 | 4 |\n| 2022 | 3 |\n")
    data = _build(out)
    assert _kpis(openpyxl.load_workbook(io.BytesIO(data)), _calc(data)) == {"Revenue 2024": 150, "Loans 2024": 9, "Cost 2024": 5}


def test_kpis_resolve_on_sheets_with_names_longer_than_31_characters():
    a, b = "Executive Summary and Key Highlights - Board", "Executive Summary and Key Highlights - Detail"
    out = (f"# T\n## Sheet: {a}\n| Metric | Value |\n|---|---|\n| Alpha | 11 |\n| Beta | 22 |\n| Eps | 5 |\n"
           f"## Sheet: {b}\n| Metric | Value |\n|---|---|\n| Gamma | 33 |\n| Delta | 44.5 |\n| Zeta | 6 |\n"
           "## Sheet: Notes\n| Topic | Comment |\n|---|---|\n| a | b |\n")
    data = _build(out)
    wb = openpyxl.load_workbook(io.BytesIO(data))
    res = _calc(data)
    assert res["errors"] == []
    assert _kpis(wb, res) == {"Alpha": 11, "Beta": 22, "Eps": 5, "Gamma": 33, "Delta": 44.5, "Zeta": 6}
    assert all(len(n) <= 31 for n in wb.sheetnames) and len(set(wb.sheetnames)) == len(wb.sheetnames)
    f = [c.value for row in wb["Dashboard"].iter_rows() for c in row if isinstance(c.value, str) and c.value.startswith("=")]
    assert all(f_.startswith("='") for f_ in f)                           # quoted: the names hold spaces


def test_section_bands_and_a_subtitle_are_counted_in_kpi_addresses():
    out = ("# T\n## Sheet: Summary\nFigures at year end\n| Metric | Value |\n|---|---|\n| >> ASSETS | |\n| Cash | 10 |\n| Loans | 20 |\n"
           "| >> LIABILITIES | |\n| Deposits | 30 |\n"
           "## Sheet: B\n| Topic | Comment |\n|---|---|\n| a | b |\n## Sheet: C\n| Topic | Comment |\n|---|---|\n| a | b |\n")
    data = _build(out)
    wb = openpyxl.load_workbook(io.BytesIO(data))
    res = _calc(data)
    assert res["errors"] == [] and _kpis(wb, res) == {"Cash": 10, "Loans": 20, "Deposits": 30}


def test_fewer_than_three_kpis_means_no_dashboard_and_the_report_says_why():
    out = "# T\n## Sheet: Summary\n| Metric | Value |\n|---|---|\n| a | 1 |\n| b | 2 |\n| c | x |\n## Sheet: B\n| T | C |\n|---|---|\n| a | b |\n## Sheet: C\n| T | C |\n|---|---|\n| a | b |\n"
    wb = _wb(out)
    assert "Dashboard" not in wb.sheetnames
    assert any(r.startswith("no Dashboard") for r in B.take_autodesign_report())


def test_explicit_dashboard_or_model_named_contents_suppress_the_auto_ones():
    dash = RESEARCH + "## Dashboard: Mine\nkpi: Assets = =SUM('Key Metrics'!B4:B5)\n"
    wb = _wb(dash)
    assert "Dashboard" not in wb.sheetnames and "Mine" in wb.sheetnames and wb.sheetnames[0] == "Contents"
    cont = RESEARCH.replace("## Sheet: Notes", "## Sheet: Contents\n| Part | Note |\n|---|---|\n| a | b |\n## Sheet: Notes")
    wb = _wb(cont)
    assert wb.sheetnames.count("Contents") == 1 and wb.sheetnames[0] == "Dashboard"
    assert not any(c.hyperlink for row in wb["Contents"].iter_rows() for c in row)


def test_three_sheets_get_a_dashboard_but_no_contents():
    out = RESEARCH.split("## Sheet: Notes")[0]
    wb = _wb(out)
    assert wb.sheetnames == ["Dashboard", "Key Metrics", "Financial Performance", "Segments"]


def test_extra_sheets_respect_the_thirty_sheet_limit():
    out = "# T\n" + "".join(f"## Sheet: S{i}\n| A | B |\n|---|---|\n| x | {i} |\n" for i in range(30))
    assert len(_wb(out).sheetnames) == 30


# ---- the link directive and soft drop-downs --------------------------------------------------------------------------
def test_link_directive_makes_internal_hyperlinks_and_skips_unknown_names():
    out = "style: plain\n# T\n## Sheet: Index\n| Page | Note |\n|---|---|\n| Data | x |\n| Nowhere | y |\nlink: Page\n## Sheet: Data\n| A |\n|---|\n| 1 |\n"
    ws = _wb(out)["Index"]
    assert ws["A2"].hyperlink.location == "'Data'!A1" and ws["A3"].hyperlink is None


def test_auto_drop_downs_warn_while_explicit_ones_stop():
    out = ("# T\n## Sheet: Tasks\n| Task | Status |\n|---|---|\n| a | Done |\n| b | Pending |\n| c | Done |\n"
           "## Sheet: Other\n| Item | Status |\n|---|---|\n| a | Done |\n| b | Pending |\n| c | Done |\nvalidate: Status in [Done, Pending]\n")
    wb = _wb(out)
    auto = wb["Tasks"].data_validations.dataValidation[0]
    explicit = wb["Other"].data_validations.dataValidation[0]
    assert auto.errorStyle == "warning" and "Done" in auto.formula1
    assert explicit.errorStyle in (None, "stop")


# ---- wide period tables and KPI selection ----------------------------------------------------------------------------
WIDE = """# T
## Sheet: Financials
| Metric | Unit | FY2021 | FY2022 | FY2023 | FY2024 | FY2025 |
|---|---|---|---|---|---|---|
| Net profit | Tk crore | 100 | 120 | 140 | 160 | 180 |
| Total assets | Tk crore | 1000 | 1100 | 1200 | 1300 | 1500 |
| Net margin | % | 10% | 11% | 12% | 13% | 14% |
| Comment | text | n/a | n/a | n/a | n/a | n/a |
## Sheet: Notes
| Topic | Comment |
|---|---|
| a | b |
"""


def test_wide_period_table_gets_row_series_charts_with_header_row_categories():
    ws = _wb(WIDE)["Financials"]
    assert len(ws._charts) == 2                                           # money rows, then the percent row on its own
    money, pct = ws._charts
    assert type(money).__name__ == "BarChart"                             # 5 periods <= 6
    assert [s.val.numRef.f for s in money.series] == ["'Financials'!$C$4:$G$4", "'Financials'!$C$5:$G$5"]
    assert [s.tx.strRef.f for s in money.series] == ["'Financials'!$A$4", "'Financials'!$A$5"]
    assert money.series[0].cat.numRef is None and money.series[0].cat.strRef.f == "'Financials'!$C$3:$G$3"       # header cells, row 3
    assert [s.val.numRef.f for s in pct.series] == ["'Financials'!$C$6:$G$6"]
    xml = _chart_xml(_build(WIDE))
    assert "0%" in xml and "#,##0" in xml


def test_wide_table_with_many_periods_is_a_line_chart_and_explicit_by_row_works_in_plain():
    hdr = "| Metric | " + " | ".join(f"FY{y}" for y in range(2016, 2026)) + " |\n|---|" + "---|" * 10 + "\n"
    row = "| Revenue | " + " | ".join(str(100 + i) for i in range(10)) + " |\n"
    ws = _wb("# T\n## Sheet: W\n" + hdr + row + row.replace("Revenue", "Cost") + "## Sheet: N\n| a | b |\n|---|---|\n| 1 | 2 |\n")["W"]
    assert type(ws._charts[0]).__name__ == "LineChart"
    plain = "style: plain\n# T\n## Sheet: W\n" + hdr + row + row.replace("Revenue", "Cost") + "chart: line | by=row | cols=FY2016:FY2025 | rows=Revenue,Cost\n"
    ch = _wb(plain)["W"]._charts[0]
    assert [s.val.numRef.f for s in ch.series] == ["'W'!$B$2:$K$2", "'W'!$B$3:$K$3"]


def test_wide_table_kpis_use_the_latest_period_cell_and_evaluate():
    out = WIDE + "## Sheet: More\n| Topic | Comment |\n|---|---|\n| a | b |\n"
    data = _build(out)
    wb = openpyxl.load_workbook(io.BytesIO(data))
    res = _calc(data)
    assert res["errors"] == []
    assert _kpis(wb, res) == {"Net profit FY2025": 180, "Total assets FY2025": 1500, "Net margin FY2025": 0.14}
    assert wb["Dashboard"]["J5"].number_format == "0.0%"


def test_wide_table_listing_newest_period_first_is_found():
    out = ("# T\n## Sheet: W\n| Metric | FY2025 | FY2024 | FY2023 |\n|---|---|---|---|\n| Revenue | 30 | 20 | 10 |\n| Cost | 9 | 8 | 7 |\n| Tax | 3 | 2 | 1 |\n"
           "## Sheet: A\n| T | C |\n|---|---|\n| a | b |\n## Sheet: B\n| T | C |\n|---|---|\n| a | b |\n")
    data = _build(out)
    assert _kpis(openpyxl.load_workbook(io.BytesIO(data)), _calc(data)) == {"Revenue FY2025": 30, "Cost FY2025": 9, "Tax FY2025": 3}


def test_year_twins_collapse_to_the_latest_and_a_growth_column_is_not_the_value():
    out = ("# T\n## Sheet: Executive Summary\n| KPI | Latest Value | YoY Growth |\n|---|---|---|\n| Net Profit 2024 | 80 | 0.2 |\n| Net Profit 2025 | 100 | 0.25 |\n"
           "| Revenue 2024 | 400 | 0.1 |\n| Revenue 2025 | 500 | 0.25 |\n| Deposits 2025 | 900 | 0.3 |\n| Loans 2025 | 700 | 0.3 |\n"
           "## Sheet: A\n| T | C |\n|---|---|\n| a | b |\n## Sheet: B\n| T | C |\n|---|---|\n| a | b |\n")
    data = _build(out)
    assert _kpis(openpyxl.load_workbook(io.BytesIO(data)), _calc(data)) == {"Net Profit 2025": 100, "Revenue 2025": 500, "Deposits 2025": 900, "Loans 2025": 700}


def test_a_thin_summary_is_topped_up_from_other_sheets():
    out = ("# T\n## Sheet: Summary\n| Metric | Value |\n|---|---|\n| Net profit 2024 | 80 |\n| Net profit 2025 | 100 |\n| Branches | 5 |\n"
           "## Sheet: Fin\n| Year | Revenue | Cost |\n|---|---|---|\n| 2023 | 10 | 5 |\n| 2024 | 20 | 6 |\n| 2025 | 30 | 7 |\n"
           "## Sheet: B\n| T | C |\n|---|---|\n| a | b |\n")
    data = _build(out)
    got = _kpis(openpyxl.load_workbook(io.BytesIO(data)), _calc(data))
    assert got["Net Profit 2025".replace("Profit", "profit")] == 100 and "Net profit 2024" not in got
    assert got["Revenue 2025"] == 30 and got["Branches"] == 5


def test_wide_table_rows_get_their_own_number_formats():
    out = ("# T\n## Sheet: W\n| Metric | Unit | FY2023 | FY2024 | FY2025 |\n|---|---|---|---|---|\n"
           "| ROA | ratio | 0.231 | 0.25 | 0.27 |\n| Revenue | Tk crore | 1200 | 1300.5 | 1500 |\n| Cost to income | % | 45.5 | 44.1 | 43.0 |\n"
           "## Sheet: N\n| a | b |\n|---|---|\n| 1 | 2 |\n")
    ws = _wb(out)["W"]
    fm = lambda r: {ws.cell(row=r, column=c).number_format for c in (3, 4, 5)}      # noqa: E731
    assert fm(4) == {"0.0%"} and fm(5) == {"#,##0.0"} and fm(6) == {'0.0"%"'}
    assert ws["C4"].value == 0.231 and ws["A4"].number_format == "General"
    assert any(r.startswith("row formats on 3 rows") for r in B.take_autodesign_report())
    # the model's own column format wins: no row formats are added
    ws = _wb(out.replace("## Sheet: N", "format: FY2025=#,##0.00\n## Sheet: N"))["W"]
    assert ws["E4"].number_format == "#,##0.00"
    # plain: untouched
    assert _wb("style: plain\n" + out)["W"]["C4"].number_format == "General"

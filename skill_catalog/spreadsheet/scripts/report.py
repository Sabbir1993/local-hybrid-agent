"""Create a structured, visually polished Excel report using native Excel charts."""
from __future__ import annotations

import math
import zipfile
from pathlib import Path
from typing import Any
import xlsxwriter

NAVY = "#152C48"
NAVY2 = "#244565"
BLUE = "#2166B1"
CYAN = "#E6F3FB"
BG = "#F5F8FC"
TEXT = "#203047"
MUTED = "#61758A"
GREEN = "#16764B"
RED = "#B42332"
BORDER = "#D9E2ED"


def _clean_text(v: Any, limit: int = 32000) -> str:
    if v is None:
        return ""
    s = str(v).replace("\x00", "")
    return s[:limit]


def _write_safe(ws, row: int, col: int, v: Any, fmt=None):
    """Keep untrusted spreadsheet text inert; never auto-convert user text to formulas/URLs."""
    if v is None:
        ws.write_blank(row, col, None, fmt)
    elif isinstance(v, bool):
        ws.write_boolean(row, col, v, fmt)
    elif isinstance(v, (int, float)) and math.isfinite(v) and not isinstance(v, bool):
        ws.write_number(row, col, float(v), fmt)
    else:
        try:
            from datetime import date, datetime
            if isinstance(v, (date, datetime)):
                ws.write_string(row, col, v.isoformat(), fmt)
                return
        except ImportError:
            pass
        ws.write_string(row, col, _clean_text(v), fmt)


def _init_sheet(wb, name, headers, widths=None, title=None):
    ws = wb.add_worksheet(name)
    ws.hide_gridlines(2)
    ws.set_tab_color(BLUE)
    ws.set_zoom(90)
    ws.freeze_panes(4, 1)
    widths = widths or []
    for i, w in enumerate(widths):
        ws.set_column(i, i, w)
    if not widths:
        ws.set_column(0, max(20, min(35, len(headers) * 4)))
    last_col = max(4, len(headers) - 1)
    ws.merge_range(0, 0, 1, last_col, title or name.replace("_", " "), wb.add_format({
        "font_name": "Aptos Display", "font_size": 21, "bold": True,
        "font_color": "white", "bg_color": NAVY, "valign": "vcenter", "indent": 1}))
    ws.set_row(0, 25)
    ws.set_row(1, 16)
    header_fmt = wb.add_format({"bold": True, "font_name": "Aptos", "font_size": 10,
                                "font_color": "white", "bg_color": NAVY2,
                                "bottom": 1, "bottom_color": BORDER, "text_wrap": True,
                                "valign": "vcenter"})
    for i, h in enumerate(headers):
        ws.write_string(3, i, str(h), header_fmt)
    ws.set_row(3, 33)
    return ws


def _format_styles(wb):
    return {
        "text": wb.add_format({"font_name": "Aptos", "font_size": 10, "font_color": TEXT,
                               "valign": "top", "bottom": 1, "bottom_color": "#EDF1F6"}),
        "wrap": wb.add_format({"font_name": "Aptos", "font_size": 10, "font_color": TEXT,
                               "valign": "top", "text_wrap": True, "bottom": 1, "bottom_color": "#EDF1F6"}),
        "int": wb.add_format({"font_name": "Aptos", "font_color": TEXT, "num_format": "#,##0",
                              "bottom": 1, "bottom_color": "#EDF1F6"}),
        "num": wb.add_format({"font_name": "Aptos", "font_color": TEXT, "num_format": "#,##0.00;[Red](#,##0.00)",
                              "bottom": 1, "bottom_color": "#EDF1F6"}),
        "pct": wb.add_format({"font_name": "Aptos", "font_color": TEXT, "num_format": "0.0%;[Red](0.0%)",
                              "bottom": 1, "bottom_color": "#EDF1F6"}),
        "muted": wb.add_format({"font_name": "Aptos", "font_color": MUTED, "italic": True, "text_wrap": True}),
        "link": wb.add_format({"font_name": "Aptos", "font_color": BLUE, "underline": True}),
    }


def write_report(result: dict, output: str | Path) -> dict[str, Any]:
    path = Path(output)
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.resolve() == Path(result["source_file"]).resolve():
        raise ValueError("Refusing to overwrite input workbook")
    wb = xlsxwriter.Workbook(str(path), {"strings_to_urls": False, "strings_to_formulas": False,
                                         "constant_memory": False, "use_zip64": True})
    wb.set_properties({"title": "Advanced Excel Analysis Report", "subject": "Data profiling and executive reporting",
                       "comments": "Local analytics. Exact aggregate metrics; sampled quantiles labeled."})
    fmt = _format_styles(wb)
    rc = result["row_count"]
    roles = result["roles"]
    chart_count = 0
    sheet_names = []

    # Create executive dashboard first, then data worksheets that charts refer to.
    dash = wb.add_worksheet("Executive_Dashboard")
    sheet_names.append("Executive_Dashboard")
    dash.hide_gridlines(2)
    dash.set_zoom(80)
    dash.set_tab_color("#2F67BB")
    dash.set_landscape()
    dash.fit_to_pages(1, 2)
    dash.set_margins(.25, .25, .35, .35)
    dash.set_paper(9)
    dash.set_column("A:Q", 12)
    for row in range(0, 55):
        dash.set_row(row, 21)
    dash.merge_range("A1:Q3", "DATA INTELLIGENCE  |  EXECUTIVE DASHBOARD",
                     wb.add_format({"bg_color": NAVY, "font_color": "white", "font_size": 22,
                                    "bold": True, "font_name": "Aptos Display", "valign": "vcenter", "indent": 1}))
    dash.merge_range("B4:P4", f"Source: {Path(result['source_file']).name}    •    Sheet: {result['source_sheet']}    •    Generated (UTC): {result['generated_at']}",
                     wb.add_format({"font_name": "Aptos", "font_color": MUTED, "font_size": 10}))

    # All dashboard KPI formulas reference documented source/analysis cells.
    kpis = [
        ("ROWS ANALYZED", "=Source_Metadata!B7", rc, "#,##0"),
        ("SOURCE FIELDS", "=Source_Metadata!B8", len(result["headers"]), "#,##0"),
        (f"TOTAL {roles['value']}" if roles["value"] else "DATA QUALITY: DUPLICATES",
         "=Source_Metadata!B9" if roles["value"] else "=Source_Metadata!B10",
         result["value_sum"] if roles["value"] else result["quality"]["exact_duplicate_rows"],
         "#,##0.00" if roles["value"] else "#,##0"),
        ("SUCCESS: KNOWN OUTCOMES" if result["status"]["recognized"] else "DATASET PROFILE",
         "=IFERROR(Source_Metadata!B11/Source_Metadata!B13,0)" if result["status"]["recognized"] else "=Source_Metadata!B7",
         result["status"]["success"] / result["status"]["recognized"] if result["status"]["recognized"] else rc,
         "0.0%" if result["status"]["recognized"] else "#,##0"),
    ]
    kpi_locs = [("B6:D6", "B7:D9"), ("F6:H6", "F7:H9"), ("J6:L6", "J7:L9"), ("N6:P6", "N7:P9")]
    for (label, formula, cached, number_format), (label_span, value_span) in zip(kpis, kpi_locs):
        dash.merge_range(label_span, label, wb.add_format({"bg_color": "#E6EDF5", "font_color": NAVY2,
                                                       "font_size": 10, "font_name": "Aptos", "bold": True,
                                                       "align": "center", "valign": "vcenter"}))
        value_fmt = wb.add_format({"bg_color": "#FFFFFF", "font_color": NAVY, "font_size": 24,
                                  "bold": True, "font_name": "Aptos Display", "num_format": number_format,
                                  "align": "center", "valign": "vcenter", "border": 1,
                                  "border_color": BORDER})
        dash.merge_range(value_span, "", value_fmt)
        first_cell = value_span.split(":")[0]
        dash.write_formula(first_cell, formula, value_fmt, cached)
    dash.merge_range("B11:P11", "DATA STORY  |  MOST IMPORTANT FINDINGS",
                     wb.add_format({"font_color": "white", "bg_color": NAVY2, "font_size": 11,
                                    "bold": True, "font_name": "Aptos", "indent": 1}))
    for idx, insight in enumerate(result["insights"][:3]):
        dash.merge_range(11 + idx, 1, 11 + idx, 15,
                         f"{idx+1}. {insight['title']} — {insight['evidence']}",
                         wb.add_format({"bg_color": "#EFF5FB", "font_name": "Aptos", "font_size": 10,
                                        "font_color": TEXT, "text_wrap": True, "indent": 1, "valign": "vcenter"}))
        dash.set_row(11 + idx, 28)

    # Source Metadata, with fixed cell addresses used by dashboard KPI references.
    meta = _init_sheet(wb, "Source_Metadata", ["Property", "Value"], [32, 92], "SOURCE  |  PROVENANCE & PROCESSING")
    sheet_names.append("Source_Metadata")
    metadata = [
        ("Input file", result["source_file"]), ("Selected source worksheet", result["source_sheet"]),
        ("Data rows analyzed", rc), ("Source field count", len(result["headers"])),
        ("Total selected value", result["value_sum"]), ("Exact duplicate rows", result["quality"]["exact_duplicate_rows"]),
        ("Recognized successes", result["status"]["success"]),
        ("Recognized failures", result["status"]["failure"]),
        ("Recognized status outcomes", result["status"]["recognized"]),
        ("Input size (bytes)", result["source_size"]),
        ("Generated UTC", result["generated_at"]), ("Analysis mode", result["mode"]),
        ("Date field", roles["date"] or "Not detected"), ("Value field", roles["value"] or "Not detected"),
        ("Category field", roles["group"] or "Not detected"), ("Status field", roles["status"] or "Not detected"),
        ("All source worksheets", ", ".join(result["all_source_sheets"])),
        ("Selected worksheet policy", "Exactly one worksheet analyzed; other sheets not combined."),
    ]
    # Zero-based row 4 = A5; row 5 = A6, dashboard references B7 for rows.
    for row, (k, v) in enumerate(metadata, 4):
        _write_safe(meta, row, 0, k, fmt["text"])
        _write_safe(meta, row, 1, v, fmt["int"] if isinstance(v, int) else fmt["num"] if isinstance(v, float) else fmt["wrap"])
    meta.freeze_panes(4, 0)

    summary = _init_sheet(wb, "Executive_Summary", ["Metric", "Result", "Explanation"], [38, 22, 112],
                          "EXECUTIVE SUMMARY  |  KEY METRICS")
    sheet_names.append("Executive_Summary")
    summary_rows = [
        ("Rows examined", rc, "Complete nonblank input records processed."),
        ("Columns detected", len(result["headers"]), "All nonempty header fields in the selected worksheet."),
        ("Exact duplicate rows", result["quality"]["exact_duplicate_rows"], "Exact complete-row duplicates, not necessarily repeated business events."),
        ("Selected value column", roles["value"] or "Not detected", "One numeric column selected for financial or quantitative summaries."),
        ("Total selected value", result["value_sum"] if roles["value"] else "N/A", "Sum of finite values; missing or invalid values excluded."),
        ("Valid numeric values", result["value_valid_count"] if roles["value"] else "N/A", "Count used in the selected-value sum."),
        ("Selected date column", roles["date"] or "Not detected", "Used for calendar-month aggregations when available."),
        ("Selected group column", roles["group"] or "Not detected", "Used for category volume and value comparisons."),
        ("Recognized success share", (result["status"]["success"] / result["status"]["recognized"]) if result["status"]["recognized"] else "N/A",
         "Denominator includes only recognized success and failure labels."),
    ]
    for i, row_values in enumerate(summary_rows, 4):
        for j, v in enumerate(row_values):
            target = fmt["num"] if j == 1 and isinstance(v, float) else fmt["int"] if j == 1 and isinstance(v, int) else fmt["wrap"]
            if j == 1 and row_values[0] == "Recognized success share" and isinstance(v, float):
                target = fmt["pct"]
            _write_safe(summary, i, j, v, target)
        summary.set_row(i, 30)
    summary.autofilter(3, 0, 3 + len(summary_rows), 2)

    key = _init_sheet(wb, "Key_Insights", ["Finding", "Calculated evidence", "Interpretation / caveat", "Suggested follow-up", "Supporting tab"],
                      [43, 69, 84, 79, 25], "INSIGHTS  |  EVIDENCE BEFORE ASSERTIONS")
    sheet_names.append("Key_Insights")
    for i, item in enumerate(result["insights"], 4):
        for j, k in enumerate(["title", "evidence", "interpretation", "recommendation", "reference"]):
            _write_safe(key, i, j, item[k], fmt["wrap"])
        key.set_row(i, 58)
    key.autofilter(3, 0, 3 + len(result["insights"]), 4)

    dq = _init_sheet(wb, "Data_Quality", ["Source field", "Data role", "Nonmissing", "Missing", "Missing %", "Invalid numeric", "Invalid date"],
                     [32, 23, 19, 17, 17, 20, 20], "DATA QUALITY  |  COMPLETE RECORD PROFILING")
    sheet_names.append("Data_Quality")
    for i, (h, p) in enumerate(result["columns"].items(), 4):
        vals = [h, p["data_type"], p["nonmissing"], p["missing"], p["missing_pct"] / 100, p["invalid_numeric"], p["invalid_date"]]
        for j, val in enumerate(vals):
            _write_safe(dq, i, j, val, fmt["pct"] if j == 4 else fmt["int"] if j in [2,3,5,6] else fmt["text"])
    if result["headers"]:
        dq.conditional_format(4, 4, 3 + len(result["headers"]), 4,
                              {"type": "3_color_scale", "min_color": "#DCFCE7", "mid_color": "#FEF9C3", "max_color": "#FECACA"})
        dq.autofilter(3, 0, 3 + len(result["headers"]), 6)
    dq.write_string(len(result["headers"]) + 6, 0, "Exact duplicate rows", fmt["text"])
    dq.write_number(len(result["headers"]) + 6, 2, result["quality"]["exact_duplicate_rows"], fmt["int"])

    stat_cols = ["Numeric field", "Count", "Missing", "Sum", "Mean", "Std. dev.", "Min", "Q1*", "Median*", "Q3*", "Max", "Quantile sample", "Note"]
    stat = _init_sheet(wb, "Descriptive_Statistics", stat_cols,
                       [32, 14, 14, 20, 18, 19, 18, 18, 18, 18, 18, 18, 56],
                       "DESCRIPTIVE STATISTICS  |  DISTRIBUTIONS & SPREAD")
    sheet_names.append("Descriptive_Statistics")
    numeric_names = [h for h in result["headers"] if "stats" in result["columns"][h]]
    for i, h in enumerate(numeric_names, 4):
        info = result["columns"][h]
        s = info["stats"]
        vals = [h, s["count"], info["missing"], s["sum"], s["mean"], s["stddev"], s["min"], s["q1"], s["median"], s["q3"], s["max"],
                s["sample_size"], "*Approximate from reservoir sample" if s["quantile_is_sampled"] else "Exact: all valid values sampled"]
        for j, v in enumerate(vals):
            _write_safe(stat, i, j, v, fmt["text"] if j in (0,12) else fmt["int"] if j in (1,2,11) else fmt["num"])
    stat.autofilter(3, 0, max(4, 3 + len(numeric_names)), 12)

    category_sheet = None
    shown_categories = []
    if roles["group"] and result["categories"]:
        category_sheet = _init_sheet(wb, "Category_Analysis",
                                     [roles["group"], "Records", "Total value", "Valid numeric", "Avg valid value", "Row share", "Known success", "Known failure", "Recognized success %"],
                                     [39, 18, 22, 18, 20, 17, 18, 18, 22],
                                     f"CATEGORY ANALYSIS  |  {roles['group']}")
        sheet_names.append("Category_Analysis")
        sorted_cats = result["categories"]
        shown_categories = sorted_cats[:40]
        if len(sorted_cats) > 40:
            remaining = sorted_cats[40:]
            rest = {"name": "(Other categories — aggregated)", **{field: sum(v[field] for v in remaining)
                    for field in ("count", "amount", "valid_amount", "success", "failure")}}
            shown_categories.append(rest)
        for i, cat in enumerate(shown_categories, 4):
            excel_r = i + 1
            values = [cat["name"], cat["count"], cat["amount"], cat["valid_amount"],
                      (cat["amount"] / cat["valid_amount"]) if cat["valid_amount"] else None,
                      cat["count"] / rc if rc else 0, cat["success"], cat["failure"],
                      cat["success"] / (cat["success"] + cat["failure"]) if cat["success"] + cat["failure"] else None]
            for j, v in enumerate(values):
                if j == 4 and v is not None:
                    category_sheet.write_formula(i, j, f"=IFERROR(C{excel_r}/D{excel_r},0)", fmt["num"], v)
                elif j == 5:
                    category_sheet.write_formula(i, j, f"=IFERROR(B{excel_r}/Source_Metadata!$B$7,0)", fmt["pct"], v)
                elif j == 8 and v is not None:
                    category_sheet.write_formula(i, j, f"=IFERROR(G{excel_r}/(G{excel_r}+H{excel_r}),0)", fmt["pct"], v)
                else:
                    _write_safe(category_sheet, i, j, v, fmt["text"] if j == 0 else fmt["int"] if j in (1,3,6,7) else fmt["num"])
        category_sheet.autofilter(3, 0, 3 + len(shown_categories), 8)
        category_sheet.conditional_format(4, 5, 3 + len(shown_categories), 5,
                                          {"type": "data_bar", "bar_color": "#61A6E7"})

    trend_sheet = None
    if result["months"]:
        trend_sheet = _init_sheet(wb, "Trend_Analysis", ["Month", "Record count", "Total value", "Avg valid value", "MoM volume change", "Known successes", "Known failures", "Valid values"],
                                  [20, 22, 23, 23, 23, 22, 22, 18],
                                  "TIME SERIES  |  CALENDAR-MONTH COMPARISONS")
        sheet_names.append("Trend_Analysis")
        previous = None
        for i, row in enumerate(result["months"], 4):
            e = i + 1
            avg = row["amount"] / row["valid_amount"] if row["valid_amount"] else None
            _write_safe(trend_sheet, i, 0, row["month"], fmt["text"])
            _write_safe(trend_sheet, i, 1, row["count"], fmt["int"])
            _write_safe(trend_sheet, i, 2, row["amount"], fmt["num"])
            if avg is not None:
                trend_sheet.write_formula(i, 3, f"=IFERROR(C{e}/H{e},0)", fmt["num"], avg)
            if previous and previous["count"]:
                cached_change = row["count"] / previous["count"] - 1
                trend_sheet.write_formula(i, 4, f"=IFERROR(B{e}/B{e-1}-1,0)", fmt["pct"], cached_change)
            _write_safe(trend_sheet, i, 5, row["success"], fmt["int"])
            _write_safe(trend_sheet, i, 6, row["failure"], fmt["int"])
            _write_safe(trend_sheet, i, 7, row["valid_amount"], fmt["int"])
            previous = row
        trend_sheet.autofilter(3, 0, 3 + len(result["months"]), 7)
        trend_sheet.conditional_format(4, 4, 3 + len(result["months"]), 4,
                                       {"type": "3_color_scale", "min_color": "#F9B5BC",
                                        "mid_color": "#FFFFFF", "max_color": "#B1E4D0"})

    if roles["status"]:
        status_ws = _init_sheet(wb, "Status_Analysis", ["Observed status", "Records", "Share of all records", "Interpretation"],
                                [35, 18, 22, 93], "STATUS BREAKDOWN  |  TOP LABELS + REMAINDER")
        sheet_names.append("Status_Analysis")
        for i, (status_label, status_count) in enumerate(result["status_counts"].items(), 4):
            _write_safe(status_ws, i, 0, status_label, fmt["text"])
            _write_safe(status_ws, i, 1, status_count, fmt["int"])
            status_ws.write_formula(i, 2, f"=IFERROR(B{i+1}/Source_Metadata!$B$7,0)", fmt["pct"], status_count / rc if rc else 0)
            _write_safe(status_ws, i, 3, "Recorded raw status; unknown or unmapped statuses are not assumed to represent failure.", fmt["wrap"])
        status_ws.autofilter(3, 0, 3 + len(result["status_counts"]), 3)

    if result["histogram"]:
        histogram = _init_sheet(wb, "Distribution", ["Value interval (sample)", "Frequency", "Meaning"],
                                [40, 19, 96], "DISTRIBUTION  |  SAMPLED NUMERIC HISTOGRAM")
        sheet_names.append("Distribution")
        for i, item in enumerate(result["histogram"], 4):
            _write_safe(histogram, i, 0, item["bin"], fmt["text"])
            _write_safe(histogram, i, 1, item["count"], fmt["int"])
            _write_safe(histogram, i, 2, f"Sample-only distribution of {result['histogram_column']}; not exact full-dataset bin frequencies.", fmt["wrap"])

    anomalies = _init_sheet(wb, "Anomaly_Analysis", ["Flag", "Affected period or field", "Observed value", "Interpretation"],
                            [35, 40, 24, 100], "REVIEW FLAGS  |  NOT AUTOMATIC ERRORS")
    sheet_names.append("Anomaly_Analysis")
    if not result["anomalies"]:
        _write_safe(anomalies, 4, 0, "No rule-based anomalies detected", fmt["text"])
        _write_safe(anomalies, 4, 3, "Absence of a rule-based flag does not prove the data is anomaly-free.", fmt["wrap"])
    for i, item in enumerate(result["anomalies"], 4):
        for j, field in enumerate(("type", "detail", "value", "note")):
            _write_safe(anomalies, i, j, item[field], fmt["wrap"])
        anomalies.set_row(i, 34)

    dictionary = _init_sheet(wb, "Data_Dictionary", ["Field name", "Detected type", "Assigned role", "Description / detection policy"],
                             [36, 26, 28, 95], "DATA DICTIONARY  |  INFERRED STRUCTURE")
    sheet_names.append("Data_Dictionary")
    for i, h in enumerate(result["headers"], 4):
        assigned = ", ".join(k for k in ("date", "value", "group", "status") if roles[k] == h) or ("numeric measure" if h in roles["numeric"] else "unassigned")
        values = (h, result["columns"][h]["data_type"], assigned,
                  "Inference from column name and initial data sample; verify semantic meaning with source owner.")
        for j, v in enumerate(values):
            _write_safe(dictionary, i, j, v, fmt["wrap"])

    meth = _init_sheet(wb, "Methodology", ["Method", "Definition / limitation"], [36, 118],
                       "METHODOLOGY  |  REPRODUCIBILITY & LIMITATIONS")
    sheet_names.append("Methodology")
    method_lines = [
        ("Exact quantities", result["methodology"]["exact"]),
        ("Approximate quantities", result["methodology"]["sampled"]),
        ("Sample size per numeric field", result["methodology"]["sample_size_per_numeric"]),
        ("Period aggregation", "Calendar month based on the identified date column; missing and invalid dates excluded from trends. Current/first months may be partial."),
        ("Rate denominator", "Recognized successes / (recognized successes + recognized failures); unknown/missing statuses excluded."),
        ("Percentage change", "(current month count / prior month count) - 1; missing periods have no synthetic observations."),
        ("Null handling", result["methodology"]["missing_policy"]),
        ("Exact duplicates", "Repeated complete-row content, not verified repeated business events; full-row hash checks."),
        ("Input formula handling", "Reads cached Excel formula results only; formulas are not recalculated or macros executed."),
        ("Category display", "Top 40 groups plus aggregated remainder; aggregate values still reconcile with complete source groups."),
        ("Source preview", "First N nonblank source rows only; NOT an exhaustive source-data export."),
        ("Causality policy", "Descriptive associations and changes do not establish business or scientific causality."),
        ("Currency policy", "Value units are not inferred from headers alone. Confirm currency and scale before making financial claims."),
        ("Success values", ", ".join(result["methodology"]["status_success_codes"])),
        ("Failure values", ", ".join(result["methodology"]["status_failure_codes"])),
    ] + [(f"Warning {i}", msg) for i, msg in enumerate(result["warnings"], 1)]
    for i, (label, value) in enumerate(method_lines, 4):
        _write_safe(meth, i, 0, label, fmt["text"])
        _write_safe(meth, i, 1, value, fmt["wrap"])
        meth.set_row(i, 30)

    valid = _init_sheet(wb, "Validation_Status", ["Check", "Status", "Detail"], [48, 21, 110],
                        "VALIDATION  |  COMPLETENESS & INTERNAL RECONCILIATION")
    sheet_names.append("Validation_Status")
    for i, (test, passed) in enumerate(result["checks"].items(), 4):
        _write_safe(valid, i, 0, test.replace("_", " "), fmt["text"])
        _write_safe(valid, i, 1, "PASS" if passed else "FAIL", fmt["text"])
        _write_safe(valid, i, 2, "Full parsed input, no aggregation sampling" if passed else "Review source and selected fields", fmt["text"])
    valid.conditional_format(4, 1, 3 + len(result["checks"]), 1,
                             {"type": "text", "criteria": "containing", "value": "FAIL",
                              "format": wb.add_format({"font_color": RED, "bg_color": "#FCE7E9"})})

    preview = _init_sheet(wb, "Input_Preview", result["headers"], [24] * len(result["headers"]),
                          f"INPUT PREVIEW  |  FIRST {len(result['preview']):,} RECORDS ONLY")
    sheet_names.append("Input_Preview")
    preview.freeze_panes(4, 0)
    for i, row in enumerate(result["preview"], 4):
        for j, value in enumerate(row):
            _write_safe(preview, i, j, value, fmt["text"] if not isinstance(value, (float, int)) else fmt["num"])
    if result["preview"]:
        preview.autofilter(3, 0, 3 + len(result["preview"]), len(result["headers"]) - 1)

    # Native Excel charts. All series refer to summary tables in this workbook.
    def chart_base(chart_type: str, subtype=None):
        chart = wb.add_chart({"type": chart_type, **({"subtype": subtype} if subtype else {})})
        chart.set_chartarea({"fill": {"color": "#FFFFFF"}, "border": {"color": BORDER}})
        chart.set_plotarea({"fill": {"color": "#FFFFFF"}, "border": {"none": True}})
        chart.set_title({"name_font": {"name": "Aptos Display", "size": 12, "bold": True, "color": NAVY}})
        chart.set_legend({"none": True})
        chart.set_x_axis({"name_font": {"name": "Aptos", "size": 10}, "num_font": {"size": 9}})
        chart.set_y_axis({"major_gridlines": {"visible": True, "line": {"color": "#E9EEF4"}},
                          "num_font": {"size": 9}})
        chart.set_size({"width": 655, "height": 300})
        return chart

    if trend_sheet:
        end = 4 + len(result["months"])
        c = chart_base("line")
        c.set_title({"name": "Monthly records  |  trend"})
        c.add_series({"name": "Records", "categories": ["Trend_Analysis", 4, 0, end-1, 0],
                      "values": ["Trend_Analysis", 4, 1, end-1, 1], "line": {"color": BLUE, "width": 2.5},
                      "marker": {"type": "circle", "size": 4, "border": {"color": BLUE}, "fill": {"color": BLUE}}})
        dash.insert_chart("A17", c, {"x_offset": 7, "y_offset": 5})
        chart_count += 1
        if roles["value"]:
            c = chart_base("column")
            c.set_title({"name": "Monthly selected value  |  total"})
            c.add_series({"name": "Total value", "categories": ["Trend_Analysis", 4, 0, end-1, 0],
                          "values": ["Trend_Analysis", 4, 2, end-1, 2], "fill": {"color": "#2698B8"},
                          "border": {"none": True}})
            dash.insert_chart("I17", c, {"x_offset": 7, "y_offset": 5})
            chart_count += 1
    if category_sheet:
        count = min(12, len(result["categories"]))
        c = chart_base("bar")
        c.set_title({"name": f"Top {count} categories  |  record volume"})
        c.add_series({"name": "Records", "categories": ["Category_Analysis", 4, 0, 3+count, 0],
                      "values": ["Category_Analysis", 4, 1, 3+count, 1], "fill": {"color": BLUE},
                      "border": {"none": True}})
        c.set_y_axis({"reverse": True, "num_font": {"size": 9}})
        dash.insert_chart("A36", c, {"x_offset": 7, "y_offset": 4})
        chart_count += 1
    if result["histogram"]:
        c = chart_base("column")
        c.set_title({"name": f"Distribution sample  |  {result['histogram_column']}"})
        c.add_series({"name": "Sample frequency", "categories": ["Distribution", 4, 0, 13, 0],
                      "values": ["Distribution", 4, 1, 13, 1], "fill": {"color": "#60B5AC"},
                      "border": {"none": True}})
        c.set_x_axis({"num_font": {"rotation": -35, "size": 8}})
        dash.insert_chart("I36", c, {"x_offset": 7, "y_offset": 4})
        chart_count += 1
    # Navigation links kept below the dashboard findings, not over chart areas.
    dash.write_string("B15", "DRILL DOWN TO DETAIL", wb.add_format({"bold": True, "font_color": NAVY2}))
    for i, dest in enumerate(("Key_Insights", "Data_Quality", "Descriptive_Statistics", "Methodology")):
        dash.write_url(14, 5+i*3, f"internal:'{dest}'!A1", fmt["link"], dest.replace("_", " "))
    dash.set_footer("Generated automatically | Verify definitions against source context | Page &P of &N")
    wb.close()
    validation = verify_file(path, required_sheets=sheet_names, minimum_charts=chart_count)
    return {"path": str(path.resolve()), "charts": chart_count, "sheets": sheet_names, "validation": validation}


def verify_file(path: str | Path, *, required_sheets: list[str], minimum_charts: int) -> dict[str, Any]:
    checks: dict[str, bool] = {}
    with zipfile.ZipFile(path) as archive:
        checks["zip_integrity"] = archive.testzip() is None
        names = archive.namelist()
        checks["workbook_xml_present"] = "xl/workbook.xml" in names
        checks["all_worksheets_present"] = (sum(n.startswith("xl/worksheets/sheet") and n.endswith(".xml") for n in names)
                                            == len(required_sheets))
        checks["native_chart_xml_present"] = (sum(n.startswith("xl/charts/chart") and n.endswith(".xml") for n in names)
                                               >= minimum_charts)
        xml = archive.read("xl/workbook.xml").decode("utf-8")
        checks["expected_sheet_names"] = all(f'name="{s}"' in xml for s in required_sheets)
    return {"checks": checks, "passed": all(checks.values())}

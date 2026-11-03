#!/usr/bin/env python3
"""CLI for portable Excel/CSV analysis and professional workbook generation."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from analytics import analyze
from report import write_report


def _manifest_content(result: dict) -> dict:
    """Compact manifest; strip reservoir samples and raw preview from metadata JSON."""
    manifest = {k: v for k, v in result.items() if k not in ("preview", "columns")}
    columns = {}
    for name, item in result["columns"].items():
        c = dict(item)
        if "stats" in c:
            c["stats"] = {k: v for k, v in c["stats"].items() if k != "sample"}
        columns[name] = c
    manifest["columns"] = columns
    # Retain bounded chart summaries, never full raw category cardinality in the manifest.
    manifest["categories"] = result["categories"][:50]
    return manifest


def main(argv=None) -> int:
    cli = argparse.ArgumentParser(description="Analyze an Excel/CSV worksheet and create a chart-rich Excel report.")
    cli.add_argument("input", type=Path, help="Source .xlsx, .xlsm, .csv, or optional .xls")
    cli.add_argument("--output", type=Path, help="Destination workbook, defaults to <input>_Analysis_Report.xlsx")
    cli.add_argument("--sheet", help="Worksheet name, defaults to first nonempty worksheet")
    cli.add_argument("--mode", choices=("standard", "deep"), default="deep")
    cli.add_argument("--date-column")
    cli.add_argument("--value-column")
    cli.add_argument("--group-column")
    cli.add_argument("--status-column")
    cli.add_argument("--success-values", default="SUCCESS,SUCCEEDED,COMPLETED,PAID,APPROVED,CAPTURED")
    cli.add_argument("--failure-values", default="FAILED,FAILURE,DECLINED,REJECTED,ERROR,CANCELLED,CANCELED")
    cli.add_argument("--preview-rows", type=int, default=1000)
    cli.add_argument("--sample-size", type=int, default=10000)
    opts = cli.parse_args(argv)
    dest = opts.output or opts.input.with_name(opts.input.stem + "_Analysis_Report.xlsx")
    if dest.suffix.lower() != ".xlsx":
        cli.error("--output must end in .xlsx")
    if opts.preview_rows < 0 or opts.preview_rows > 5000:
        cli.error("--preview-rows must be between 0 and 5000")
    if opts.sample_size <= 0:
        cli.error("--sample-size must be positive")
    try:
        print(f"Analyzing: {opts.input}", flush=True)
        analyzed = analyze(opts.input, opts.sheet, opts.mode,
                           date_column=opts.date_column, value_column=opts.value_column,
                           group_column=opts.group_column, status_column=opts.status_column,
                           success_values=opts.success_values, failure_values=opts.failure_values,
                           preview_rows=opts.preview_rows, sample_size=opts.sample_size)
        report = write_report(analyzed, dest)
        manifest = _manifest_content(analyzed)
        manifest["output"] = report
        manifest_file = dest.with_suffix(".manifest.json")
        manifest_file.write_text(json.dumps(manifest, indent=2, ensure_ascii=False, default=str), encoding="utf-8")
        passed = all(analyzed["checks"].values()) and report["validation"]["passed"]
        print(f"Records processed: {analyzed['row_count']:,}")
        print(f"Identified fields: {analyzed['roles']}")
        print(f"Native Excel charts: {report['charts']}; worksheets: {len(report['sheets'])}")
        print(f"Validation: {'PASS' if passed else 'FAIL'}")
        for warning in analyzed["warnings"]:
            print(f"WARNING: {warning}")
        print(f"Workbook: {report['path']}")
        print(f"Manifest: {manifest_file.resolve()}")
        return 0 if passed else 2
    except Exception as exc:
        print(f"ERROR: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())

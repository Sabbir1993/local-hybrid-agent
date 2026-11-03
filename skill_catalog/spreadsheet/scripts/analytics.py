"""Streaming profiling and descriptive analysis for Excel/CSV input.

All original records are read-only. Exact aggregates cover every parsed row.
Quantiles/histograms use reproducible bounded reservoir samples.
"""
from __future__ import annotations

import csv
import hashlib
import math
import random
import re
import sqlite3
import tempfile
from collections import Counter, defaultdict
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterator


def missing(v: Any) -> bool:
    return v is None or (isinstance(v, str) and v.strip() == "")


def as_number(v: Any) -> float | None:
    if isinstance(v, bool) or missing(v) or isinstance(v, (date, datetime)):
        return None
    try:
        if isinstance(v, str):
            s = v.strip().replace(",", "").replace("৳", "").replace("$", "").replace("€", "")
            neg = s.startswith("(") and s.endswith(")")
            if neg:
                s = s[1:-1]
            if s.endswith("%"):
                f = float(s[:-1]) / 100
            else:
                f = float(s)
            if neg:
                f = -f
        else:
            f = float(v)
        return f if math.isfinite(f) else None
    except (TypeError, ValueError, OverflowError):
        return None


def as_date(v: Any, allow_serial: bool = False) -> date | None:
    if isinstance(v, datetime):
        return v.date()
    if isinstance(v, date):
        return v
    if allow_serial and isinstance(v, (float, int)) and not isinstance(v, bool) and 20000 <= v <= 80000:
        return date(1899, 12, 30) + timedelta(days=int(v))
    if not isinstance(v, str) or not v.strip():
        return None
    s = v.strip()
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00")).date()
    except ValueError:
        pass
    for fmt in ("%m/%d/%Y", "%d/%m/%Y", "%Y/%m/%d", "%d-%b-%Y", "%d-%m-%Y", "%Y%m%d"):
        try:
            return datetime.strptime(s, fmt).date()
        except ValueError:
            continue
    return None


def safe_header(v: Any, index: int) -> str:
    value = str(v).strip() if v is not None else ""
    return (value or f"Column_{index + 1}")[:120]


class InputReader:
    def __init__(self, path: str | Path, sheet: str | None = None):
        self.path = Path(path)
        if not self.path.is_file():
            raise FileNotFoundError(f"Source file not found: {self.path}")
        self.sheet = sheet
        self.headers: list[str] = []
        self.sheet_name: str = ""
        self.sheet_names: list[str] = []
        self.source_type = self.path.suffix.lower()
        if self.source_type not in {".csv", ".xlsx", ".xlsm", ".xls"}:
            raise ValueError("Supported inputs: .csv, .xlsx, .xlsm; .xls with optional python-calamine")

    def _raw_rows(self) -> Iterator[list[Any]]:
        ext = self.source_type
        if ext == ".csv":
            self.sheet_name = "CSV"
            self.sheet_names = ["CSV"]
            with self.path.open("r", encoding="utf-8-sig", newline="", errors="replace") as f:
                yield from csv.reader(f)
        elif ext in {".xlsx", ".xlsm"}:
            from openpyxl import load_workbook
            wb = load_workbook(self.path, read_only=True, data_only=True, keep_links=False)
            try:
                self.sheet_names = list(wb.sheetnames)
                if self.sheet and self.sheet not in wb.sheetnames:
                    raise ValueError(f"Worksheet {self.sheet!r} not found. Available: {wb.sheetnames}")
                if self.sheet:
                    ws = wb[self.sheet]
                else:
                    ws = next((w for w in wb.worksheets if w.max_row > 0 and w.max_column > 0), wb.worksheets[0])
                self.sheet_name = ws.title
                for row in ws.iter_rows(values_only=True):
                    yield list(row)
            finally:
                wb.close()
        else:
            try:
                from python_calamine import CalamineWorkbook
            except ImportError as e:
                raise RuntimeError(".xls requires optional dependency: pip install python-calamine") from e
            wb = CalamineWorkbook.from_path(str(self.path))
            self.sheet_names = list(wb.sheet_names)
            selected = self.sheet or (self.sheet_names[0] if self.sheet_names else None)
            if selected not in self.sheet_names:
                raise ValueError(f"Worksheet {selected!r} not found")
            self.sheet_name = str(selected)
            # Calamine currently exposes a materialized row array for legacy workbooks.
            for row in wb.get_sheet_by_name(selected).to_python():
                yield list(row)

    def rows(self) -> Iterator[dict[str, Any]]:
        it = iter(self._raw_rows())
        header_row = None
        for row in it:
            if any(not missing(v) for v in row):
                header_row = row
                break
        if header_row is None:
            raise ValueError("Worksheet has no nonempty rows")
        seen: dict[str, int] = {}
        headers = []
        for i, h in enumerate(header_row):
            name = safe_header(h, i)
            key = name.lower()
            seen[key] = seen.get(key, 0) + 1
            if seen[key] > 1:
                name = f"{name}_{seen[key]}"
            headers.append(name)
        self.headers = headers
        for raw in it:
            if not any(not missing(v) for v in raw):
                continue  # skip physically blank worksheet rows
            yield {h: (raw[i] if i < len(raw) else None) for i, h in enumerate(headers)}


def choose_columns(headers: list[str], preview: list[dict[str, Any]], overrides: dict[str, str | None]) -> dict[str, Any]:
    if not headers:
        raise ValueError("No columns found")
    lookup = {h.lower(): h for h in headers}
    def specified(role: str) -> str | None:
        v = overrides.get(role)
        if v is None:
            return None
        if v.lower() not in lookup:
            raise ValueError(f"{role} column {v!r} does not exist. Available: {headers}")
        return lookup[v.lower()]

    names = {h: re.sub(r"[^a-z0-9]+", "_", h.lower()).strip("_") for h in headers}
    ratios = {}
    unique_counts = {}
    for h in headers:
        present = [r[h] for r in preview if not missing(r[h])]
        ratios[h] = (sum(as_number(v) is not None for v in present) / len(present)) if present else 0
        unique_counts[h] = len({str(v) for v in present[:500]})
    id_like = lambda n: n == "id" or n.endswith("_id") or n in {"index", "row_number", "account_number", "phone", "zip", "postal_code"}
    numeric = [h for h in headers if ratios[h] >= 0.80 and not id_like(names[h])]
    date_candidates = [h for h in headers if any(w in names[h] for w in ("date", "time", "timestamp", "created", "posted", "day"))]
    def date_score(h: str) -> float:
        vals = [r[h] for r in preview if not missing(r[h])][:60]
        return sum(as_date(v, True) is not None for v in vals) / len(vals) if vals else 0
    inferred_date = next((h for h in date_candidates if date_score(h) >= 0.65), None)
    if inferred_date is None:
        inferred_date = next((h for h in headers if date_score(h) >= 0.9 and any(isinstance(r[h], (date, datetime)) for r in preview)), None)

    def find_ranked(parts: list[str], allowed: list[str] | None = None) -> str | None:
        available = allowed if allowed is not None else headers
        for phrase in parts:
            for h in available:
                if names[h] == phrase:
                    return h
        for phrase in parts:
            for h in available:
                if phrase in names[h]:
                    return h
        return None

    inferred_value = find_ranked(["transaction_amount", "amount", "revenue", "sales", "payment_value", "total_value", "total_amount", "price", "cost", "volume"], numeric)
    if inferred_value is None and numeric:
        inferred_value = numeric[0]

    inferred_status = find_ranked(["transaction_status", "payment_status", "status", "outcome", "result"])
    categorical = [h for h in headers if h not in numeric and h != inferred_date and h != inferred_status and not id_like(names[h]) and unique_counts[h] > 1]
    inferred_group = find_ranked(["payment_method", "method", "category", "product_category", "channel", "region", "country", "merchant", "segment", "department", "type", "product"], categorical)
    if inferred_group is None:
        inferred_group = next((h for h in categorical if unique_counts[h] <= max(5, len(preview) // 2)), None)

    roles = {
        "date": specified("date") or inferred_date,
        "value": specified("value") or inferred_value,
        "group": specified("group") or inferred_group,
        "status": specified("status") or inferred_status,
        "numeric": numeric[:30],
        "id_columns": [h for h in headers if id_like(names[h])],
    }
    if roles["value"] and roles["value"] not in roles["numeric"]:
        roles["numeric"] = [roles["value"]] + roles["numeric"]
    return roles


class NumberStats:
    def __init__(self, sample_size: int = 10000, seed: int = 919):
        self.n = 0
        self.total = 0.0
        self.mean = 0.0
        self.m2 = 0.0
        self.low = math.inf
        self.high = -math.inf
        self.sample: list[float] = []
        self.max_sample = max(100, sample_size)
        self.rng = random.Random(seed)

    def add(self, x: float):
        self.n += 1
        self.total += x
        delta = x - self.mean
        self.mean += delta / self.n
        self.m2 += delta * (x - self.mean)
        self.low = min(self.low, x)
        self.high = max(self.high, x)
        if len(self.sample) < self.max_sample:
            self.sample.append(x)
        else:
            j = self.rng.randrange(self.n)
            if j < self.max_sample:
                self.sample[j] = x

    def output(self):
        vals = sorted(self.sample)
        def q(p):
            if not vals:
                return None
            x = p * (len(vals) - 1)
            lo = math.floor(x)
            hi = math.ceil(x)
            return vals[lo] + (vals[hi] - vals[lo]) * (x - lo)
        return {"count": self.n, "sum": self.total, "mean": self.mean if self.n else None,
                "min": self.low if self.n else None, "max": self.high if self.n else None,
                "stddev": math.sqrt(self.m2 / (self.n - 1)) if self.n > 1 else None,
                "q1": q(.25), "median": q(.5), "q3": q(.75),
                "sample_size": len(vals), "quantile_is_sampled": self.n > len(vals),
                "sample": vals}


def _count_group(agg: dict, key: str, value: float | None, state: str):
    slot = agg[key]
    slot["count"] += 1
    if value is not None:
        slot["amount"] += value
        slot["valid_amount"] += 1
    if state == "success":
        slot["success"] += 1
    elif state == "failure":
        slot["failure"] += 1


def _new_group() -> dict[str, Any]:
    return {"count": 0, "amount": 0.0, "valid_amount": 0, "success": 0, "failure": 0}


def _insights(result: dict[str, Any]) -> list[dict[str, str]]:
    ins: list[dict[str, str]] = []
    rows, roles = result["row_count"], result["roles"]
    def add(title: str, evidence: str, interpretation: str, action: str, reference: str):
        ins.append({"title": title, "evidence": evidence, "interpretation": interpretation,
                    "recommendation": action, "reference": reference})

    categories = result["categories"]
    if len(categories) >= 2:
        first = categories[0]
        share = first["count"] / rows * 100 if rows else 0
        add("Leading category by record volume",
            f"{first['name']}: {first['count']:,} of {rows:,} rows ({share:.1f}%).",
            f"The selected grouping field is {roles['group']}. The leading category accounts for {share:.1f}% of observed records.",
            "Review whether resource allocation and reporting focus match this concentration.", "Category_Analysis")
    trends = result["months"]
    if len(trends) >= 2:
        a, b = trends[-2], trends[-1]
        if a["count"]:
            pct = (b["count"] / a["count"] - 1) * 100
            add("Latest monthly record change",
                f"{a['month']}: {a['count']:,}; {b['month']}: {b['count']:,}; change {pct:+.1f}%.",
                "This is an observed month-over-month change, not a causal explanation. Partial calendar months may affect comparison.",
                "Compare like-for-like reporting periods before attributing the change to operations.", "Trend_Analysis")
    if roles["value"] and result["value_valid_count"]:
        add("Observed aggregate value",
            f"{roles['value']} sums to {result['value_sum']:,.2f} across {result['value_valid_count']:,} valid records.",
            f"The sum excludes {rows - result['value_valid_count']:,} missing or nonnumeric values. Units/currency were not inferred with certainty.",
            "Confirm the financial meaning and currency of the value field before publication.", "Executive_Summary")
    quality = result["quality"]
    if quality["exact_duplicate_rows"]:
        add("Exact duplicate rows flagged",
            f"{quality['exact_duplicate_rows']:,} of {rows:,} rows ({quality['exact_duplicate_rows']/rows*100:.2f}%) match an earlier complete row.",
            "Whole-row repetition is not necessarily a duplicated transaction; distinct business identifiers must be checked.",
            "Investigate flagged records using business identifiers before removing anything.", "Data_Quality")
    missing_fields = sorted(((h, p["missing"]) for h, p in result["columns"].items()), key=lambda x: -x[1])
    if missing_fields and missing_fields[0][1] > 0:
        h, count = missing_fields[0]
        add("Largest missing-data exposure",
            f"{h}: {count:,} missing entries ({count/rows*100:.1f}% of rows).",
            "Missingness can bias segmentation and weighted summaries depending on the affected variable.",
            "Assess whether the missing-data pattern is systematic before imputing values.", "Data_Quality")
    if result["status"]["recognized"]:
        s = result["status"]
        pct = 100 * s["success"] / s["recognized"]
        add("Recognized-outcome success share",
            f"{s['success']:,} successes / {s['recognized']:,} recognized outcomes = {pct:.2f}%.",
            f"{s['unknown']:,} nonempty statuses were not matched to the selected success/failure vocabulary and are excluded.",
            "Review status mapping and compare success share across periods and segments.", "Executive_Dashboard")
    if not ins:
        add("Data coverage", f"{rows:,} input rows and {len(result['headers'])} source columns processed.",
            "No strong business finding was supportable without more clearly defined metrics or dimensions.",
            "Provide a reporting objective or specify date/value/category columns.", "Source_Metadata")
    return ins[:10]


def analyze(path: str | Path, sheet: str | None = None, mode: str = "deep", *,
            date_column: str | None = None, value_column: str | None = None,
            group_column: str | None = None, status_column: str | None = None,
            success_values: str = "SUCCESS,SUCCEEDED,COMPLETED,PAID,APPROVED,CAPTURED",
            failure_values: str = "FAILED,FAILURE,DECLINED,REJECTED,ERROR,CANCELLED,CANCELED",
            preview_rows: int = 1000, sample_size: int = 10000) -> dict[str, Any]:
    source = InputReader(path, sheet)
    initial = []
    iterator = source.rows()
    for i, row in enumerate(iterator):
        initial.append(row)
        if i >= 199:
            break
    if not initial:
        raise ValueError("The selected source contains no data rows")
    headers = source.headers[:]
    roles = choose_columns(headers, initial, {"date": date_column, "value": value_column,
                                              "group": group_column, "status": status_column})
    success_codes = {s.strip().upper() for s in success_values.split(",") if s.strip()}
    failure_codes = {s.strip().upper() for s in failure_values.split(",") if s.strip()}
    overlap = success_codes & failure_codes
    if overlap:
        raise ValueError(f"Conflicting success and failure labels: {sorted(overlap)}")
    sample_size = min(max(100, sample_size), 50000 if mode == "deep" else 5000)
    stats = {h: NumberStats(sample_size, 910 + i) for i, h in enumerate(roles["numeric"])}
    column_info = {h: {"missing": 0, "nonmissing": 0, "invalid_numeric": 0, "invalid_date": 0} for h in headers}
    categories = defaultdict(_new_group)
    months = defaultdict(_new_group)
    status = {"success": 0, "failure": 0, "unknown": 0, "missing": 0, "recognized": 0}
    status_detail: Counter = Counter()
    examples: list[list[Any]] = []
    row_count = 0
    valid_amount_count = 0
    amount_sum = 0.0
    dup_count = 0
    warnings = []

    with tempfile.TemporaryDirectory(prefix="analytics_dup_") as temp:
        con = sqlite3.connect(str(Path(temp) / "digests.db"))
        con.execute("PRAGMA journal_mode=OFF")
        con.execute("PRAGMA synchronous=OFF")
        con.execute("CREATE TABLE seen (digest BLOB PRIMARY KEY) WITHOUT ROWID")
        try:
            for row in source.rows():
                row_count += 1
                if len(examples) < max(0, preview_rows):
                    examples.append([row[h] for h in headers])
                canonical = "\x1f".join(f"{type(row[h]).__name__}:{row[h]!s}" for h in headers)
                digest = hashlib.blake2b(canonical.encode("utf-8", errors="replace"), digest_size=16).digest()
                try:
                    con.execute("INSERT INTO seen VALUES (?)", (digest,))
                except sqlite3.IntegrityError:
                    dup_count += 1
                for h in headers:
                    if missing(row[h]):
                        column_info[h]["missing"] += 1
                    else:
                        column_info[h]["nonmissing"] += 1
                for h, st in stats.items():
                    if not missing(row[h]):
                        num = as_number(row[h])
                        if num is None:
                            column_info[h]["invalid_numeric"] += 1
                        else:
                            st.add(num)
                val = as_number(row[roles["value"]]) if roles["value"] else None
                if val is not None:
                    valid_amount_count += 1
                    amount_sum += val
                d = as_date(row[roles["date"]], True) if roles["date"] else None
                if roles["date"] and d is None and not missing(row[roles["date"]]):
                    column_info[roles["date"]]["invalid_date"] += 1
                code = str(row[roles["status"]]).strip().upper() if roles["status"] and not missing(row[roles["status"]]) else ""
                state = "unknown"
                if roles["status"]:
                    if not code:
                        status["missing"] += 1
                    else:
                        status_detail[code] += 1
                        if code in success_codes:
                            state = "success"
                            status["success"] += 1
                            status["recognized"] += 1
                        elif code in failure_codes:
                            state = "failure"
                            status["failure"] += 1
                            status["recognized"] += 1
                        else:
                            status["unknown"] += 1
                if roles["group"]:
                    group = str(row[roles["group"]]).strip() if not missing(row[roles["group"]]) else "(Missing)"
                    if len(categories) >= 100000 and group not in categories:
                        group = "(Other high-cardinality values)"
                    _count_group(categories, group[:180], val, state)
                if d:
                    _count_group(months, d.strftime("%Y-%m"), val, state)
                if row_count % 10000 == 0:
                    con.commit()
            con.commit()
        finally:
            con.close()

    if roles["value"] and not valid_amount_count:
        warnings.append(f"Selected value column {roles['value']!r} has no valid numeric values.")
    if roles["date"] and not months:
        warnings.append(f"Selected date column {roles['date']!r} has no parsable dates.")
    if roles["status"] and not status["recognized"]:
        warnings.append("No recognized success/failure labels; success rate omitted. Check --success-values and --failure-values.")
    if roles["status"] and status["unknown"]:
        warnings.append(f"{status['unknown']:,} statuses not recognized; excluded from known-outcome success rate.")
    if len(categories) >= 100000:
        warnings.append("Category cardinality exceeds 100,000; extra labels pooled into an Other bucket.")
    if row_count > preview_rows:
        warnings.append(f"Input_Preview contains only the first {preview_rows:,} rows; exact analysis covers all {row_count:,} data rows.")
    if any(st.n > len(st.sample) for st in stats.values()):
        warnings.append("Quantiles and distribution charts are approximate and based on reproducible reservoir samples.")
    if len(source.sheet_names) > 1:
        warnings.append(f"Only worksheet '{source.sheet_name}' was analyzed; other source worksheets were not merged.")

    column_results = {}
    for h in headers:
        col = dict(column_info[h])
        col["missing_pct"] = 100 * col["missing"] / row_count if row_count else 0
        col["data_type"] = "numeric" if h in stats else ("date" if h == roles["date"] else "categorical / text")
        if h in stats:
            col["stats"] = stats[h].output()
        column_results[h] = col

    top_categories = sorted((dict(name=k, **v) for k, v in categories.items()), key=lambda z: (-z["count"], z["name"]))
    sorted_months = [dict(month=k, **v) for k, v in sorted(months.items())]
    anomalies = []
    for m1, m2 in zip(sorted_months, sorted_months[1:]):
        if m1["count"] >= 20:
            relative = (m2["count"] / m1["count"] - 1) * 100
            if abs(relative) >= 30:
                anomalies.append({"type": "Monthly volume shift", "detail": f"{m1['month']} to {m2['month']}",
                                  "value": f"{relative:+.1f}%", "note": "Flag for review, not evidence of a cause; partial months may affect comparison."})
    for h, info in column_results.items():
        if "stats" in info:
            st = info["stats"]
            if st["sample_size"] >= 30 and st["q1"] is not None and st["q3"] is not None:
                iqr = st["q3"] - st["q1"]
                if iqr > 0:
                    lower, upper = st["q1"] - 1.5 * iqr, st["q3"] + 1.5 * iqr
                    sample_outliers = sum(v < lower or v > upper for v in st["sample"])
                    if sample_outliers:
                        anomalies.append({"type": "Potential sample outliers", "detail": h,
                                          "value": f"{sample_outliers:,}/{st['sample_size']:,} sampled",
                                          "note": "Tukey's 1.5xIQR rule on a bounded numeric sample; not a full-population count or proof of error."})
        if info["invalid_numeric"]:
            anomalies.append({"type": "Invalid numeric values", "detail": h, "value": str(info["invalid_numeric"]),
                              "note": "Nonempty values could not be interpreted as finite numbers."})
        if info["invalid_date"]:
            anomalies.append({"type": "Invalid dates", "detail": h, "value": str(info["invalid_date"]),
                              "note": "Nonempty date values could not be parsed consistently."})
    primary_numeric = roles["value"] or (roles["numeric"][0] if roles["numeric"] else None)
    histogram = []
    if primary_numeric:
        sample = column_results[primary_numeric]["stats"]["sample"]
        if len(sample) >= 10:
            low, high = sample[0], sample[-1]
            if high > low:
                bins = [0] * 10
                for v in sample:
                    j = min(9, int((v - low) / (high - low) * 10))
                    bins[j] += 1
                width = (high - low) / 10
                histogram = [{"bin": f"{low + i*width:,.2f}–{low +(i+1)*width:,.2f}",
                              "count": count} for i, count in enumerate(bins)]

    top_statuses = dict(status_detail.most_common(30))
    if len(status_detail) > 30:
        top_statuses["(Other status labels)"] = sum(status_detail.values()) - sum(top_statuses.values())

    result = {
        "source_file": str(Path(path).resolve()), "source_size": Path(path).stat().st_size,
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "source_sheet": source.sheet_name, "all_source_sheets": source.sheet_names,
        "headers": headers, "row_count": row_count, "roles": roles, "mode": mode,
        "quality": {"exact_duplicate_rows": dup_count, "duplicate_check": "exact complete-row hash; SHA-independent BLAKE2b-128"},
        "columns": column_results,
        "value_sum": amount_sum, "value_valid_count": valid_amount_count,
        "status": status, "status_counts": top_statuses,
        "categories": top_categories, "months": sorted_months,
        "anomalies": anomalies[:300], "histogram": histogram, "histogram_column": primary_numeric,
        "preview": examples,
        "warnings": warnings,
        "methodology": {"exact": "Full-row counts; finite-number sums; group/month counts and sums; Welford mean/stddev; min/max; duplicate hash checks",
                        "sampled": "Deterministic reservoir-based quartiles/medians and histogram; results are approximate above sample size",
                        "sample_size_per_numeric": sample_size,
                        "date_policy": "ISO and selected common date formats; serial dates for plausible Excel date columns",
                        "missing_policy": "Blank/null excluded from numeric aggregate and retained in source profiling",
                        "status_success_codes": sorted(success_codes), "status_failure_codes": sorted(failure_codes)},
    }
    result["insights"] = _insights(result)
    result["checks"] = {
        "column_nonmissing_reconciliation": all((v["missing"] + v["nonmissing"] == row_count) for v in column_results.values()),
        "group_count_reconciliation": (not roles["group"]) or sum(v["count"] for v in top_categories) == row_count,
        "value_count_reconciliation": (not roles["value"]) or (valid_amount_count == column_results[roles["value"]]["stats"]["count"]),
        "status_reconciliation": (not roles["status"]) or (status["recognized"] + status["unknown"] + status["missing"] == row_count),
    }
    return result

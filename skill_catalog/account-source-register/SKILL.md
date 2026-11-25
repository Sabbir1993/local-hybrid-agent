---
name: account-source-register
title: Account Source Register
description: Source ID and reliability-status model for research workbooks: Sources sheet layout, status legend, cross-check and validation rules.
triggers: source register, source ids, sources sheet, data reliability, status legend, citation workbook
category: sales
version: 1.0.0
---

# Account Source Register

Used by read_skill(account-intelligence-workbook) and read_skill(account-research).

## Source IDs
- Number sources in order of first use: `S1`, `S2`, ... One ID per document or page, not per publisher.
- `Sources` sheet columns: `Source ID | Title | Publisher | Date | URL | Reliability`. Only list pages actually fetched. Never invent a URL.
- Each fact row carries one or more Source IDs (`S3, S7`).

## Status legend (put it on the Cover)
| Status | Meaning | Colour |
|---|---|---|
| Verified | From the company's or regulator's own published document | green |
| Reported | From reputable press or an aggregator | amber |
| Estimated | Derived by the analyst; method stated in the row | yellow |
| Not disclosed | Searched, not publicly available | grey |
Apply with `highlight: Status = <value> <colour>` and `validate: Status in [Verified, Reported, Estimated, Not disclosed]`.

## Checks before delivery
1. Every Source ID used in a sheet exists on `Sources` (use a formula check cell: `=SUMPRODUCT(--ISNA(MATCH(<ids>, Sources!A:A, 0)))` should be 0, or verify while assembling).
2. Every row has a Status; Verified rows must point to a primary source.
3. Two-source rule for headline figures; discrepancies noted, not hidden.
4. Dates: `Date` and `As of` in ISO format (YYYY-MM-DD) or a clear period (FY2025, 9M-2026).
5. Show a data-coverage KPI on the Exec Summary: share of Verified rows and count of Not disclosed rows.

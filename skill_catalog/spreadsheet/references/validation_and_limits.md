# Validation and analytical limitations

## Validated behavior

- Unit tests cover number and date parsing, online mean/variance, exact aggregates, missing input values, status classification, and exact-row duplicate checks.
- Integration tests produce a real `.xlsx` with native charts, check its ZIP/package structure, confirm dashboard formulas reference source-derived figures, and inspect imported text for formula injection.
- A deterministic example `.xlsx` with 2,402 rows runs successfully through the CLI.
- A separate synthetic CSV dataset with 100,000 rows was processed successfully, including full-count and total reconciliation. This is a functional stress check, not a sustained throughput benchmark.

## Known constraints

1. Primary analysis covers one selected worksheet. Multiple independent sheets require separate analysis runs; do not infer cross-sheet joins automatically.
2. Formula-derived input data must have cached Excel values. Input formulas and macros are not executed.
3. Full-dataset sums, valid numeric counts, mean, min/max, group counts and totals are exact for parsed data; floating-point arithmetic may have standard floating-point rounding limitations.
4. Quartiles, medians and histograms on large data are estimated from a bounded deterministic reservoir sample. A sample may miss rare outliers.
5. The report omits full raw data, retaining a clearly labeled preview, so Excel's per-sheet row limit never masquerades as an analyzed-record limit.
6. Category tables show the top 40 categories, with all remaining groups rolled up into `Other` to reconcile record/value totals.
7. Most statuses are classified using an explicit vocabulary, not semantic model inference. Missing and unrecognized labels do not enter recognized-outcome success denominators.
8. Dates in ambiguous dd/mm/yyyy versus mm/dd/yyyy text formats may require preprocessing or ISO formatting.
9. Statistical testing, time-series forecasting, causal inference, automatic financial statement reconstruction, fraud detection and ML models are not included in this version.
10. Performance depends on source file complexity, disk, memory, operating system and installed Python environment. Large source workbooks should be trialed against local resource constraints.

## Improvement roadmap

- Multi-sheet relational analysis and join-key mapping with user approval.
- Streaming approximate distinct cardinality (HyperLogLog) and exact configurable business-key duplicate checks.
- Domain-specific templates for payments, finance, sales and research designs.
- Optional statistically principled confidence intervals, hypothesis tests and forecast evaluation.
- Policy-managed data anonymization and encrypted intermediate stores for regulated data.

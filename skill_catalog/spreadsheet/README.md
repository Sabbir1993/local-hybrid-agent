# Advanced Excel Analytics Skill

Reusable Agent Skill for Claude Code and OpenAI Codex. Transforms an Excel/CSV dataset into a documented, chart-rich analytical Excel report using streaming input ingestion. No API key, external data upload, or external AI service is needed for the built-in numerical engine.

## Quick start

**Requirement:** Python 3.10+.

1. Unzip the package so the folder `advanced-excel-analytics` contains `SKILL.md`.
2. From a terminal in that folder, create a virtual environment and install dependencies:

   **Windows PowerShell**
   ```powershell
   py -3 -m venv .venv
   .\.venv\Scripts\Activate.ps1
   python -m pip install -r requirements.txt
   ```

   **macOS / Linux**
   ```bash
   python3 -m venv .venv
   source .venv/bin/activate
   python -m pip install -r requirements.txt
   ```
3. Test the engine:
   ```bash
   python -m unittest discover -s tests -v
   python scripts/analyze.py examples/sample_transactions.csv --output examples/Sample_Analysis_Report.xlsx --mode deep
   ```
4. Run on your file:
   ```bash
   python scripts/analyze.py "path/to/your-file.xlsx" --output "path/to/Analysis_Report.xlsx" --mode deep
   ```

## One-command skill installation

From the extracted `advanced-excel-analytics` directory:

```powershell
# Windows PowerShell; installs as a personal Claude Code skill and its dependencies
py -3 install_skill.py --agent claude --scope user --install-deps

# Use the skill with Codex instead
py -3 install_skill.py --agent codex --scope user --install-deps

# Or install to both (separate skill directories)
py -3 install_skill.py --agent both --scope user --install-deps
```

On macOS/Linux substitute `python3` for `py -3`.

To install only for the current project, run `python install_skill.py --agent claude --scope project --project-dir /path/to/project --install-deps` (use `codex` as needed). Re-running requires `--force` to replace an installed skill.

The agent should use the Python interpreter in the installed skill's `.venv`; do not assume shell activation persists across tool executions.

## Install as agent skill

**Claude Code**
- Global: copy the entire folder into `~/.claude/skills/advanced-excel-analytics/`.
- Project only: copy it to `<project>/.claude/skills/advanced-excel-analytics/`.
- In Claude Code ask: `Use advanced-excel-analytics to analyze ./data/sales.xlsx and create a detailed executive dashboard.`

**Codex**
- Global: copy into `~/.agents/skills/advanced-excel-analytics/`.
- Project only: copy to `<project>/.agents/skills/advanced-excel-analytics/`.
- Ask Codex: `$advanced-excel-analytics analyze ./data/sales.xlsx and return the report.`

**Claude web app:** Its support for uploaded custom skill ZIPs depends on product settings and the available code execution environment. Scripts that require local package installation may not run there; Claude Code is the recommended setup.

After copying into an agent skills directory, install `requirements.txt` in the Python environment in which the agent will run the scripts. The agent must have permission to read input files and write the destination.

## Output

An `.xlsx` workbook containing (when supported): `Executive_Dashboard`, `Executive_Summary`, `Key_Insights`, `Data_Quality`, `Descriptive_Statistics`, `Trend_Analysis`, `Category_Analysis`, `Status_Analysis`, `Distribution`, `Anomaly_Analysis`, `Data_Dictionary`, `Methodology`, `Source_Metadata`, `Validation_Status`, and `Input_Preview`.

Additional `<report-name>.manifest.json` documents source metadata, quality metrics, methods, actual chart count and validation results.

## Options

```text
--sheet NAME                  worksheet to analyze (default: first nonempty sheet)
--mode standard|deep          depth of data profiling and reporting
--date-column NAME           explicit date/time field
--value-column NAME          amount/value to sum and chart
--group-column NAME          dimension for group comparisons
--status-column NAME         outcome/status field
--success-values CSV         recognized positive outcomes
--failure-values CSV         recognized negative outcomes
--preview-rows N             bounded records in Input_Preview (default 1000)
--sample-size N              reservoir sample size per numeric field (default 10000)
--output PATH                 destination .xlsx file
```

## Important constraints

- One worksheet per invocation. Run the tool separately for multiple incompatible sheets.
- `.xlsx`, `.xlsm`, and `.csv` supported. Legacy `.xls` is supported only when the optional `python-calamine` package is installed; behavior depends on package version.
- Only cached input formula results are read; Excel formula execution is not provided.
- Native Excel row limit: 1,048,576 per sheet. The report deliberately includes an input preview, never a silently truncated supposed full data copy.
- Approximate percentiles and histograms are marked as sample-based. Counts, sums, groupings, min/max and mean are computed on complete valid rows.
- Exact-row duplicate checks store hashes in a temporary SQLite database rather than loading the full dataset in memory.
- Both modes scan all source records. `standard` uses a smaller sample cap for distribution statistics; `deep` allows larger samples.
- Hidden/filtered source rows are included. The source file is never modified.
- A recognized status can be counted as success/failure, but this is descriptive; it is not a fraud-detection algorithm.
- Figures and insight descriptions use only observed values. This is an analytical engine, not an external LLM reasoning call.

## Suggested prompts

1. "Analyze my Excel file and generate a professional executive dashboard with insights, trends, graphs, and data quality checks."
2. "Evaluate monthly transaction volume and value by payment method; generate evidence-based findings."
3. "Find missing data, duplicates, negative values and outliers, and explain what requires attention."
4. "Compare sales by product category, summarize each month, and add interactive-style Excel-native charts."
5. "Provide a detailed statistical analysis of the numeric measurements and an Excel distribution chart."

## Files

- `SKILL.md` — discovery and execution instructions for the agent.
- `install_skill.py` — installer for Claude Code/Codex (Windows/macOS/Linux).
- `scripts/analyze.py` — executable CLI.
- `scripts/analytics.py` — data ingestion, streaming aggregation and insights.
- `scripts/report.py` — Excel workbook and chart writer.
- `tests/` — unit and end-to-end verification tests.
- `examples/sample_transactions.csv` — deterministic sample input.

## Safety

All processing is local. The tool does not run macros, and all imported text is written through safe string APIs to avoid formula injection. Keep sensitive workbooks in access-controlled folders.

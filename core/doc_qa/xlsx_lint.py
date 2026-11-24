"""Pre-flight Quality Assurance Linter for Excel Workbooks (.xlsx).

Inspects workbooks for:
1. Formula errors: #VALUE!, #REF!, #DIV/0!, #NAME?, #NULL!, #NUM!, #N/A.
2. Suspicious string literals that resemble unresolved errors.
3. Column width truncation risks (numbers that may render as ###).
4. Blank/empty workbooks or sheets without rows.
"""

import io
import re
from typing import Optional
import openpyxl
from openpyxl.utils import get_column_letter

ERROR_VALUES = {
    "#VALUE!", "#REF!", "#DIV/0!", "#NAME?", "#NULL!", "#NUM!", "#N/A"
}

ERROR_RX = re.compile(r"#(?:VALUE|REF|DIV/0|NAME\?|NULL|NUM|N/A)!", re.I)


def lint_xlsx(data: bytes) -> list[str]:
    """Inspects an in-memory .xlsx file and returns a list of warnings/errors.
    An empty list indicates the workbook passed all QA checks.
    """
    issues: list[str] = []
    try:
        wb = openpyxl.load_workbook(io.BytesIO(data), data_only=False)
    except Exception as e:
        return [f"Workbook cannot be parsed: {e}"]

    if not wb.sheetnames:
        return ["Workbook contains zero sheets."]

    for sheet_name in wb.sheetnames:
        ws = wb[sheet_name]
        max_r = ws.max_row
        max_c = ws.max_column

        if max_r == 1 and max_c == 1 and ws.cell(1, 1).value is None:
            issues.append(f"Sheet '{sheet_name}' is completely empty.")
            continue

        for r in range(1, max_r + 1):
            for c in range(1, max_c + 1):
                cell = ws.cell(row=r, column=c)
                v = cell.value
                if v is None:
                    continue

                v_str = str(v).strip()
                coord = f"{get_column_letter(c)}{r}"

                # Check for explicit Excel error codes
                if v_str.upper() in ERROR_VALUES or ERROR_RX.search(v_str):
                    issues.append(f"Sheet '{sheet_name}' cell {coord} contains error: {v_str}")

                # Check for formula syntax issues
                if isinstance(v, str) and v.startswith("="):
                    if len(v) < 2:
                        issues.append(f"Sheet '{sheet_name}' cell {coord} has empty formula '='")
                    # Check for unclosed parentheses in formulas
                    if v.count("(") != v.count(")"):
                        issues.append(f"Sheet '{sheet_name}' cell {coord} has unbalanced parentheses in formula: {v}")

                # Check for potential column width truncation on numeric values
                col_letter = get_column_letter(c)
                col_dim = ws.column_dimensions.get(col_letter)
                allocated_width = col_dim.width if (col_dim and col_dim.width is not None) else 8.43
                val_len = len(v_str)

                # Numbers in cells narrower than their string length show as ###
                if isinstance(v, (int, float)) and val_len > allocated_width:
                    issues.append(f"Sheet '{sheet_name}' column {col_letter} width ({allocated_width:.1f}) is narrower than number in cell {coord} ({val_len} chars); will display as ###")

    return issues

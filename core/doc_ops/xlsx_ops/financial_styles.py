"""Financial and banking styling standards for Excel workbooks.

Provides standardized accounting number formats, corporate banking color palettes,
accounting borders (thin top, double bottom underline for totals), alignment rules,
and auto-formatting for banking MIS reports and credit analyses.
"""

from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
from openpyxl.utils import get_column_letter

# Standard OpenXML Accounting / Currency formats
FMT_ACCOUNTING_BDT = '_([$৳-bn-BD]* #,##0.00_);_([$৳-bn-BD]* (#,##0.00);_([$৳-bn-BD]* "-"??_);_(@_)'
FMT_CURRENCY_BDT = '[$৳-bn-BD] #,##0.00;([$৳-bn-BD] #,##0.00);"-"'
FMT_ACCOUNTING_USD = '_($* #,##0.00_);_($* (#,##0.00);_($* "-"??_);_(@_)'
FMT_NUMBER_DECIMAL = '#,##0.00;(#,##0.00);"-"'
FMT_NUMBER_INT = '#,##0;(#,##0);"-"'
FMT_PERCENT = '0.00%'
FMT_DATE = 'yyyy-mm-dd'

# Bank Color Palette
COLOR_HEADER_NAVY = "0F2C59"       # Deep Bank Navy
COLOR_HEADER_TEXT = "FFFFFF"       # Crisp White
COLOR_ZEBRA_LIGHT = "F8FAFC"       # Slate 50
COLOR_TOTAL_FILL  = "F1F5F9"       # Slate 100
COLOR_BORDER_GRID = "E2E8F0"       # Slate 200
COLOR_BORDER_RULE = "94A3B8"       # Slate 400
COLOR_BORDER_DARK = "0F172A"       # Slate 900

# Reusable Style Objects
FONT_HEADER = Font(name="Calibri", size=11, bold=True, color=COLOR_HEADER_TEXT)
FONT_DATA = Font(name="Calibri", size=11, bold=False, color="1E293B")
FONT_TOTAL = Font(name="Calibri", size=11, bold=True, color="0F172A")

FILL_HEADER = PatternFill("solid", fgColor=COLOR_HEADER_NAVY)
FILL_ZEBRA = PatternFill("solid", fgColor=COLOR_ZEBRA_LIGHT)
FILL_TOTAL = PatternFill("solid", fgColor=COLOR_TOTAL_FILL)

BORDER_GRID = Border(
    left=Side(style="thin", color=COLOR_BORDER_GRID),
    right=Side(style="thin", color=COLOR_BORDER_GRID),
    top=Side(style="thin", color=COLOR_BORDER_GRID),
    bottom=Side(style="thin", color=COLOR_BORDER_GRID),
)

# Standard accounting border for Summary/Total rows: thin top line, double bottom line
BORDER_TOTAL = Border(
    left=Side(style="thin", color=COLOR_BORDER_GRID),
    right=Side(style="thin", color=COLOR_BORDER_GRID),
    top=Side(style="thin", color=COLOR_BORDER_RULE),
    bottom=Side(style="double", color=COLOR_BORDER_DARK),
)

ALIGN_LEFT = Alignment(horizontal="left", vertical="center")
ALIGN_RIGHT = Alignment(horizontal="right", vertical="center")
ALIGN_CENTER = Alignment(horizontal="center", vertical="center")
ALIGN_HEADER = Alignment(horizontal="center", vertical="center", wrap_text=True)


def is_total_row(label: str) -> bool:
    """Checks if a row header indicates a summary or total row."""
    if not isinstance(label, str):
        return False
    s = label.strip().lower()
    tokens = ("total", "grand total", "summary", "overall", "net total", "subtotal", "average", "avg")
    return any(s == t or s.startswith(t + " ") or s.startswith(t + "(") or s.startswith(t + ":") or s.startswith(t + "-") for t in tokens)


def apply_financial_styling(ws, rows: list[list], theme: str = "navy") -> None:
    """Applies professional enterprise banking styles to an openpyxl Worksheet.
    
    - Formats header row with Bank Navy fill and white bold text.
    - Applies zebra-banding to alternating rows.
    - Inspects data types to right-align numbers and apply comma/currency formats.
    - Automatically detects summary/total rows and applies accounting borders.
    - Pins header row with freeze_panes.
    - Adds auto-filter to header.
    - Dynamically computes column widths to prevent `###` display truncation.
    """
    if not rows:
        return

    num_rows = len(rows)
    num_cols = max(len(r) for r in rows) if rows else 0
    if num_cols == 0:
        return

    # 1. Header Styling
    ws.row_dimensions[1].height = 28
    for col_idx in range(1, num_cols + 1):
        cell = ws.cell(row=1, column=col_idx)
        cell.font = FONT_HEADER
        cell.fill = FILL_HEADER
        cell.alignment = ALIGN_HEADER
        cell.border = BORDER_GRID

    # Enable freeze panes at row 2 and auto-filter across all columns
    ws.freeze_panes = "A2"
    start_letter = get_column_letter(1)
    end_letter = get_column_letter(num_cols)
    ws.auto_filter.ref = f"{start_letter}1:{end_letter}{num_rows}"

    # 2. Body Styling
    for r_idx, row in enumerate(rows[1:], start=2):
        ws.row_dimensions[r_idx].height = 20
        first_val = row[0] if len(row) > 0 else ""
        is_total = is_total_row(str(first_val) if first_val is not None else "")
        is_even = (r_idx % 2 == 0)

        for c_idx in range(1, num_cols + 1):
            val = row[c_idx - 1] if len(row) >= c_idx else None
            cell = ws.cell(row=r_idx, column=c_idx)

            if is_total:
                cell.font = FONT_TOTAL
                cell.fill = FILL_TOTAL
                cell.border = BORDER_TOTAL
            else:
                cell.font = FONT_DATA
                if is_even:
                    cell.fill = FILL_ZEBRA
                cell.border = BORDER_GRID

            # Alignment & Number Formatting
            if isinstance(val, (int, float)):
                cell.alignment = ALIGN_RIGHT
                if isinstance(val, float):
                    cell.number_format = FMT_NUMBER_DECIMAL
                else:
                    cell.number_format = FMT_NUMBER_INT
            elif isinstance(val, str):
                v_str = val.strip()
                if v_str.startswith("=") and is_total:
                    cell.alignment = ALIGN_RIGHT
                    cell.number_format = FMT_NUMBER_DECIMAL
                elif v_str.endswith("%") and (v_str[:-1].replace(".", "", 1).replace("-", "", 1).isdigit()):
                    cell.alignment = ALIGN_RIGHT
                    cell.number_format = FMT_PERCENT
                elif any(v_str.startswith(sym) for sym in ("৳", "Tk", "BDT", "$", "€", "£")):
                    cell.alignment = ALIGN_RIGHT
                    cell.number_format = FMT_CURRENCY_BDT if ("৳" in v_str or "Tk" in v_str or "BDT" in v_str) else FMT_ACCOUNTING_USD
                elif v_str.replace(",", "").replace(".", "", 1).replace("-", "", 1).isdigit():
                    cell.alignment = ALIGN_RIGHT
                    if "." in v_str:
                        cell.number_format = FMT_NUMBER_DECIMAL
                    else:
                        cell.number_format = FMT_NUMBER_INT
                else:
                    cell.alignment = ALIGN_LEFT

    # 3. Dynamic Column Widths with padding
    for c_idx in range(1, num_cols + 1):
        col_letter = get_column_letter(c_idx)
        max_len = 0
        for r_idx in range(1, num_rows + 1):
            cell_val = ws.cell(row=r_idx, column=c_idx).value
            if cell_val is not None:
                max_len = max(max_len, len(str(cell_val)))
        # Margin of 4, minimum width 12, maximum 55
        ws.column_dimensions[col_letter].width = min(max(max_len + 4, 12), 55)

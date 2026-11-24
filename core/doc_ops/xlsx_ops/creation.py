import io
from .coercion import _coerce
from .financial_styles import apply_financial_styling


def create(rows: list[list], sheet: str = "Sheet1") -> bytes:
    import openpyxl
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = sheet[:31] or "Sheet1"
    for r, vals in enumerate(rows, 1):
        for c, v in enumerate(vals, 1):
            kind, payload = _coerce(v, allow_formula=False) if not (isinstance(v, str) and v.startswith("=")) \
                else ("str", v)
            cell = ws.cell(row=r, column=c, value=payload if kind != "empty" else None)
            if kind == "str":
                cell.data_type = "s"   # model text like "=cmd" stays text, never a formula
    if rows:
        apply_financial_styling(ws, rows)
    out = io.BytesIO()
    wb.save(out)
    return out.getvalue()

import io
from .book import _Book
from .constants import (
    LOSSY_PARTS,
    MAX_CELLS_SHOWN,
    MAX_ROWS_SHOWN,
    _fmt,
)


def inspect(data: bytes) -> dict:
    import openpyxl
    book = _Book(data)
    wb = openpyxl.load_workbook(io.BytesIO(data), read_only=True, data_only=False)
    extras = sorted({p.split("/")[1] for p in book.parts if p.startswith(LOSSY_PARTS)})
    head = f"Excel workbook: sheets {[n for n, _ in book.sheets]}"
    if extras:
        head += f" (also contains: {', '.join(extras)} - preserved by cell edits)"
    els, shown = [], 0
    for ws in wb.worksheets:
        dims = ws.calculate_dimension() if hasattr(ws, "calculate_dimension") else ""
        els.append({"addr": f"{ws.title}!", "kind": "sheet", "text": f"range {dims}"})
        for r_i, row in enumerate(ws.iter_rows(), 1):
            if r_i > MAX_ROWS_SHOWN or shown >= MAX_CELLS_SHOWN:
                els.append({"addr": f"{ws.title}!", "kind": "more", "text": f"(rows after {r_i - 1} not shown - use focus)"})
                break
            real = [c for c in row if getattr(c, "value", None) is not None and hasattr(c, "row")]
            if not real:
                continue
            cells = [(c.coordinate, c.value) for c in real]
            shown += len(cells)
            els.append({"addr": f"{ws.title}!row{real[0].row}", "kind": "row",
                        "text": " | ".join(f"{k}={_fmt(v)}" for k, v in cells)})
    wb.close()
    return {"kind": "xlsx", "header": head, "elements": els}

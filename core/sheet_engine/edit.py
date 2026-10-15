"""Restyle / extend a workbook that was built here: the ops change its OUTLINE (the source kept with the file) and the
workbook is rebuilt from it, so charts, formats, rules and styles that a cell-level edit cannot express are all still
possible. Cell-level ops (set_cell, set_formula, set_range, append_rows ...) stay byte-preserving and are not handled here;
after one of them the file no longer matches its outline, so these ops are refused until the workbook is rebuilt.

Ops: add_chart, set_format, add_highlight, add_scale, add_databar, add_validation, set_total, set_theme, set_freeze,
set_table, set_filter, add_note, add_column.
"""
import re
from typing import Optional

from .build import BuildError, _SHEET_RX, _split_row, build, is_outline, parse

OUTLINE_OPS = ("add_outline_sheet", "add_chart", "set_format", "add_highlight", "add_scale", "add_databar", "add_validation", "set_total",
               "set_theme", "set_freeze", "set_table", "set_filter", "add_note", "add_column")


class EditError(ValueError):
    pass


def is_outline_op(op: dict) -> bool:
    return str((op or {}).get("op") or "").lower() in OUTLINE_OPS


def _sections(lines: list) -> list:
    """[(sheet name, first line index, end index)] for each `## Sheet:` block (a single unnamed sheet counts as 'Sheet1')."""
    heads = [(i, _SHEET_RX.match(l).group(1)) for i, l in enumerate(lines) if _SHEET_RX.match(l)]
    if not heads:
        first = next((i for i, l in enumerate(lines) if l.lstrip().startswith("|")), 0)
        return [("Sheet1", first, len(lines))]
    return [(name, i, heads[k + 1][0] if k + 1 < len(heads) else len(lines)) for k, (i, name) in enumerate(heads)]


def _find(lines: list, sheet: Optional[str]) -> tuple:
    secs = _sections(lines)
    if not sheet:
        return secs[0]
    for s in secs:
        if s[0].lower() == str(sheet).strip().lower():
            return s
    raise EditError(f"sheet '{sheet}' not found (sheets: {', '.join(s[0] for s in secs)})")


def _append_directive(lines: list, sec: tuple, line: str) -> None:
    end = sec[2]
    while end > sec[1] and not lines[end - 1].strip():
        end -= 1
    lines.insert(end, line)


def _need(op: dict, key: str) -> str:
    v = op.get(key)
    if v is None or str(v).strip() == "":
        raise EditError(f"{op.get('op')}: {key} required")
    return str(v).strip()


def apply_outline_ops(outline: str, ops: list) -> tuple:
    """-> (new outline, [change descriptions]). The caller rebuilds the workbook and surfaces build errors."""
    lines = str(outline).split("\n")
    changes = []
    for op in ops:
        kind = str(op.get("op")).lower()
        sheet = op.get("sheet")
        if kind == "add_outline_sheet":
            name, body = _need(op, "name"), _need(op, "content")
            if any(n.lower() == name.lower() for n, _a, _b in _sections(lines)):
                raise EditError(f"add_outline_sheet: a sheet named '{name}' already exists")
            body_lines = body.strip().splitlines()
            if not any(ln.lstrip().startswith("|") for ln in body_lines):
                raise EditError("add_outline_sheet: content must contain a markdown table (| Col | Col | then |---|---| and rows)")
            while lines and not lines[-1].strip():
                lines.pop()
            lines += ["", f"## Sheet: {name}"] + body_lines
            changes.append(f"sheet '{name}' added")
            continue
        if kind == "set_theme":
            theme = _need(op, "theme").lower()
            idx = next((i for i, l in enumerate(lines) if re.match(r"^\s*theme\s*:", l, re.I)), None)
            if idx is not None:
                lines[idx] = f"theme: {theme}"
            else:
                at = next((i + 1 for i, l in enumerate(lines) if re.match(r"^\s*#\s+", l)), 0)
                lines.insert(at, f"theme: {theme}")
            changes.append(f"theme set to {theme}")
            continue
        sec = _find(lines, sheet)
        label = f"{sec[0]}: "
        if kind == "add_chart":
            spec = op.get("chart") or " | ".join(filter(None, [
                str(op.get("type") or "bar"), f"title={op['title']}" if op.get("title") else "",
                f"x={op['x']}" if op.get("x") else "", f"y={op['y']}" if op.get("y") else "",
                f"at={op['at']}" if op.get("at") else "", f"source={op['source']}" if op.get("source") else ""]))
            _append_directive(lines, sec, f"chart: {spec}")
            changes.append(label + f"chart added ({spec})")
        elif kind == "set_format":
            col, fmt = _need(op, "column"), _need(op, "format")
            _append_directive(lines, sec, f"format: {col}={fmt}")
            changes.append(label + f"{col} formatted as {fmt}")
        elif kind == "add_highlight":
            rule = _need(op, "rule")
            _append_directive(lines, sec, f"highlight: {rule}")
            changes.append(label + f"highlight rule '{rule}'")
        elif kind in ("add_scale", "add_databar"):
            col = _need(op, "column") + (" reverse" if op.get("reverse") else "")
            _append_directive(lines, sec, ("scale: " if kind == "add_scale" else "databar: ") + col)
            changes.append(label + f"{'colour scale' if kind == 'add_scale' else 'data bars'} on {col}")
        elif kind == "add_validation":
            rule = _need(op, "rule")
            _append_directive(lines, sec, f"validate: {rule}")
            changes.append(label + f"validation '{rule}'")
        elif kind == "set_total":
            col, fn = _need(op, "column"), _need(op, "fn")
            _append_directive(lines, sec, f"total: {col}={fn}")
            changes.append(label + f"total row: {fn} of {col}")
        elif kind == "set_freeze":
            _append_directive(lines, sec, f"freeze: {_need(op, 'cell')}")
            changes.append(label + f"freeze panes at {op['cell']}")
        elif kind == "set_table":
            _append_directive(lines, sec, f"table: {_need(op, 'name')}")
            changes.append(label + f"Excel table '{op['name']}'")
        elif kind == "set_filter":
            _append_directive(lines, sec, f"filter: {'on' if op.get('on', True) else 'off'}")
            changes.append(label + f"filter {'on' if op.get('on', True) else 'off'}")
        elif kind == "add_note":
            _append_directive(lines, sec, f"note: {_need(op, 'text')}")
            changes.append(label + "note added")
        elif kind == "add_column":
            name, formula = _need(op, "name"), _need(op, "formula")
            if not formula.startswith("="):
                raise EditError("add_column: formula must start with = and may use {r} for the row number (=C{r}*TaxRate)")
            rows = [i for i in range(sec[1], sec[2]) if lines[i].lstrip().startswith("|")]
            if len(rows) < 2:
                raise EditError(f"add_column: sheet '{sec[0]}' has no table")
            header = _split_row(lines[rows[0]])
            lines[rows[0]] = "| " + " | ".join(header + [name]) + " |"
            n = 0
            for i in rows[1:]:
                cells = _split_row(lines[i])
                if all(re.fullmatch(r":?-{2,}:?", c.strip()) or not c.strip() for c in cells):
                    continue                                              # the |---| separator
                n += 1
                lines[i] = "| " + " | ".join(cells + [formula.replace("{r}", str(n + 1))]) + " |"
            changes.append(label + f"column '{name}' added ({n} rows)")
        else:
            raise EditError(f"unknown workbook op '{kind}'")
    return "\n".join(lines), changes


def edit(outline: Optional[str], ops: list) -> tuple:
    """-> (xlsx bytes with cached values, new outline, changes). Raises EditError / BuildError with a reason for the model."""
    if not outline or not is_outline(outline):
        raise EditError("this workbook was not built from an outline here (or it was changed cell by cell since), so it cannot be "
                        "restyled - rebuild it with doc_create, or change cells with set_cell / set_formula / set_range")
    new_outline, changes = apply_outline_ops(outline, ops)
    try:
        data = build(new_outline)
    except BuildError as e:
        raise EditError(str(e))
    try:
        from . import calc
        data = calc.write_cached(data, calc.calculate(data)["values"])
    except Exception:
        pass
    parse(new_outline)
    return data, new_outline, changes

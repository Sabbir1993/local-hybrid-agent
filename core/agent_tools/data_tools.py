"""data_query: read-only structured queries over a spreadsheet the user attached (core/file_tools/data_query.py)."""

import re  # noqa: E402

DATA_QUERY_SCHEMA = {
    "type": "function",
    "function": {
        "name": "data_query",
        "description": "Exact answers over an attached Excel / CSV data set: how many rows match, totals, averages, group-by, top-N, "
                       "distinct values, row listings. The attachment in the message is only a profile (columns, types, ranges, "
                       "samples), so use this for any figure that must be right instead of estimating from the samples. Start with "
                       "describe=true if you need the columns. Read-only; never changes the file. Use the file name given in the "
                       "attachment note.",
        "parameters": {
            "type": "object",
            "properties": {
                "file": {"type": "string", "description": "the attached file's name, exactly as shown in the attachment note"},
                "sheet": {"type": "string", "description": "sheet name (default: the first sheet)"},
                "describe": {"type": "boolean", "description": "return the profile of every sheet (columns, types, ranges) instead of querying"},
                "filters": {
                    "type": "array",
                    "description": "all must match. Each {column, op, value}; op: =, !=, contains, not_contains, starts_with, >, >=, <, <=, in (value is a list), is_empty, not_empty",
                    "items": {"type": "object", "properties": {"column": {"type": "string"}, "op": {"type": "string"},
                                                               "value": {"description": "text, number or list (for in)"}},
                              "required": ["column"]},
                },
                "group_by": {"type": "string", "description": "one row per distinct value of this column, with the aggregates in agg (default: row count)"},
                "agg": {
                    "type": "array",
                    "description": "aggregates over the matching rows (per group when group_by is set). Each {fn: count|sum|avg|min|max|distinct, column}; count needs no column",
                    "items": {"type": "object", "properties": {"fn": {"type": "string"}, "column": {"type": "string"}}, "required": ["fn"]},
                },
                "distinct": {"type": "string", "description": "list the distinct values of this column with their row counts"},
                "select": {"type": "array", "items": {"type": "string"}, "description": "columns to list (default: the first 12)"},
                "sort_by": {"type": "string", "description": "column (or aggregate name) to sort by"},
                "desc": {"type": "boolean", "description": "sort descending (default true for groups)"},
                "count": {"type": "boolean", "description": "only return how many rows match"},
                "limit": {"type": "integer", "description": "max rows / groups to list (default 20, max 100)"},
            },
            "required": ["file"],
        },
    },
}


def tool_data_query(args: dict) -> str:
    from ..doc_tools.location import _file_arg, resolve_common
    from ..file_tools.data_query import QueryError, run_data_query
    try:
        name = _file_arg(args)
    except ValueError:
        return "error: file required (the attached file's name)"
    p = resolve_common(name)
    if p is None or not p.is_file():
        return f"error: no attached data file named '{name}' - use the name from the attachment note"
    if p.suffix.lower() not in (".xlsx", ".xls", ".csv"):
        return f"error: data_query reads .xlsx / .xls / .csv files, not {p.suffix or 'this file'}"
    try:
        return run_data_query(p, args)
    except QueryError as e:
        return f"error: {e}"
    except Exception as e:
        return f"error: could not read the data file ({type(e).__name__})"


DATA_SQL_SCHEMA = {
    "type": "function",
    "function": {
        "name": "data_sql",
        "description": "Exact analysis over attached Excel / CSV data with read-only SQL (SQLite): joins across sheets or files, "
                       "pivots with SUM(CASE WHEN ...), GROUP BY several columns, window functions, medians / percentiles "
                       "(MEDIAN, PERCENTILE(col, 0.9), STDEV, MODE), month / quarter buckets with strftime, top-N, ratios. "
                       "Call with describe=true first to see the table and column names. Each sheet is a table; dates are ISO text, "
                       "numbers are REAL. SELECT only; never changes the file. Prefer this over data_query for anything beyond a "
                       "single filter / one group-by.",
        "parameters": {
            "type": "object",
            "properties": {
                "files": {"type": "array", "items": {"type": "string"},
                          "description": "attached file name(s) exactly as shown in the attachment note; several files can be joined"},
                "file": {"type": "string", "description": "one attached file (same as files with one item)"},
                "describe": {"type": "boolean", "description": "list the tables and columns instead of running a query"},
                "sql": {"type": "string", "description": "one SELECT (or WITH ... SELECT) statement"},
                "limit": {"type": "integer", "description": "max rows returned (default 50, max 200)"},
            },
        },
    },
}


def tool_data_sql(args: dict) -> str:
    from ..doc_tools.location import resolve_common
    from ..sheet_engine import sql as sheet_sql
    names = list(args.get("files") or [])
    if args.get("file"):
        names.append(args["file"])
    names = [str(n) for n in names if n]
    if not names:
        return "error: file required (the attached file's name)"
    paths = []
    for n in names[:6]:
        p = resolve_common(n)
        if p is None or not p.is_file():
            return f"error: no attached data file named '{n}' - use the name from the attachment note"
        if p.suffix.lower() not in (".xlsx", ".xls", ".csv"):
            return f"error: data_sql reads .xlsx / .xls / .csv files, not {p.suffix or 'this file'}"
        paths.append(p)
    try:
        wb = sheet_sql.load(paths)
        if args.get("describe") or not args.get("sql"):
            return sheet_sql.describe(wb)
        return sheet_sql.query(wb, args["sql"], args.get("limit") or sheet_sql.DEFAULT_ROWS)
    except sheet_sql.SqlError as e:
        return f"error: {e}"
    except Exception as e:
        return f"error: could not read the data file ({type(e).__name__})"


DATA_CLEAN_SCHEMA = {
    "type": "function",
    "function": {
        "name": "data_clean",
        "description": "Clean an attached Excel / CSV sheet and save the result as a NEW workbook (the original is never changed; a "
                       "'Changes' sheet lists what each step did). ops is a list of steps: {op:'trim'}, {op:'dedupe', columns?:[..]}, "
                       "{op:'drop_empty_rows'}, {op:'drop_empty_columns'}, {op:'fix_types', columns?}, {op:'rename', from, to}, "
                       "{op:'fill', column, value | method:'down'}, {op:'case', column, to:'upper|lower|title'}, "
                       "{op:'replace', column, find, with}, {op:'drop_columns', columns:[..]}, "
                       "{op:'split', column, delimiter, into:['First','Last']}. Look at the data with data_sql / the profile first.",
        "parameters": {
            "type": "object",
            "properties": {
                "file": {"type": "string", "description": "the attached file's name, exactly as shown in the attachment note"},
                "sheet": {"type": "string", "description": "sheet name (default: the first sheet)"},
                "ops": {"type": "array", "items": {"type": "object"}, "description": "the cleaning steps, in order"},
            },
            "required": ["file", "ops"],
        },
    },
}


def tool_data_clean(args: dict) -> str:
    import uuid
    from pathlib import Path
    from ..doc_tools.location import _file_arg, resolve_common
    from ..sheet_engine import clean
    from .workspace import _common_resolve
    try:
        name = _file_arg(args)
    except ValueError:
        return "error: file required (the attached file's name)"
    p = resolve_common(name)
    if p is None or not p.is_file():
        return f"error: no attached data file named '{name}' - use the name from the attachment note"
    if p.suffix.lower() not in (".xlsx", ".xls", ".csv"):
        return f"error: data_clean reads .xlsx / .xls / .csv files, not {p.suffix or 'this file'}"
    try:
        data, report = clean.run(p, args.get("ops"), args.get("sheet"))
    except clean.CleanError as e:
        return f"error: {e}"
    except Exception as e:
        return f"error: could not clean the data file ({type(e).__name__})"
    stem = re.sub(r"([-_][0-9a-fA-F]{8})+$", "", Path(name).stem)
    out_name = f"{stem}-cleaned-{uuid.uuid4().hex[:8]}.xlsx"
    out = _common_resolve(out_name)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_bytes(data)
    try:
        from .. import doc_store
        from ..doc_ops.base import sha256
        from ..request_context import get_current_user_id
        doc_store.register(get_current_user_id(), out.name, "xlsx", sha256=sha256(data))
    except Exception:
        pass
    return f"Saved the cleaned data as {out.name}. {report}. [DOWNLOAD: {out.name}]"


DATA_IMPLS = {"data_query": tool_data_query, "data_sql": tool_data_sql, "data_clean": tool_data_clean}
DATA_SCHEMAS = [DATA_QUERY_SCHEMA, DATA_SQL_SCHEMA, DATA_CLEAN_SCHEMA]

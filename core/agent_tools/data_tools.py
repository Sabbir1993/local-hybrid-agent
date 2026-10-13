"""data_query: read-only structured queries over a spreadsheet the user attached (core/file_tools/data_query.py)."""

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


DATA_IMPLS = {"data_query": tool_data_query}
DATA_SCHEMAS = [DATA_QUERY_SCHEMA]

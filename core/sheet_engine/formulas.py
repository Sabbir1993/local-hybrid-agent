"""Which formulas a model may put in a workbook it builds.

A formula is code that runs inside the user's Excel, so the rule is an allow-list, not a block-list: ordinary arithmetic,
comparison and a fixed set of calculation / lookup / text / date functions. Anything that can reach outside the workbook
(HYPERLINK, WEBSERVICE, external links, DDE, INDIRECT/OFFSET that build references from text, CALL/REGISTER) is refused, with a
reason the model can act on. Cell data that merely starts with "=" in a user's own table is still escaped elsewhere; this
module only judges formulas the author wrote on purpose.
"""
import re

MAX_FORMULA_CHARS = 1000

ALLOWED_FUNCTIONS = frozenset("""
SUM SUMIF SUMIFS SUMPRODUCT SUBTOTAL AVERAGE AVERAGEIF AVERAGEIFS MIN MAX MINIFS MAXIFS COUNT COUNTA COUNTBLANK COUNTIF COUNTIFS
MEDIAN STDEV STDEV.S STDEV.P VAR VAR.S VAR.P LARGE SMALL RANK RANK.EQ PERCENTILE QUARTILE MODE CORREL
IF IFS IFERROR IFNA AND OR NOT XOR TRUE FALSE SWITCH CHOOSE
ROUND ROUNDUP ROUNDDOWN CEILING FLOOR INT MOD ABS SIGN SQRT POWER EXP LN LOG LOG10 PI TRUNC
VLOOKUP HLOOKUP XLOOKUP LOOKUP INDEX MATCH XMATCH ROW ROWS COLUMN COLUMNS
PMT IPMT PPMT FV PV NPV IRR XNPV XIRR RATE NPER
DATE DAY MONTH YEAR TODAY NOW EDATE EOMONTH DAYS DATEDIF WEEKDAY WEEKNUM NETWORKDAYS WORKDAY HOUR MINUTE SECOND TIME DATEVALUE
TEXT VALUE LEFT RIGHT MID LEN TRIM UPPER LOWER PROPER CONCAT CONCATENATE TEXTJOIN SUBSTITUTE REPLACE FIND SEARCH REPT EXACT
ISBLANK ISNUMBER ISTEXT ISERROR ISNA ISERR ISLOGICAL NA N
""".split())

_STRING = re.compile(r'"(?:[^"]|"")*"')
_FUNC = re.compile(r"([A-Za-z_][A-Za-z0-9_.]*)\s*\(")
_ERROR_LITERAL = re.compile(r"#(?:REF|NAME|DIV/0|VALUE|N/A|NUM|NULL)[!?]?", re.I)


class FormulaError(ValueError):
    pass


def functions_in(formula: str) -> list:
    """Upper-cased function names used (string literals ignored)."""
    body = _STRING.sub('""', formula)
    return [m.group(1).upper() for m in _FUNC.finditer(body)]


def check_formula(formula: str, sheets=None, names=None) -> str:
    """Return the formula unchanged when it is acceptable, else raise FormulaError with the reason.
    `sheets`: the sheet names that exist (a reference to another sheet must point at one of them).
    `names`: defined names that exist (informational; unknown names are reported by the workbook check, not refused)."""
    f = str(formula or "").strip()
    if not f.startswith("="):
        raise FormulaError("a formula must start with =")
    if len(f) > MAX_FORMULA_CHARS:
        raise FormulaError(f"formula is longer than {MAX_FORMULA_CHARS} characters - split it across helper columns")
    body = _STRING.sub('""', f)
    if "[" in body or "]" in body:
        raise FormulaError("references to other workbooks or structured table references are not allowed - use A1 ranges "
                           "(Sheet!B2:B20) or a defined name")
    if "|" in body:
        raise FormulaError("DDE-style formulas are not allowed")
    if re.search(r"(?i)\bfile:|https?:|\\\\|\.xl[sx]m?!", body):
        raise FormulaError("a formula may not point at files or URLs")
    for fn in functions_in(f):
        base = fn[6:] if fn.startswith("_XLFN.") else fn
        if base in ("HYPERLINK", "WEBSERVICE", "FILTERXML", "ENCODEURL", "CALL", "REGISTER", "REGISTER.ID", "EXEC", "SHELL"):
            raise FormulaError(f"{base} is not allowed (it can reach outside the workbook)")
        if base in ("INDIRECT", "OFFSET"):
            raise FormulaError(f"{base} builds references from text and is not allowed - use INDEX/MATCH or a direct reference")
        if base not in ALLOWED_FUNCTIONS:
            raise FormulaError(f"function {base} is not supported here; use one of the standard functions "
                               "(SUM, SUMIFS, COUNTIFS, AVERAGEIFS, IF, IFERROR, ROUND, INDEX, MATCH, VLOOKUP, XLOOKUP, TEXT, DATE, EOMONTH, ...)")
    if sheets is not None:
        known = {s.lower() for s in sheets}
        for m in re.finditer(r"(?:'((?:[^']|'')+)'|([A-Za-z_][A-Za-z0-9_.]*))!", body):
            name = (m.group(1) or m.group(2)).replace("''", "'")
            if name.lower() not in known:
                raise FormulaError(f"the formula refers to sheet '{name}' which does not exist in this workbook")
    return f


def hardcoded_numbers(formula: str) -> list:
    """Literal numbers other than 0, 1, 2, 12, 100 (and small step values) inside a formula: 'magic numbers' that belong in an
    assumptions cell. Cell references, strings and function names are ignored."""
    body = _STRING.sub('""', formula)
    body = re.sub(r"'(?:[^']|'')+'!|[A-Za-z_][A-Za-z0-9_.]*!", "", body)          # sheet prefixes
    body = re.sub(r"\$?[A-Za-z]{1,3}\$?\d+(?::\$?[A-Za-z]{1,3}\$?\d+)?", "", body)  # A1 / A1:B9 references
    body = re.sub(r"[A-Za-z_][A-Za-z0-9_.]*", "", body)                             # names and functions
    out = []
    for m in re.finditer(r"(?<![\w.])\d+(?:\.\d+)?%?", body):
        tok = m.group(0)
        try:
            val = float(tok.rstrip("%")) / (100 if tok.endswith("%") else 1)
        except ValueError:
            continue
        small_int = float(val).is_integer() and 0 <= val <= 31 and not tok.endswith("%")
        if not small_int and val not in (100, 109, 101, 102, 103, 104, 105, 60, 365, 52) and tok not in out:
            out.append(tok)
    return out

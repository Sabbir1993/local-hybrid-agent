"""Built-in calculator for the formulas a built workbook contains (the allow-list in formulas.py, the common subset).

Why: openpyxl writes formulas without their results, so a workbook made by a script shows blanks in any viewer that does not
recalculate (the in-app preview, previewers, pandas / openpyxl data_only), and nothing checks that the formulas are right.
This module evaluates the workbook, stores the results next to the formulas (cached values, Excel still recalculates on open),
and reports what is wrong: #DIV/0!, #REF!, #NAME?, circular references, functions it cannot evaluate (never guessed).
"""
import datetime as _dt
import io
import math
import re
import zipfile
from typing import Any

from .formulas import functions_in, hardcoded_numbers

_EPOCH = _dt.datetime(1899, 12, 30)


class XlError(Exception):
    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


class Unsupported(Exception):
    pass


def _err(code):
    raise XlError(code)


# ---- tokenizer -------------------------------------------------------------------------------------------------------
_TOKEN = re.compile(r"""
    (?P<ws>\s+)
  | (?P<str>"(?:[^"]|"")*")
  | (?P<num>\d+(?:\.\d+)?(?:[eE][+-]?\d+)?%?|\.\d+%?)
  | (?P<ref>(?:(?:'(?:[^']|'')+'|[A-Za-z_][A-Za-z0-9_.]*)!)?\$?[A-Za-z]{1,3}\$?\d+(?::\$?[A-Za-z]{1,3}\$?\d+)?(?![A-Za-z0-9_(]))
  | (?P<func>[A-Za-z_][A-Za-z0-9_.]*(?=\())
  | (?P<name>(?:(?:'(?:[^']|'')+'|[A-Za-z_][A-Za-z0-9_.]*)!)?[A-Za-z_][A-Za-z0-9_.]*)
  | (?P<op><>|<=|>=|[-+*/^&=<>(),:%])
""", re.X)


def _tokens(f: str) -> list:
    out, pos = [], 0
    while pos < len(f):
        m = _TOKEN.match(f, pos)
        if not m:
            raise Unsupported(f"cannot read formula near '{f[pos:pos + 12]}'")
        pos = m.end()
        kind = m.lastgroup
        if kind != "ws":
            out.append((kind, m.group(kind)))
    return out


# ---- parser to a tree ------------------------------------------------------------------------------------------------
class Parser:
    def __init__(self, f: str):
        self.t = _tokens(f.lstrip("="))
        self.i = 0

    def peek(self):
        return self.t[self.i] if self.i < len(self.t) else (None, None)

    def take(self):
        tok = self.peek()
        self.i += 1
        return tok

    def expect(self, op):
        if self.take()[1] != op:
            raise Unsupported(f"expected {op}")

    def parse(self):
        node = self.compare()
        if self.i != len(self.t):
            raise Unsupported("unexpected text after the formula")
        return node

    def compare(self):
        n = self.concat()
        while self.peek()[1] in ("=", "<>", "<", ">", "<=", ">="):
            op = self.take()[1]
            n = ("bin", op, n, self.concat())
        return n

    def concat(self):
        n = self.add()
        while self.peek()[1] == "&":
            self.take()
            n = ("bin", "&", n, self.add())
        return n

    def add(self):
        n = self.mul()
        while self.peek()[1] in ("+", "-"):
            op = self.take()[1]
            n = ("bin", op, n, self.mul())
        return n

    def mul(self):
        n = self.power()
        while self.peek()[1] in ("*", "/"):
            op = self.take()[1]
            n = ("bin", op, n, self.power())
        return n

    def power(self):
        n = self.unary()
        while self.peek()[1] == "^":
            self.take()
            n = ("bin", "^", n, self.unary())
        return n

    def unary(self):
        if self.peek()[1] in ("-", "+"):
            op = self.take()[1]
            return ("neg", self.unary()) if op == "-" else self.unary()
        return self.postfix()

    def postfix(self):
        n = self.atom()
        while self.peek()[1] == "%":
            self.take()
            n = ("bin", "/", n, ("num", 100.0))
        return n

    def atom(self):
        kind, val = self.take()
        if kind == "num":
            return ("num", float(val[:-1]) / 100 if val.endswith("%") else float(val))
        if kind == "str":
            return ("str", val[1:-1].replace('""', '"'))
        if kind == "ref":
            return ("ref", val)
        if kind == "name":
            up = val.upper()
            if up in ("TRUE", "FALSE"):
                return ("bool", up == "TRUE")
            return ("name", val)
        if kind == "func":
            self.expect("(")
            args = []
            if self.peek()[1] != ")":
                while True:
                    args.append(self.compare() if self.peek()[1] not in (",", ")") else ("empty",))
                    if self.peek()[1] == ",":
                        self.take()
                        continue
                    break
            self.expect(")")
            return ("call", val.upper().removeprefix("_XLFN."), args)
        if val == "(":
            n = self.compare()
            self.expect(")")
            return n
        raise Unsupported("unexpected token")


# ---- workbook model --------------------------------------------------------------------------------------------------
def _col_num(letters: str) -> int:
    n = 0
    for ch in letters.upper():
        n = n * 26 + ord(ch) - 64
    return n


def _split_ref(ref: str):
    sheet = None
    if "!" in ref:
        sheet, ref = ref.rsplit("!", 1)
        sheet = sheet[1:-1].replace("''", "'") if sheet.startswith("'") else sheet
    a, _, b = ref.replace("$", "").partition(":")
    ma = re.match(r"([A-Za-z]+)(\d+)$", a)
    mb = re.match(r"([A-Za-z]+)(\d+)$", b) if b else ma
    return sheet, (int(ma.group(2)), _col_num(ma.group(1))), (int(mb.group(2)), _col_num(mb.group(1)))


class Range:
    def __init__(self, rows):
        self.rows = rows                      # list of lists of values

    def flat(self):
        return [v for r in self.rows for v in r]


class Model:
    def __init__(self, wb):
        self.formulas = {}                    # (sheet, r, c) -> formula text
        self.values = {}                      # (sheet, r, c) -> constant
        self.names = {}                       # name -> 'Sheet'!A1
        self.sheets = []
        for ws in wb.worksheets:
            self.sheets.append(ws.title)
            for row in ws.iter_rows():
                for c in row:
                    v = c.value
                    if v is None:
                        continue
                    if isinstance(v, str) and v.startswith("=") and c.data_type == "f":
                        self.formulas[(ws.title, c.row, c.column)] = v
                    else:
                        self.values[(ws.title, c.row, c.column)] = v
        for n, dn in wb.defined_names.items():
            self.names[n.lower()] = dn.attr_text
        self.cache = {}
        self.stack = []
        self.unsupported = {}
        self.circular = set()

    def cell(self, sheet, r, c):
        key = (sheet, r, c)
        if key in self.cache:
            return self.cache[key]
        if key in self.formulas:
            if key in self.stack:
                self.circular.add(key)
                raise XlError("#CIRC")
            self.stack.append(key)
            try:
                try:
                    val = evaluate(Parser(self.formulas[key]).parse(), self, sheet)
                    if isinstance(val, Range):
                        val = val.rows[0][0] if val.rows and val.rows[0] else None
                except XlError as e:
                    val = e
                except Unsupported as e:
                    self.unsupported[key] = str(e)
                    val = Unsupported(str(e))
                self.cache[key] = val
            finally:
                self.stack.pop()
            return self.cache[key]
        v = self.values.get(key)
        if isinstance(v, (_dt.datetime, _dt.date)):
            v = _to_serial(v)
        return v

    def rng(self, sheet, a, b):
        rows = []
        for r in range(a[0], b[0] + 1):
            rows.append([self.cell(sheet, r, c) for c in range(a[1], b[1] + 1)])
        return Range(rows)


def _to_serial(d):
    if isinstance(d, _dt.datetime):
        return (d - _EPOCH).total_seconds() / 86400
    return float((_dt.datetime(d.year, d.month, d.day) - _EPOCH).days)


def _from_serial(n):
    return _EPOCH + _dt.timedelta(days=float(n))


# ---- evaluation ------------------------------------------------------------------------------------------------------
def _num(v):
    if isinstance(v, XlError):
        raise v
    if isinstance(v, Unsupported):
        raise v
    if v is None or v == "":
        return 0.0
    if isinstance(v, bool):
        return 1.0 if v else 0.0
    if isinstance(v, (int, float)):
        return float(v)
    if isinstance(v, str):
        try:
            return float(v.replace(",", ""))
        except ValueError:
            _err("#VALUE!")
    _err("#VALUE!")


def _flat_nums(args, skip_text=True):
    out = []
    for a in args:
        items = a.flat() if isinstance(a, Range) else [a]
        for v in items:
            if isinstance(v, (XlError, Unsupported)):
                raise v
            if isinstance(v, bool) and isinstance(a, Range):
                continue
            if isinstance(v, (int, float)) or isinstance(v, bool):
                out.append(float(v))
            elif isinstance(v, str) and not isinstance(a, Range):
                out.append(_num(v))
    return out


def _crit(c):
    """Excel criteria (">5", "<>x", "=a", 5, "text*") -> predicate."""
    if isinstance(c, str):
        m = re.match(r"^(<>|>=|<=|=|>|<)(.*)$", c)
        op, rhs = (m.group(1), m.group(2)) if m else ("=", c)
    else:
        op, rhs = "=", c
    try:
        num = float(rhs)
    except (TypeError, ValueError):
        num = None

    def test(v):
        if num is not None and isinstance(v, (int, float)) and not isinstance(v, bool):
            return {"=": v == num, "<>": v != num, ">": v > num, "<": v < num, ">=": v >= num, "<=": v <= num}[op]
        s = "" if v is None else str(v)
        r = str(rhs)
        if op in ("=", "<>"):
            rx = "^" + re.escape(r.lower()).replace(r"\*", ".*").replace(r"\?", ".") + "$"
            hit = re.match(rx, s.lower()) is not None
            return hit if op == "=" else not hit
        return False
    return test


def _bool(v):
    if isinstance(v, (XlError, Unsupported)):
        raise v
    if isinstance(v, str):
        if v.upper() in ("TRUE", "FALSE"):
            return v.upper() == "TRUE"
        _err("#VALUE!")
    return bool(_num(v)) if v is not None else False


def _round(x, d, mode="half_up"):
    f = 10 ** int(d)
    if mode == "up":
        return math.copysign(math.ceil(abs(x) * f - 1e-12) / f, x)
    if mode == "down":
        return math.copysign(math.floor(abs(x) * f + 1e-12) / f, x)
    return math.copysign(math.floor(abs(x) * f + 0.5 + 1e-12) / f, x)


def _text_fmt(v, fmt):
    f = str(fmt)
    if re.search(r"[yd]|mm", f, re.I) and isinstance(v, (int, float)):
        d = _from_serial(v)
        return d.strftime(f.lower().replace("yyyy", "%Y").replace("yy", "%y").replace("mmmm", "%B").replace("mmm", "%b")
                          .replace("mm", "%m").replace("dd", "%d").replace("hh", "%H").replace(":ss", ":%S"))
    n = _num(v)
    pct = f.endswith("%")
    if pct:
        n *= 100
        f = f[:-1]
    dec = len(f.split(".")[1].rstrip("%")) if "." in f else 0
    out = f"{n:,.{dec}f}" if "," in f else f"{n:.{dec}f}"
    return out + ("%" if pct else "")


def _lookup_exact(key, rows):
    for i, v in enumerate(rows):
        if v == key or (isinstance(v, str) and isinstance(key, str) and v.lower() == key.lower()):
            return i
    return None


def _irr(flows):
    lo, hi = -0.99, 10.0
    f = lambda r: sum(c / (1 + r) ** i for i, c in enumerate(flows))   # noqa: E731
    if f(lo) * f(hi) > 0:
        _err("#NUM!")
    for _ in range(200):
        mid = (lo + hi) / 2
        if f(lo) * f(mid) <= 0:
            hi = mid
        else:
            lo = mid
    return (lo + hi) / 2


def evaluate(node, m: Model, sheet: str):
    k = node[0]
    if k == "num" or k == "str" or k == "bool":
        return node[1]
    if k == "empty":
        return None
    if k == "neg":
        v = evaluate(node[1], m, sheet)
        if isinstance(v, Range):
            return Range([[-_num(x) for x in row] for row in v.rows])
        return -_num(v)
    if k == "name":
        ref = m.names.get(node[1].lower())
        if ref is None:
            _err("#NAME?")
        return evaluate(("ref", ref), m, sheet)
    if k == "ref":
        try:
            sh, a, b = _split_ref(node[1])
        except Exception:
            _err("#REF!")
        sh = sh or sheet
        if sh.lower() not in {s.lower() for s in m.sheets}:
            _err("#REF!")
        sh = next(s for s in m.sheets if s.lower() == sh.lower())
        if a == b:
            v = m.cell(sh, a[0], a[1])
            if isinstance(v, (XlError, Unsupported)):
                raise v
            return v
        return m.rng(sh, a, b)
    if k == "bin":
        op = node[1]
        a, b = evaluate(node[2], m, sheet), evaluate(node[3], m, sheet)
        for x in (a, b):
            if isinstance(x, (XlError, Unsupported)):
                raise x
        if isinstance(a, Range) or isinstance(b, Range):
            return _array_op(op, a, b)
        return _scalar_op(op, a, b)
    if k == "call":
        return _call(node[1], node[2], m, sheet)
    raise Unsupported("unknown expression")


def _array_op(op, a, b):
    """Element-wise operation (what SUMPRODUCT((range=x)*range) relies on). A scalar is broadcast over the range."""
    ra = a.rows if isinstance(a, Range) else None
    rb = b.rows if isinstance(b, Range) else None
    shape = ra or rb
    if ra and rb and (len(ra) != len(rb) or len(ra[0]) != len(rb[0])):
        _err("#VALUE!")
    out = []
    for i, row in enumerate(shape):
        out.append([_scalar_op(op, ra[i][j] if ra else a, rb[i][j] if rb else b) for j in range(len(row))])
    return Range(out)


def _scalar_op(op, a, b):
    if True:
        for x in (a, b):
            if isinstance(x, (XlError, Unsupported)):
                raise x
        if op == "&":
            return ("" if a is None else _s(a)) + ("" if b is None else _s(b))
        if op in ("=", "<>", "<", ">", "<=", ">="):
            if isinstance(a, str) and isinstance(b, str):
                a, b = a.lower(), b.lower()
            elif isinstance(a, str) != isinstance(b, str):
                if op == "=":
                    return False
                if op == "<>":
                    return True
                a, b = (1, 0) if isinstance(b, str) else (0, 1)        # text sorts after numbers
            a = 0 if a is None and not isinstance(b, str) else ("" if a is None else a)
            b = 0 if b is None and not isinstance(a, str) else ("" if b is None else b)
            return {"=": a == b, "<>": a != b, "<": a < b, ">": a > b, "<=": a <= b, ">=": a >= b}[op]
        x, y = _num(a), _num(b)
        if op == "+":
            return x + y
        if op == "-":
            return x - y
        if op == "*":
            return x * y
        if op == "/":
            if y == 0:
                _err("#DIV/0!")
            return x / y
        if op == "^":
            try:
                return x ** y
            except (ZeroDivisionError, OverflowError):
                _err("#NUM!")


def _s(v):
    if isinstance(v, bool):
        return "TRUE" if v else "FALSE"
    if isinstance(v, float) and v.is_integer():
        return str(int(v))
    return str(v)


def _call(name, args, m, sheet):
    lazy = {"IF", "IFERROR", "IFNA", "IFS", "SWITCH", "CHOOSE"}
    if name == "IF":
        c = _bool(evaluate(args[0], m, sheet))
        pick = args[1] if c else (args[2] if len(args) > 2 else ("bool", False))
        return evaluate(pick, m, sheet) if pick[0] != "empty" else 0.0
    if name in ("IFERROR", "IFNA"):
        try:
            v = evaluate(args[0], m, sheet)
            if isinstance(v, Range):
                v = v.rows[0][0]
            if isinstance(v, XlError):
                raise v
            return v
        except XlError as e:
            if name == "IFNA" and e.code != "#N/A":
                raise
            return evaluate(args[1], m, sheet)
    if name == "IFS":
        for i in range(0, len(args) - 1, 2):
            if _bool(evaluate(args[i], m, sheet)):
                return evaluate(args[i + 1], m, sheet)
        _err("#N/A")
    vals = [evaluate(a, m, sheet) for a in args] if name not in lazy else []
    for v in vals:
        if isinstance(v, (XlError, Unsupported)):
            raise v
    n = len(vals)
    if name == "SUM":
        return sum(_flat_nums(vals))
    if name in ("AVERAGE", "MEDIAN", "STDEV", "STDEV.S", "STDEV.P", "VAR", "VAR.S", "VAR.P", "MIN", "MAX"):
        nums = _flat_nums(vals)
        if not nums:
            if name in ("MIN", "MAX"):
                return 0.0
            _err("#DIV/0!")
        if name == "AVERAGE":
            return sum(nums) / len(nums)
        if name == "MIN":
            return min(nums)
        if name == "MAX":
            return max(nums)
        if name == "MEDIAN":
            s = sorted(nums)
            return s[len(s) // 2] if len(s) % 2 else (s[len(s) // 2 - 1] + s[len(s) // 2]) / 2
        mean = sum(nums) / len(nums)
        sq = sum((x - mean) ** 2 for x in nums)
        if name.endswith(".P"):
            var = sq / len(nums)
        else:
            if len(nums) < 2:
                _err("#DIV/0!")
            var = sq / (len(nums) - 1)
        return var if name.startswith("VAR") else math.sqrt(var)
    if name == "COUNT":
        return float(sum(1 for a in vals for v in (a.flat() if isinstance(a, Range) else [a])
                         if isinstance(v, (int, float)) and not isinstance(v, bool)))
    if name == "COUNTA":
        return float(sum(1 for a in vals for v in (a.flat() if isinstance(a, Range) else [a]) if v not in (None, "")))
    if name == "COUNTBLANK":
        return float(sum(1 for a in vals for v in (a.flat() if isinstance(a, Range) else [a]) if v in (None, "")))
    if name in ("SUMIF", "AVERAGEIF", "COUNTIF"):
        rng = vals[0].flat() if isinstance(vals[0], Range) else [vals[0]]
        test = _crit(vals[1])
        tgt = (vals[2].flat() if isinstance(vals[2], Range) else [vals[2]]) if n > 2 else rng
        hits = [i for i, v in enumerate(rng) if test(v)]
        if name == "COUNTIF":
            return float(len(hits))
        nums = [float(tgt[i]) for i in hits if i < len(tgt) and isinstance(tgt[i], (int, float)) and not isinstance(tgt[i], bool)]
        if name == "SUMIF":
            return sum(nums)
        if not nums:
            _err("#DIV/0!")
        return sum(nums) / len(nums)
    if name in ("SUMIFS", "AVERAGEIFS", "COUNTIFS", "MINIFS", "MAXIFS"):
        if name == "COUNTIFS":
            pairs, tgt = vals, None
        else:
            tgt, pairs = (vals[0].flat() if isinstance(vals[0], Range) else [vals[0]]), vals[1:]
        ranges = [(p.flat() if isinstance(p, Range) else [p]) for p in pairs[0::2]]
        tests = [_crit(c) for c in pairs[1::2]]
        size = len(ranges[0]) if ranges else 0
        hits = [i for i in range(size) if all(t(r[i]) for r, t in zip(ranges, tests))]
        if name == "COUNTIFS":
            return float(len(hits))
        nums = [float(tgt[i]) for i in hits if i < len(tgt) and isinstance(tgt[i], (int, float)) and not isinstance(tgt[i], bool)]
        if name == "SUMIFS":
            return sum(nums)
        if name == "MINIFS":
            return min(nums) if nums else 0.0
        if name == "MAXIFS":
            return max(nums) if nums else 0.0
        if not nums:
            _err("#DIV/0!")
        return sum(nums) / len(nums)
    if name == "SUMPRODUCT":
        arrs = [a.flat() if isinstance(a, Range) else [a] for a in vals]
        if len({len(a) for a in arrs}) != 1:
            _err("#VALUE!")
        return sum(math.prod(float(v) if isinstance(v, (int, float)) and not isinstance(v, bool) else 0.0 for v in col) for col in zip(*arrs))
    if name == "SUBTOTAL":
        fn = int(_num(vals[0]))
        base = fn % 100
        data = _flat_nums(vals[1:])
        if base == 9:
            return sum(data)
        if base == 1:
            if not data:
                _err("#DIV/0!")
            return sum(data) / len(data)
        if base in (2, 3):
            return float(len([v for a in vals[1:] for v in (a.flat() if isinstance(a, Range) else [a]) if v not in (None, "")] if base == 3 else data))
        if base == 4:
            return max(data) if data else 0.0
        if base == 5:
            return min(data) if data else 0.0
        raise Unsupported(f"SUBTOTAL({fn})")
    if name == "AND":
        return all(_bool(v) for a in vals for v in (a.flat() if isinstance(a, Range) else [a]) if v is not None)
    if name == "OR":
        return any(_bool(v) for a in vals for v in (a.flat() if isinstance(a, Range) else [a]) if v is not None)
    if name == "NOT":
        return not _bool(vals[0])
    if name == "ROUND":
        return _round(_num(vals[0]), _num(vals[1]) if n > 1 else 0)
    if name == "ROUNDUP":
        return _round(_num(vals[0]), _num(vals[1]) if n > 1 else 0, "up")
    if name == "ROUNDDOWN":
        return _round(_num(vals[0]), _num(vals[1]) if n > 1 else 0, "down")
    if name == "INT":
        return float(math.floor(_num(vals[0])))
    if name == "ABS":
        return abs(_num(vals[0]))
    if name == "SIGN":
        x = _num(vals[0])
        return float((x > 0) - (x < 0))
    if name == "MOD":
        d = _num(vals[1])
        if d == 0:
            _err("#DIV/0!")
        return _num(vals[0]) - d * math.floor(_num(vals[0]) / d)
    if name == "POWER":
        return _num(vals[0]) ** _num(vals[1])
    if name == "SQRT":
        x = _num(vals[0])
        if x < 0:
            _err("#NUM!")
        return math.sqrt(x)
    if name in ("LARGE", "SMALL"):
        nums = sorted(_flat_nums([vals[0]]), reverse=(name == "LARGE"))
        k_ = int(_num(vals[1]))
        if not 1 <= k_ <= len(nums):
            _err("#NUM!")
        return nums[k_ - 1]
    if name == "PERCENTILE":
        nums = sorted(_flat_nums([vals[0]]))
        p = _num(vals[1])
        if not nums or not 0 <= p <= 1:
            _err("#NUM!")
        pos = (len(nums) - 1) * p
        lo, hi = math.floor(pos), math.ceil(pos)
        return nums[lo] + (nums[hi] - nums[lo]) * (pos - lo)
    if name in ("RANK", "RANK.EQ"):
        x = _num(vals[0])
        nums = _flat_nums([vals[1]])
        asc = n > 2 and _num(vals[2]) != 0
        return float(1 + sum(1 for v in nums if (v < x if asc else v > x)))
    if name == "INDEX":
        rows = vals[0].rows if isinstance(vals[0], Range) else [[vals[0]]]
        r = int(_num(vals[1])) if n > 1 and vals[1] is not None else 1
        c = int(_num(vals[2])) if n > 2 and vals[2] is not None else 1
        if len(rows) == 1 and n == 2:
            r, c = 1, r
        if not (1 <= r <= len(rows) and 1 <= c <= len(rows[0])):
            _err("#REF!")
        return rows[r - 1][c - 1]
    if name in ("MATCH", "XMATCH"):
        look = vals[1].flat() if isinstance(vals[1], Range) else [vals[1]]
        mt = int(_num(vals[2])) if n > 2 and vals[2] is not None else (1 if name == "MATCH" else 0)
        if mt == 0:
            i = _lookup_exact(vals[0], look)
            if i is None:
                _err("#N/A")
            return float(i + 1)
        best = None
        for i, v in enumerate(look):
            if isinstance(v, (int, float)) and isinstance(vals[0], (int, float)) and ((v <= vals[0]) if mt == 1 else (v >= vals[0])):
                best = i
        if best is None:
            _err("#N/A")
        return float(best + 1)
    if name == "VLOOKUP":
        rows = vals[1].rows
        col = int(_num(vals[2]))
        approx = n < 4 or _bool(vals[3])
        keys = [r[0] for r in rows]
        i = _lookup_exact(vals[0], keys)
        if i is None and approx and isinstance(vals[0], (int, float)):
            cands = [j for j, v in enumerate(keys) if isinstance(v, (int, float)) and v <= vals[0]]
            i = cands[-1] if cands else None
        if i is None:
            _err("#N/A")
        if not 1 <= col <= len(rows[i]):
            _err("#REF!")
        return rows[i][col - 1]
    if name == "XLOOKUP":
        look = vals[1].flat() if isinstance(vals[1], Range) else [vals[1]]
        ret = vals[2].flat() if isinstance(vals[2], Range) else [vals[2]]
        i = _lookup_exact(vals[0], look)
        if i is None:
            if n > 3:
                return vals[3]
            _err("#N/A")
        return ret[i]
    if name == "TEXT":
        return _text_fmt(vals[0], vals[1])
    if name == "VALUE":
        return _num(vals[0])
    if name in ("LEN", "UPPER", "LOWER", "TRIM", "PROPER"):
        s = "" if vals[0] is None else _s(vals[0])
        return {"LEN": float(len(s)), "UPPER": s.upper(), "LOWER": s.lower(), "TRIM": " ".join(s.split()), "PROPER": s.title()}[name]
    if name in ("LEFT", "RIGHT"):
        s = "" if vals[0] is None else _s(vals[0])
        k_ = int(_num(vals[1])) if n > 1 else 1
        return s[:k_] if name == "LEFT" else (s[-k_:] if k_ else "")
    if name == "MID":
        s = "" if vals[0] is None else _s(vals[0])
        st = int(_num(vals[1]))
        return s[st - 1: st - 1 + int(_num(vals[2]))]
    if name in ("CONCAT", "CONCATENATE"):
        return "".join(_s(v) for a in vals for v in (a.flat() if isinstance(a, Range) else [a]) if v is not None)
    if name in ("ISBLANK", "ISNUMBER", "ISTEXT", "ISERROR"):
        v = vals[0]
        return {"ISBLANK": v is None, "ISNUMBER": isinstance(v, (int, float)) and not isinstance(v, bool),
                "ISTEXT": isinstance(v, str), "ISERROR": False}[name]
    if name == "DATE":
        y, mo, d = int(_num(vals[0])), int(_num(vals[1])), int(_num(vals[2]))
        y += (mo - 1) // 12
        mo = (mo - 1) % 12 + 1
        return _to_serial(_dt.date(y, mo, 1)) + d - 1
    if name in ("YEAR", "MONTH", "DAY"):
        d = _from_serial(_num(vals[0]))
        return float({"YEAR": d.year, "MONTH": d.month, "DAY": d.day}[name])
    if name in ("EOMONTH", "EDATE"):
        d = _from_serial(_num(vals[0]))
        total = d.year * 12 + d.month - 1 + int(_num(vals[1]))
        y, mo = divmod(total, 12)
        mo += 1
        if name == "EDATE":
            import calendar
            return _to_serial(_dt.date(y, mo, min(d.day, calendar.monthrange(y, mo)[1])))
        import calendar
        return _to_serial(_dt.date(y, mo, calendar.monthrange(y, mo)[1]))
    if name == "DAYS":
        return _num(vals[0]) - _num(vals[1])
    if name == "PMT":
        r, np_, pv = _num(vals[0]), _num(vals[1]), _num(vals[2])
        fv = _num(vals[3]) if n > 3 else 0.0
        typ = _num(vals[4]) if n > 4 else 0.0
        if r == 0:
            return -(pv + fv) / np_
        return -(r * (pv * (1 + r) ** np_ + fv)) / ((1 + r * typ) * ((1 + r) ** np_ - 1))
    if name == "FV":
        r, np_, pmt = _num(vals[0]), _num(vals[1]), _num(vals[2])
        pv = _num(vals[3]) if n > 3 else 0.0
        return -(pv + pmt * np_) if r == 0 else -(pv * (1 + r) ** np_ + pmt * ((1 + r) ** np_ - 1) / r)
    if name == "PV":
        r, np_, pmt = _num(vals[0]), _num(vals[1]), _num(vals[2])
        fv = _num(vals[3]) if n > 3 else 0.0
        return -(fv + pmt * np_) if r == 0 else -(fv + pmt * ((1 + r) ** np_ - 1) / r) / (1 + r) ** np_
    if name == "NPV":
        r = _num(vals[0])
        return sum(c / (1 + r) ** (i + 1) for i, c in enumerate(_flat_nums(vals[1:])))
    if name == "IRR":
        return _irr(_flat_nums([vals[0]]))
    if name in ("TODAY", "NOW"):
        raise Unsupported(f"{name} changes every day; Excel computes it on open")
    raise Unsupported(f"{name} is not evaluated by the built-in calculator")


# ---- public API ------------------------------------------------------------------------------------------------------
def calculate(data: bytes) -> dict:
    """Evaluate every formula. -> {values: {(sheet, 'A1'): value}, errors: [str], unsupported: [str], formulas: int,
    sheets: int, magic: [str]}"""
    import openpyxl
    wb = openpyxl.load_workbook(io.BytesIO(data))
    model = Model(wb)
    values, errors, magic = {}, [], []
    for (sh, r, c), f in model.formulas.items():
        ref = f"{openpyxl.utils.get_column_letter(c)}{r}"
        v = model.cell(sh, r, c)
        if isinstance(v, XlError):
            errors.append(f"{sh}!{ref} {v.code}  ({f[:60]})")
        elif isinstance(v, Unsupported):
            continue
        else:
            values[(sh, ref)] = v
        nums = hardcoded_numbers(f)
        if nums:
            magic.append(f"{sh}!{ref} has the number {nums[0]} typed into the formula - put it in an assumptions cell")
    unsupported = [f"{k[0]}!{openpyxl.utils.get_column_letter(k[2])}{k[1]}: {why}" for k, why in model.unsupported.items()]
    result = {"values": values, "errors": errors, "unsupported": unsupported, "formulas": len(model.formulas),
              "sheets": len(model.sheets), "magic": magic, "circular": len(model.circular), "engine": "built-in calculator"}
    if model.formulas:
        _prefer_libreoffice(data, result, model)
    return result


def _prefer_libreoffice(data: bytes, result: dict, model) -> None:
    """When LibreOffice is installed it is the reference: its values replace ours and it covers every function."""
    from . import office
    lo = office.recalculate(data)
    if lo is None:
        return
    import openpyxl
    values, errors = {}, []
    for (sh, r, c), f in model.formulas.items():
        ref = f"{openpyxl.utils.get_column_letter(c)}{r}"
        v = lo.get((sh, ref))
        if office.is_error(v):
            errors.append(f"{sh}!{ref} {v}  ({f[:60]})")
        elif v is not None:
            values[(sh, ref)] = v
    result.update(values=values, errors=errors, unsupported=[], engine="LibreOffice")


def write_cached(data: bytes, values: dict) -> bytes:
    """Store computed results next to the formulas: <c><f>..</f><v>result</v></c>. Excel recalculates on open anyway."""
    if not values:
        return data
    import openpyxl
    from lxml import etree
    zin = zipfile.ZipFile(io.BytesIO(data))
    # which part is which sheet: workbook.xml order + its relationships
    ns = {"m": "http://schemas.openxmlformats.org/spreadsheetml/2006/main",
          "r": "http://schemas.openxmlformats.org/officeDocument/2006/relationships"}
    wbx = etree.fromstring(zin.read("xl/workbook.xml"))
    rels = etree.fromstring(zin.read("xl/_rels/workbook.xml.rels"))
    target = {r.get("Id"): r.get("Target") for r in rels}
    part_of = {}
    for sh in wbx.find("m:sheets", ns):
        t = target.get(sh.get("{%s}id" % ns["r"]), "")
        part_of[sh.get("name")] = ("xl/" + t.lstrip("/").removeprefix("xl/")) if t else None
    by_part = {}
    for (sh, ref), v in values.items():
        if part_of.get(sh):
            by_part.setdefault(part_of[sh], {})[ref] = v
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as zout:
        for item in zin.infolist():
            raw = zin.read(item.filename)
            if item.filename in by_part:
                root = etree.fromstring(raw)
                want = by_part[item.filename]
                for c in root.iter("{%s}c" % ns["m"]):
                    ref = c.get("r")
                    if ref in want and c.find("m:f", ns) is not None:
                        val = want[ref]
                        v = c.find("m:v", ns)
                        if v is None:
                            v = etree.SubElement(c, "{%s}v" % ns["m"])
                        if isinstance(val, bool):
                            c.set("t", "b")
                            v.text = "1" if val else "0"
                        elif isinstance(val, (int, float)):
                            if "t" in c.attrib:
                                del c.attrib["t"]
                            v.text = repr(float(val)) if not float(val).is_integer() else str(int(val))
                        else:
                            c.set("t", "str")
                            v.text = str(val)
                raw = etree.tostring(root, xml_declaration=True, encoding="UTF-8", standalone=True)
            zout.writestr(item, raw)
    return out.getvalue()


def check_report(result: dict) -> str:
    """One short paragraph for the tool result: what was checked and what to fix."""
    if not result["formulas"]:
        return ""
    bits = [f"Checked ({result.get('engine', 'built-in calculator')}): {result['sheets']} sheet(s), {result['formulas']} formula(s), "
            f"{len(result['errors'])} error(s)."]
    if result["errors"]:
        bits.append("Errors: " + "; ".join(result["errors"][:6]) + (" ..." if len(result["errors"]) > 6 else ""))
    if result["circular"]:
        bits.append(f"{result['circular']} circular reference(s) - break the loop.")
    if result["unsupported"]:
        bits.append("Not evaluated here (Excel will calculate them): " + "; ".join(result["unsupported"][:3]))
    if result["magic"]:
        bits.append("Style: " + "; ".join(result["magic"][:3]))
    return " ".join(bits)

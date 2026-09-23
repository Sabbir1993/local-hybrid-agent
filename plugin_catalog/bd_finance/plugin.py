"""Bangladesh helpers: BDT formatting (lakh/crore) and mobile number validation."""
import re
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP

# third digit of 01X -> operator
_OPERATORS = {"3": "Grameenphone", "7": "Grameenphone", "4": "Banglalink", "9": "Banglalink",
              "5": "Teletalk", "6": "Airtel", "8": "Robi"}
_MOBILE_RE = re.compile(r"^(?:\+?88)?(01[3-9]\d{8})$")

_ONES = ["", "one", "two", "three", "four", "five", "six", "seven", "eight", "nine", "ten",
         "eleven", "twelve", "thirteen", "fourteen", "fifteen", "sixteen", "seventeen",
         "eighteen", "nineteen"]
_TENS = ["", "", "twenty", "thirty", "forty", "fifty", "sixty", "seventy", "eighty", "ninety"]


def register(api):
    api.add_tool(
        "format_bdt",
        _format_bdt,
        "Format an amount as Bangladeshi Taka with lakh/crore grouping (e.g. 12,34,567.00) and in words.",
        {"type": "object",
         "properties": {"amount": {"type": "string", "description": "e.g. 1234567.5"}},
         "required": ["amount"]},
    )
    api.add_tool(
        "validate_bd_mobile",
        _validate_mobile,
        "Validate a Bangladeshi mobile number, normalize it to 8801XXXXXXXXX and detect the operator.",
        {"type": "object",
         "properties": {"number": {"type": "string"}},
         "required": ["number"]},
    )


def _group_lakh(n: int) -> str:
    s = str(n)
    if len(s) <= 3:
        return s
    head, parts = s[:-3], []
    while len(head) > 2:
        parts.insert(0, head[-2:])
        head = head[:-2]
    if head:
        parts.insert(0, head)
    return ",".join(parts) + "," + s[-3:]


def _below_1000(n: int) -> str:
    out = []
    if n >= 100:
        out.append(_ONES[n // 100] + " hundred")
        n %= 100
    if n >= 20:
        out.append(_TENS[n // 10] + ("-" + _ONES[n % 10] if n % 10 else ""))
    elif n:
        out.append(_ONES[n])
    return " ".join(out)


def _words(n: int) -> str:
    if n == 0:
        return "zero"
    out = []
    for size, label in ((10_000_000, "crore"), (100_000, "lakh"), (1000, "thousand")):
        if n >= size:
            q = n // size
            out.append((_words(q) if q >= 1000 else _below_1000(q)) + " " + label)
            n %= size
    if n:
        out.append(_below_1000(n))
    return " ".join(out)


def _format_bdt(args: dict) -> str:
    try:
        amt = Decimal(str(args.get("amount") or "").replace(",", "").strip())
    except InvalidOperation:
        return "error: amount must be a number"
    amt = amt.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    neg = amt < 0
    amt = abs(amt)
    taka = int(amt)
    poisha = int((amt - taka) * 100)
    words = f"{_words(taka)} taka" + (f" and {_words(poisha)} poisha" if poisha else "")
    sign = "-" if neg else ""
    return f"BDT {sign}{_group_lakh(taka)}.{poisha:02d}\n{'minus ' if neg else ''}{words} only"


def _validate_mobile(args: dict) -> str:
    raw = re.sub(r"[\s\-()]", "", str(args.get("number") or ""))
    m = _MOBILE_RE.match(raw)
    if not m:
        return "invalid: expected 01XXXXXXXXX (11 digits, 013-019), optionally prefixed by +88/88"
    local = m.group(1)
    return f"valid: 88{local} ({_OPERATORS.get(local[2], 'unknown operator')})"

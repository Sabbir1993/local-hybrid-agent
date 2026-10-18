"""
core/pan.py - Built-in payment card number (PAN) detection for PCI DSS.

Applied to chat / agent traffic, independent of the admin-defined guard rules:
  - input:        prompts containing a PAN are blocked (input_guard.check)
  - output:       PANs in model responses are masked (output_guard)
  - cloud egress: PANs in anything sent to a cloud provider (history, tool
                  results, KB context) are masked (cloud.CloudClient._prepare)
  - KB reads:     the knowledge base stores uploads verbatim (no guard at upload,
                  by decision); what leaves it is masked at access time.

Config: config/app.json "pci" block, e.g.
  {"pan_input": "block", "pan_output": "mask", "pan_cloud_egress": "mask", "detector": "v2"}
Each pan_* key accepts "off"; anything else keeps the safe default.
"detector": "v1" restores the previous single-separator detector (rollback switch).

Detector v2 (Bangladesh first)
  - Digits are normalised first: Bengali ০-৯, Arabic-Indic, fullwidth, circled and
    superscript digits all count as digits.
  - A candidate is a run of digits joined by up to 6 non-word characters (spaces,
    dashes, dots, newlines from wrapped PDF text, markdown "**", NBSP, zero-width...).
  - Inside a candidate every whole-token window of 13-19 digits is tested, so a CVV
    pasted after the number ("4111 1111 1111 1111 123") or a quantity before it
    ("5 4111...") no longer hides it.
  - A window must pass Luhn and a scheme/prefix check (Visa, Mastercard, Amex, Discover,
    Diners, JCB, UnionPay, RuPay, Maestro, Mir...). Thousands-separated amounts
    ("4,123,456,789,012,345") are rejected by the group-size rule, and BD mobile numbers,
    NIDs, TrxIDs, routing/account numbers and timestamps fall out of the prefix/length rules.
  - Card context (card / cvv / expiry / কার্ড / সিভিভি ... within a few words, but not
    "NID card" / "SIM card") loosens the check: a 12-19 digit run is masked even if Luhn fails
    (a mistyped card number still leaks), and a 13-digit window is accepted.
  - A CVV/CVC next to its keyword (English or Bangla) is masked/blocked too.
"""

import re
import unicodedata

# Characters that count as a decimal digit: any Unicode Nd (\d, includes Bengali and fullwidth)
# plus superscript, subscript, circled and dingbat digits that unicodedata.digit() understands.
_DIG = "\\d\u00b2\u00b3\u00b9\u2070\u2074-\u2079\u2080-\u2089\u2460-\u2468\u2776-\u277e"
_D = f"[{_DIG}]"

# Candidate run: >=13 digits, at most 6 non-word characters between two digits.
PAN_PATTERN = rf"(?<![{_DIG}]){_D}(?:[\W_]{{0,6}}{_D}){{12,}}(?![{_DIG}])"
PAN_RX = re.compile(PAN_PATTERN)

# v1 (kept for the "detector": "v1" rollback switch)
_V1_SEP = "[ \t.\u2013\u00a0\u200b-\u200d\u2060\ufeff-]"
_V1_RX = re.compile(r"(?<!\d)(?:\d" + _V1_SEP + r"?){12,18}\d(?!\d)")

_TOKEN_RX = re.compile(f"{_D}+")

# Streaming holdback: a PAN (or a CVV and its keyword) can still be growing inside the trailing
# run of digits / separators, so output_guard holds that run back until it settles.
def _partial(word: str) -> str:
    """Regex for `word` or any prefix of it, so a keyword still being streamed is held back too."""
    out = ""
    for ch in reversed(word):
        out = f"{re.escape(ch)}(?:{out})?" if out else re.escape(ch)
    return out


_CVV_WORDS = ("cvv", "cvc", "csc", "cid", "সিভিভি", "সিভিসি")
_CVV_FULL = "|".join(re.escape(w) for w in _CVV_WORDS)
_CVV_PARTIAL = "|".join(_partial(w) for w in _CVV_WORDS)
TRAILING_RX = re.compile(
    rf"(?:(?<![A-Za-z])(?i:{_CVV_FULL})[^{_DIG}]{{0,30}}[{_DIG}\W_]*"      # keyword seen: hold until its digits settle
    rf"|(?<![A-Za-z])(?i:{_CVV_PARTIAL})"                                  # keyword still arriving
    rf"|[\W_]{{0,6}}{_D}(?:[\W_]{{0,6}}{_D}){{0,40}}[\W_]{{0,6}})$")       # trailing digit run, may still grow

BLOCK_MESSAGE = (
    "Your message appears to contain a payment card number. Card numbers must "
    "not be entered into chat or agent tasks (PCI DSS). Remove or mask it "
    "(e.g. keep only the last 4 digits) and try again."
)
RULE_NAME = "Payment card number (PAN)"

# --- card context --------------------------------------------------------------------------
_CTX_WORDS = (
    r"card|visa|master\s?card|amex|american\s+express|unionpay|union\s+pay|rupay|jcb|diners|nexus|q-?cash|"
    r"cvv2?|cvc2?|csc|expiry|expiration|exp\.?\s*date|pan\b|"
    r"কার্ড|ক্রেডিট|ডেবিট|সিভিভি|সিভিসি|মেয়াদ|ভিসা|মাস্টার"
)
_CTX_RX = re.compile(rf"(?i)(?:{_CTX_WORDS})")
# "NID card", "SIM card", "ID card" are not payment cards
_NOT_CARD_RX = re.compile(
    r"(?i)(?:nid|n\.i\.d|id|sim|smart|gift|sd|memory|business|visiting|ration|"
    r"এনআইডি|আইডি|সিম|স্মার্ট|পরিচয়|ভোটার|রেশন)[\s\-]*(?:card|কার্ড|পত্র)\s*$")


def _has_context(text: str, start: int, end: int) -> bool:
    before = text[max(0, start - 48):start]
    after = text[end:end + 24]
    for m in _CTX_RX.finditer(before):
        if not _NOT_CARD_RX.search(before[:m.end()]):
            return True
    return bool(_CTX_RX.search(after))


# --- CVV / CVC (sensitive authentication data, never stored) ---------------------------------
CVV_RX = re.compile(
    rf"(?i)(?<![\w])(?:cvv2?|cvc2?|csc|cid|সিভিভি|সিভিসি)(?![a-z])"
    rf"[\s:=#\-]{{0,6}}(?:(?:is|code|number|no\.?|নম্বর|কোড|হলো|হল)[\s:=#\-]{{0,6}})?"
    rf"({_D}{{3,4}})(?![{_DIG}])")


def mask_cvv(m: "re.Match") -> str:
    s = m.group(0)
    g = m.start(1) - m.start(0)
    return s[:g] + "***" + s[m.end(1) - m.start(0):]


def contains_cvv(text) -> bool:
    return bool(text) and CVV_RX.search(str(text)) is not None


# --- config -------------------------------------------------------------------------------------
def _cfg() -> dict:
    from .small_model import APP_CONFIG
    c = APP_CONFIG.get("pci")
    return c if isinstance(c, dict) else {}


def enabled(key: str) -> bool:
    """pan_input / pan_output / pan_cloud_egress: on unless set to "off"."""
    return str(_cfg().get(key, "on")).strip().lower() not in ("off", "false", "0")


def _v1() -> bool:
    return str(_cfg().get("detector", "v2")).strip().lower() == "v1"


# --- validation -----------------------------------------------------------------------------------
def _luhn_ok(digits: str) -> bool:
    total = 0
    for i, ch in enumerate(reversed(digits)):
        d = ord(ch) - 48
        if i % 2:
            d *= 2
            if d > 9:
                d -= 9
        total += d
    return total % 10 == 0


def _digits(candidate: str) -> str:
    """ASCII digits only: separators dropped, any Unicode digit (e.g. Bengali) normalized."""
    out = []
    for ch in candidate:
        if ch.isdigit():
            d = unicodedata.digit(ch, None)
            if d is not None:
                out.append(str(d))
    return "".join(out)


def _prefix_ok(d: str) -> bool:
    """Issuer ranges that exist in the wild (Visa, Mastercard, Amex, Discover, Diners, JCB, UnionPay,
    RuPay, Maestro, Mir, Troy). Anything else is an ID, phone, timestamp or account number."""
    c = d[0]
    if c in "456":                       # Visa 4; Mastercard 5 / Maestro 50,56-58 / Discover, UnionPay, RuPay, Maestro 6
        return True
    if c == "3":                         # Amex 34/37, JCB 35, Diners 30/36/38/39
        return d[:2] in ("30", "34", "35", "36", "37", "38", "39")
    if c == "2":                         # Mastercard 2221-2720, Mir 2200-2204
        return d[:2] in ("22", "23", "24", "25", "26", "27")
    if c == "8":                         # UnionPay 81, RuPay 81/82
        return d[:2] in ("81", "82")
    if c == "9":                         # Troy 9792
        return d[:4] == "9792"
    return False


def _strict_ok(d: str, ctx: bool) -> bool:
    n = len(d)
    if n < 13 or n > 19 or len(set(d)) == 1:
        return False
    if n == 13 and not ctx:              # 13-digit Visa is obsolete; 13-digit NIDs/IDs are not cards
        return False
    return _prefix_ok(d) and _luhn_ok(d)


def _spans(cand: str, full: str, base: int) -> list:
    """(start, end, digits) of each PAN found inside candidate `cand` (offsets relative to cand)."""
    toks = [(m.start(), m.end(), _digits(m.group(0))) for m in _TOKEN_RX.finditer(cand)]
    ctx = _has_context(full, base, base + len(cand))
    out = []
    i, nt = 0, len(toks)
    while i < nt:
        found = None
        loose = None
        total = 0
        for j in range(i, nt):
            total += len(toks[j][2])
            if total > 19:
                break
            if j > i and (len(toks[i][2]) < 4 or any(len(toks[k][2]) < 4 for k in range(i + 1, j))
                          or len(toks[j][2]) < 3):
                continue
            d = "".join(t[2] for t in toks[i:j + 1])
            if len(d) >= 12:
                if _strict_ok(d, ctx):
                    found = j
                    break
                if ctx and len(d) <= 19 and len(set(d)) > 1:
                    loose = j
        if found is None:
            found = loose
        if found is not None:
            d = "".join(t[2] for t in toks[i:found + 1])
            out.append((toks[i][0], toks[found][1], d))
            i = found + 1
        else:
            i += 1
    return out


def _mask_candidate(m: "re.Match") -> tuple:
    s = m.group(0)
    if _v1():
        masked = _V1_RX.sub(lambda x: f"[card ****{_digits(x.group(0))[-4:]}]" if _is_pan_v1(x.group(0)) else x.group(0), s)
        return masked, int(masked != s)
    spans = _spans(s, m.string, m.start())
    if not spans:
        return s, 0
    out, pos = [], 0
    for a, b, d in spans:
        out.append(s[pos:a])
        out.append(f"[card ****{d[-4:]}]")
        pos = b
    out.append(s[pos:])
    return "".join(out), len(spans)


def _is_pan_v1(candidate: str) -> bool:
    digits = _digits(candidate)
    if not 13 <= len(digits) <= 19:
        return False
    if digits[0] not in "23456":
        return False
    if len(set(digits)) == 1:
        return False
    return _luhn_ok(digits)


def is_pan(candidate: str) -> bool:
    """True if `candidate` (a PAN_RX match, or any text) contains a card number."""
    if _v1():
        return _is_pan_v1(candidate)
    return any(_spans(m.group(0), candidate, m.start()) for m in PAN_RX.finditer(candidate))


_ATTACH_NAME_RX = re.compile(
    r"^(--- (?:FILE|IMAGE|END)(?::)? )[^\n]{1,200}?(\.[A-Za-z0-9]{1,8})( ---)$", re.M)


def blank_attachment_names(text: str) -> str:
    """Hide the file name in the '--- FILE: x.ext ---' markers the chat adds around attachments.
    Phone/messenger names like photo_6210835229476328575_y.jpg carry long digit runs that pass the
    Luhn check by chance (~1 in 10), which blocked harmless photos. The file's own content is
    still scanned; only a name that ends in a short extension is skipped."""
    return _ATTACH_NAME_RX.sub(r"\1[name]\2\3", text) if isinstance(text, str) and "--- " in text else text


def contains_pan(text) -> bool:
    if not text:
        return False
    text = str(text)
    if _v1():
        return any(_is_pan_v1(m.group(0)) for m in _V1_RX.finditer(text))
    for m in PAN_RX.finditer(text):
        if _spans(m.group(0), text, m.start()):
            return True
    return contains_cvv(text)


def mask_match(m: "re.Match") -> str:
    """re.sub callback: mask the card number(s) inside a PAN_RX match to their last 4 digits."""
    return _mask_candidate(m)[0]


def mask_pans(text: str) -> tuple[str, int]:
    """Returns (masked_text, number_of_pans_masked)."""
    if not text or not isinstance(text, str):
        return text, 0
    n = 0

    def _sub(m):
        nonlocal n
        out, k = _mask_candidate(m)
        n += k
        return out

    text = PAN_RX.sub(_sub, text)
    if not _v1():
        text, k = CVV_RX.subn(mask_cvv, text)
        n += k
    return text, n


def mask_messages(messages) -> int:
    """Mask PANs in-place in an OpenAI-style messages list (string content or
    content parts). Returns the number of PANs masked."""
    n = 0
    for msg in messages or []:
        if not isinstance(msg, dict):
            continue
        c = msg.get("content")
        if isinstance(c, str):
            msg["content"], k = mask_pans(c)
            n += k
        elif isinstance(c, list):
            for part in c:
                if isinstance(part, dict) and isinstance(part.get("text"), str):
                    part["text"], k = mask_pans(part["text"])
                    n += k
        # assistant tool calls carry model-written arguments (JSON strings)
        for tc in msg.get("tool_calls") or []:
            fn = tc.get("function") if isinstance(tc, dict) else None
            if isinstance(fn, dict) and isinstance(fn.get("arguments"), str):
                fn["arguments"], k = mask_pans(fn["arguments"])
                n += k
    return n

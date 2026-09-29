import re
from .constants import cfg

_EMAIL_RX = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+")
# BD mobiles (01XXXXXXXXX / +8801XXXXXXXXX) and other international numbers
_PHONE_RX = re.compile(r"(?<![\w])(?:\+?880[\s-]?|0)1[3-9]\d{2}[\s-]?\d{6}(?!\d)|\+\d[\d\s-]{8,14}\d")
_LONG_DIGITS_RX = re.compile(r"(?<!\d)\d{10,}(?!\d)")     # account / wallet / transaction ids


def redact_query(q: str) -> tuple:
    """Returns (query, [kinds redacted]). Queries go to third-party engines."""
    if not cfg().get("redact_query_pii", True):
        return q, []
    kinds = []
    for kind, rx in (("email", _EMAIL_RX), ("phone number", _PHONE_RX), ("account-like number", _LONG_DIGITS_RX)):
        q2 = rx.sub(" ", q)
        if q2 != q:
            kinds.append(kind)
            q = q2
    return " ".join(q.split()), kinds

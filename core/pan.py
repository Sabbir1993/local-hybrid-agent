"""
core/pan.py - Built-in payment card number (PAN) detection for PCI DSS.

Applied to chat / agent traffic, independent of the admin-defined guard rules:
  - input:        prompts containing a PAN are blocked (input_guard.check)
  - output:       PANs in model responses are masked (output_guard)
  - cloud egress: PANs in anything sent to a cloud provider (history, tool
                  results, KB context) are masked (cloud.CloudClient._prepare)

Knowledge-base uploads are deliberately NOT scanned (knowledge_ingest).

Config: config/app.json "pci" block, e.g.
  {"pan_input": "block", "pan_output": "mask", "pan_cloud_egress": "mask"}
Each key accepts "off"; anything else keeps the safe default.
"""

import re
import unicodedata

# 13-19 digits, optionally grouped by one separator between digits: space, tab,
# hyphen, dot, en dash, NBSP, or a zero-width char (pasted from PDFs / web pages).
# \d matches any Unicode decimal digit, so Bengali ০-৯ numerals are caught too.
_SEP = "[ \t.\u2013\u00a0\u200b-\u200d\u2060\ufeff-]"
PAN_PATTERN = r"(?<!\d)(?:\d" + _SEP + r"?){12,18}\d(?!\d)"
PAN_RX = re.compile(PAN_PATTERN)

BLOCK_MESSAGE = (
    "Your message appears to contain a payment card number. Card numbers must "
    "not be entered into chat or agent tasks (PCI DSS). Remove or mask it "
    "(e.g. keep only the last 4 digits) and try again."
)
RULE_NAME = "Payment card number (PAN)"


def _cfg() -> dict:
    from .small_model import APP_CONFIG
    c = APP_CONFIG.get("pci")
    return c if isinstance(c, dict) else {}


def enabled(key: str) -> bool:
    """pan_input / pan_output / pan_cloud_egress: on unless set to "off"."""
    return str(_cfg().get(key, "on")).strip().lower() not in ("off", "false", "0")


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
    return "".join(str(unicodedata.digit(ch)) for ch in candidate if ch.isdigit())


def is_pan(candidate: str) -> bool:
    digits = _digits(candidate)
    if not 13 <= len(digits) <= 19:
        return False
    if digits[0] not in "23456":          # Mastercard 2/5, Amex 3, Visa 4, Discover/UnionPay 6
        return False
    if len(set(digits)) == 1:
        return False
    return _luhn_ok(digits)


def contains_pan(text) -> bool:
    if not text:
        return False
    return any(is_pan(m.group(0)) for m in PAN_RX.finditer(str(text)))


def mask_match(m: "re.Match") -> str:
    """re.sub callback: mask a Luhn-valid PAN to its last 4 digits."""
    s = m.group(0)
    if not is_pan(s):
        return s
    return f"[card ****{_digits(s)[-4:]}]"


def mask_pans(text: str) -> tuple[str, int]:
    """Returns (masked_text, number_of_pans_masked)."""
    if not text or not isinstance(text, str):
        return text, 0
    n = 0

    def _sub(m):
        nonlocal n
        out = mask_match(m)
        if out != m.group(0):
            n += 1
        return out

    return PAN_RX.sub(_sub, text), n


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

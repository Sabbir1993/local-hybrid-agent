"""Code-level filter for what may be saved to the agent's long-term memory.

The model is told not to store secrets, but a prompt is not a control: every memory write goes
through check() first and is refused when it looks like a credential, a payment card / bank
detail or a government ID. The reason names the *kind* of data, never the matched text, so a
refusal cannot leak the value into logs or the model's context.

Payment card numbers are always blocked here, whatever the `pci` toggles say: memory is
long-lived and is re-sent to the model every session.
"""
import re
from typing import Optional

from . import pan

_I = re.IGNORECASE

_RULES: list = [
    ("a private key", re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----")),
    ("an API key or token", re.compile(
        r"\b(?:sk|pk|rk)[-_](?:live|test|proj|ant)?[-_]?[A-Za-z0-9]{16,}"
        r"|\bAKIA[0-9A-Z]{16}\b|\bgh[pousr]_[A-Za-z0-9]{30,}"
        r"|\bxox[abprs]-[A-Za-z0-9-]{10,}|\bAIza[0-9A-Za-z_-]{30,}"
        r"|\beyJ[A-Za-z0-9_-]{8,}\.eyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}"
        r"|\bBearer\s+[A-Za-z0-9._~+/=-]{20,}")),
    ("a password or secret", re.compile(
        r"\b(?:password|passwd|pwd|passcode|pin|secret|credential)s?\b"
        r"\s*(?:is|are|=|:|->)\s*[\"']?(?!not\b|the\b|a\b|an\b|stored\b|required\b|set\b)\S{4,}", _I)),
    ("an API key or token", re.compile(
        r"\b(?:api[_ -]?key|access[_ -]?key|auth[_ -]?token|token|key)s?\b\s*(?:is|=|:|->)\s*[\"']?"
        r"[A-Za-z0-9_\-./+=]{16,}", _I)),
    ("a card verification code", re.compile(r"\b(?:cvv2?|cvc2?|cid)\b\s*(?:is|=|:)?\s*\d{3,4}\b", _I)),
    ("a bank account detail", re.compile(
        r"\b(?:account|a/c|acct|iban|routing|swift|sort\s*code)\b[^\n]{0,24}?[A-Z0-9][A-Z0-9 -]{7,}\d", _I)),
    ("an IBAN", re.compile(r"\b[A-Z]{2}\d{2}[A-Z0-9]{11,30}\b")),
    ("a government or tax ID", re.compile(
        r"\b(?:nid|national\s*id|passport|ssn|social\s*security|tin|etin|driving\s*licen[cs]e|birth\s*certificate)\b"
        r"[^\n]{0,24}?\b[A-Z]{0,3}\d[\dA-Z -]{6,}", _I)),
]


def check(text: str) -> Optional[str]:
    """None when the text is fine, else a short description of the kind of data found."""
    if not isinstance(text, str) or not text.strip():
        return None
    if pan.contains_pan(text):
        return "a payment card number"
    for kind, rx in _RULES:
        if rx.search(text):
            return kind
    return None


def refusal(kind: str) -> str:
    return (f"error: this text looks like it contains {kind}. Memory never stores credentials, payment "
            "or bank details, or ID numbers. Leave that part out and save only what is safe to keep.")

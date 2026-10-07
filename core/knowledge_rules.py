"""core/knowledge_rules.py - which knowledge chunks must stay on local models.

Layer 2 of the cloud-readability policy (layer 1 = per-source category / switch, see
knowledge_access.py). Even inside a source a cloud model may read, a chunk that matches a
sensitive-content rule (salary, IDs, bank accounts, contact details, admin-added rules) is held back:
the run is answered by a local model instead. Rules only ever restrict; none can approve a chunk.
Checked at retrieval time, so editing a rule applies immediately - no reindex.
"""

import re
from functools import lru_cache
from typing import Optional

MAX_PATTERN_LEN = 300
KINDS = ("keywords", "regex")


@lru_cache(maxsize=256)
def _compile(kind: str, pattern: str) -> "re.Pattern":
    if kind == "keywords":
        words = [w.strip() for w in pattern.split(",") if w.strip()]
        if not words:
            raise ValueError("add at least one keyword")
        return re.compile(r"(?<!\w)(?:" + "|".join(re.escape(w) for w in words) + r")(?!\w)", re.I)
    if kind == "regex":
        return re.compile(pattern)
    raise ValueError("kind must be 'keywords' or 'regex'")


def validate(kind: str, pattern: str) -> Optional[str]:
    """None when the rule is usable, else a plain-language reason."""
    if kind not in KINDS:
        return "kind must be 'keywords' or 'regex'"
    if not pattern or not pattern.strip():
        return "the pattern is empty"
    if len(pattern) > MAX_PATTERN_LEN:
        return f"the pattern is too long (max {MAX_PATTERN_LEN} characters)"
    try:
        rx = _compile(kind, pattern.strip() if kind == "keywords" else pattern)
    except (re.error, ValueError) as e:
        return f"invalid pattern: {e}"
    if rx.search(""):
        return "the pattern matches empty text, so it would flag everything"
    return None


def _rules() -> list:
    from . import auth_db
    try:
        return auth_db.list_knowledge_rules(enabled_only=True)
    except Exception:
        return []


def sensitive_reason(text, rules: Optional[list] = None) -> Optional[str]:
    """Name of the first rule this text trips, else None."""
    if not text or not isinstance(text, str):
        return None
    for r in (rules if rules is not None else _rules()):
        try:
            if _compile(r["kind"], r["pattern"]).search(text):
                return r["name"]
        except (re.error, ValueError):
            continue                      # a broken rule must not open or crash anything
    from . import pan, secrets
    if pan.contains_pan(text):
        return "Payment card number"
    hit = secrets.scan(text, any_cloud_lane=True)
    if hit:
        return f"Credential: {hit['label']}"
    return None


def matches(text: str) -> list:
    """Every rule name the text trips (for the admin 'test a snippet' box)."""
    out = []
    for r in _rules():
        try:
            if _compile(r["kind"], r["pattern"]).search(text or ""):
                out.append(r["name"])
        except (re.error, ValueError):
            pass
    from . import pan
    if pan.contains_pan(text):
        out.append("Payment card number")
    return out

"""core/kb_coverage.py - how much of a question the knowledge base (KB) answers, and what that means
for the web.

Retrieval already runs before the model (local, free). Its scores say whether the KB is likely to cover
the question, so the web is offered only for the part the KB cannot answer:

    full     the KB covers it             -> no web tools (0 extra tokens) unless the user asked for the web
    partial  the KB covers part of it     -> lean web hint, small call budget, "search only what is missing"
    none     nothing relevant in the KB   -> web only on explicit web intent (today's behaviour)

Between "clearly full" and "clearly partial" sits an ambiguous band; there one tiny call to the small local
model decides (same plumbing as the web query rewriter), and any failure falls back to "partial".
The web budget scales with the reasoning effort / Deep mode (config chat.web_budget).
"""

import asyncio
import re
from typing import Optional

TIERS = ("low", "medium", "high", "deep")

# calls allowed per (row, tier). rows: "full" (KB covers it; only deep verifies), "partial" (gap filling),
# "open" (explicit web intent / nothing in the KB). Overridable in config/app.json chat.web_budget.
DEFAULT_BUDGET = {
    "full": {"low": 0, "medium": 0, "high": 0, "deep": 3},
    "partial": {"low": 2, "medium": 3, "high": 6, "deep": 12},
    "open": {"low": 4, "medium": 8, "high": 12, "deep": 20},
}

_JUDGE_PROMPT = (
    "You check whether retrieved company documents are enough to answer a question. Reply with ONE word:\n"
    "FULL    - the documents contain everything needed.\n"
    "PARTIAL - they cover only part (the rest needs outside or public information).\n"
    "NONE    - they are not relevant.")


def _kcfg() -> dict:
    from .small_model import APP_CONFIG
    c = APP_CONFIG.get("knowledge")
    return c if isinstance(c, dict) else {}


def gap_fill_enabled() -> bool:
    return str(_kcfg().get("web_gap_fill", True)).lower() not in ("false", "0", "off", "no")


def tier(effort: Optional[str], deep: bool) -> str:
    """low | medium | high | deep from the composer's effort level (core/reasoning.py) and Deep mode."""
    if deep or effort == "extra":
        return "deep"
    if effort == "high":
        return "high"
    if effort in ("none", "low"):
        return "low"
    return "medium"


def web_budget(cov: str, tier_name: str, explicit: bool = False) -> int:
    """Web calls for this run. `explicit` = the user clearly asked for the web (URL, 'search', 'latest'...)."""
    from .small_model import APP_CONFIG
    row = "open" if (explicit or cov == "none") else ("partial" if cov == "partial" else "full")
    custom = (APP_CONFIG.get("chat") or {}).get("web_budget")
    custom = custom if isinstance(custom, dict) else {}
    t = tier_name if tier_name in TIERS else "medium"
    try:
        v = (custom.get(row) or {}).get(t)
        return max(0, min(100, int(v))) if v is not None else DEFAULT_BUDGET[row][t]
    except (TypeError, ValueError):
        return DEFAULT_BUDGET[row][t]


def rounds_for(budget: int, configured: int) -> int:
    """A web budget is useless if the tool loop ends first (it stops two turns before its limit)."""
    return max(int(configured), int(budget) + 4) if budget > 0 else int(configured)


def classify(hits) -> str:
    """full | partial | none | ambiguous, from retrieval scores only (never reads the text)."""
    if not hits:
        return "none"
    k = _kcfg()
    full_cos = float(k.get("full_cos", 0.70))
    good_cos = float(k.get("auto_inject_cos", 0.55))
    cos = sorted((float(h.get("cos") or 0.0) for h in hits), reverse=True)
    good = sum(1 for c in cos if c >= good_cos)
    if cos[0] >= full_cos and good >= 2:
        return "full"
    if cos[0] < good_cos:
        return "partial"              # injected on a company keyword, but the match itself is weak
    return "ambiguous"


async def judge(query: str, hits, blocked: bool = False) -> Optional[str]:
    """FULL / PARTIAL / NONE from the small local model, or None when it isn't available.
    When the KB text is withheld (blocked) the judge sees titles and scores only."""
    try:
        from . import lanes
        route = lanes.targets("search_rewrite")
        t = route[0] if route else None
        if t is None or t.lane == "main" or not t.is_up():
            return None
        lines = []
        for h in (hits or [])[:6]:
            head = f"- [{h.get('title') or 'source'}] match {float(h.get('cos') or 0):.2f}"
            if not blocked:
                head += ": " + " ".join(str(h.get("text") or "").split())[:240]
            lines.append(head)
        client = await t.client()
        r = await asyncio.wait_for(client.post("/v1/chat/completions", json={
            "messages": [{"role": "system", "content": _JUDGE_PROMPT},
                         {"role": "user", "content": f"Question: {query[:600]}\n\nDocuments:\n" + "\n".join(lines)}],
            "max_tokens": 8, "temperature": 0.0}), timeout=8)
        out = ((r.json().get("choices") or [{}])[0].get("message") or {}).get("content") or ""
        out = re.sub(r"<think>.*?</think>", "", out, flags=re.S).strip().upper()
        for word, cov in (("PARTIAL", "partial"), ("FULL", "full"), ("NONE", "none")):
            if out.startswith(word):
                return cov
    except Exception:
        pass
    return None


async def resolve(query: str, hits, blocked: bool = False) -> str:
    """full | partial | none. Deterministic at the extremes; the judge only runs in the ambiguous band."""
    cov = classify(hits)
    if cov != "ambiguous":
        return cov
    return await judge(query, hits, blocked) or "partial"


def decide_web(cov: str, tier_name: str, web_on: bool, explicit: bool) -> dict:
    """What to offer. -> {"web": bool, "lean": bool, "budget": int}. `web_on` = the user's web switch and
    the web capability; `explicit` = the wording is a clear web request."""
    if not web_on:
        return {"web": False, "lean": False, "budget": 0}
    budget = web_budget(cov, tier_name, explicit)
    if explicit:
        return {"web": True, "lean": False, "budget": budget}
    if cov == "partial":
        return {"web": budget > 0, "lean": True, "budget": budget}
    if cov == "full":                 # only deep mode verifies/extends a fully covered answer
        return {"web": budget > 0, "lean": True, "budget": budget}
    if tier_name == "deep":           # Deep research with nothing in the KB: its whole method is web research
        return {"web": True, "lean": False, "budget": budget}
    return {"web": False, "lean": False, "budget": 0}


def query_key(query: str) -> str:
    """Order- and inflection-insensitive key so a re-worded repeat of a search is recognised."""
    from .memory.constants import _normalize_query_words
    return " ".join(sorted(set(_normalize_query_words(query or "", limit=12))))


def cap_web_result(text, calls_so_far: int, soft_after: int = 6, soft_chars: int = 6000):
    """A long research run keeps its context: once `soft_after` web calls are in, each further result is trimmed."""
    if isinstance(text, str) and calls_so_far >= soft_after and len(text) > soft_chars:
        return text[:soft_chars] + "\n...[trimmed to keep the research run within context]"
    return text

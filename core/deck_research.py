"""Research budget for building a deck or PDF.

A deck written from the model's memory comes out as three vague bullets per slide. A good one needs figures, dates and
names that were READ somewhere, so after the user has answered the questionnaire the build gets its own, larger research
budget (searches and full-page reads) and a thinking budget, independent of the chat's normal tier: at the default "low"
effort the chat allows 4 web calls and ~1k thinking tokens, which is a quick answer, not a deck.

All knobs live under `chat` in config/app.json:
  deck_research_calls   web calls for a deck / PDF build (default 10; 0 turns the extra budget off)
  deck_repeat_penalty   repeat penalty while writing the deck (default 1.0; the chat's 1.15 garbles URLs and names)
  deck_max_tokens       cap for ONE turn while building (default 8000; 0 = no cap)
  deck_effort           reasoning effort floor while building (default "medium"; the cap reasoning_effort.doc_build_ceiling
                        still applies afterwards)
"""
import re
from typing import Optional

from . import reasoning

DEFAULT_CALLS = 10
BUILD_TURNS = 12          # tool rounds kept for doc_create / doc_append after the research


def _cfg(app_cfg: Optional[dict]) -> dict:
    return (app_cfg or {}).get("chat") or {}


def calls(app_cfg: Optional[dict]) -> int:
    try:
        return max(0, min(40, int(_cfg(app_cfg).get("deck_research_calls", DEFAULT_CALLS))))
    except (TypeError, ValueError):
        return DEFAULT_CALLS


def effort_for(effort: Optional[str], app_cfg: Optional[dict]) -> Optional[str]:
    """The composer's effort, raised to the deck floor (never lowered)."""
    floor = str(_cfg(app_cfg).get("deck_effort", "medium"))
    if effort not in reasoning.LEVELS or floor not in reasoning.LEVELS:
        return effort
    return floor if reasoning.LEVELS.index(effort) < reasoning.LEVELS.index(floor) else effort


def web_plan(app_cfg: Optional[dict], brief_phase: str, web_on: bool, coverage: str, has_web: bool, lean: bool,
             budget: Optional[int]) -> dict:
    """-> {has_web, lean, budget} for the build phase of a deck / PDF. Outside it, nothing changes.

    The web is used when the user's web switch is on and the Knowledge Base does not already cover the topic
    ("full"); the research budget is at least `deck_research_calls`."""
    keep = {"has_web": has_web, "lean": lean, "budget": budget}
    n = calls(app_cfg)
    if brief_phase != "build" or not web_on or n <= 0 or coverage == "full":
        return keep
    return {"has_web": True, "lean": False, "budget": max(int(budget or 0), n)}


def build_limits(app_cfg: Optional[dict], phase: str) -> dict:
    """Sampling limits for the turns that WRITE the deck. -> {repeat_penalty, max_tokens} (None = leave as configured).

    The chat default repeat penalty (1.15) punishes every repeated token, and a deck is full of repeats - URLs, source names,
    JSON-like slide syntax. The model then spells words wrong ("Decemberber", "wikipedia.wikipedia.wikipedia") and can run
    away into a loop, which on a local GPU is half an hour and 25k tokens for one doc_append. While building: penalty 1.0
    and a hard cap on the tokens of one turn (a 3-slide doc_append is ~1.5k)."""
    if phase != "build":
        return {"repeat_penalty": None, "max_tokens": None}
    chat = _cfg(app_cfg)
    try:
        rp = float(chat.get("deck_repeat_penalty", 1.0))
    except (TypeError, ValueError):
        rp = 1.0
    try:
        mt = int(chat.get("deck_max_tokens", 8000))
    except (TypeError, ValueError):
        mt = 8000
    return {"repeat_penalty": rp if rp > 0 else None, "max_tokens": mt if mt > 0 else None}


def clamp_tokens(requested, cap: Optional[int]):
    """The request's max_tokens (-1 / None / 0 = unlimited), lowered to `cap` when there is one."""
    if not cap:
        return requested
    if requested in (None, -1, 0) or int(requested) > cap:
        return cap
    return requested


def web_cap(phase: str, app_cfg: Optional[dict] = None) -> tuple:
    """(soft_after, soft_chars) for trimming web results: a deck's research keeps its context small, because every later
    turn (and every slide written) is processed on top of it."""
    soft_chars = 6000
    if app_cfg:
        try:
            soft_chars = int((app_cfg.get("chat") or {}).get("web_soft_chars", 6000))
        except (TypeError, ValueError):
            soft_chars = 6000
    return (1, 4500) if phase == "build" else (6, soft_chars)


def turns_for(budget: int, configured: int) -> int:
    """Tool rounds for research plus the build itself (the loop stops two rounds before its limit)."""
    return max(int(configured), int(budget) + BUILD_TURNS)


PARALLEL_FACETS = ("overview key facts", "statistics and figures", "history and timeline",
                   "latest developments", "examples and notable cases")


def parallel_on(app_cfg: Optional[dict]) -> bool:
    return _cfg(app_cfg).get("deck_parallel_research", True) is not False


def parallel_queries(topic: str, budget: int, app_cfg: Optional[dict] = None) -> list:
    """Searches the server runs AT THE SAME TIME before the model starts writing a deck (tool-side parallelism: web
    I/O overlaps, no extra LLM load). The model chose one search at a time, line by line; this front-loads the breadth
    and leaves the rest of the budget for the model's own follow-ups. Up to 4, never more than half the budget."""
    topic = topic_of(topic)
    n = min(len(PARALLEL_FACETS), 4, int(budget or 0) // 2)
    if not topic or n < 2 or not parallel_on(app_cfg):
        return []
    return [f"{topic} {f}" for f in PARALLEL_FACETS[:n]]


_ASK_HEAD = re.compile(r"^.*?(?:\b(?:ppt|pptx|presentation|deck|slides?|pdf|report|guide|document)s?\b\s*)+(?:(?:about|of|on|for|regarding|covering)\s+)?",
                       re.I)
_ASK_TAIL = re.compile(r"\s+(?:with|using|including|and add|and include|that has|which has|having|in (?:\d+|a few) )\b.*$", re.I)


def topic_of(request: str) -> str:
    """'Make a deep informative PPT of Dhaka with images from web' -> 'Dhaka' (a search needs the subject, not the order)."""
    t = " ".join(str(request or "").split())
    m = _ASK_HEAD.match(t)
    if m and m.end() < len(t):
        t = t[m.end():]
    t = _ASK_TAIL.sub("", t).strip(" .?!,")
    return t[:90]


def project_limits(app_cfg: Optional[dict]) -> dict:
    """Sampling limits for a website project turn: same as a deck build (penalty 1.0 - HTML / CSS / JS are full of repeats) and a
    cap on one turn (a 150-line piece is ~3k tokens; a loop used to run 25k before the guard cut it)."""
    chat = _cfg(app_cfg)
    try:
        mt = int(chat.get("project_max_tokens", 6000))
    except (TypeError, ValueError):
        mt = 6000
    return {"repeat_penalty": 1.0, "max_tokens": mt if mt > 0 else None}


def build_dry(app_cfg: Optional[dict]) -> dict:
    """DRY sampler fields for the turns that write a document, deck or site. DRY penalises a long exact sequence that is about
    to repeat (the loops the guard cuts: "andandand", the same URL again) without punishing single tokens the way the
    repeat penalty does, which spells words wrong. chat.build_dry.multiplier 0 turns it off."""
    cfg = (_cfg(app_cfg).get("build_dry") or {}) if isinstance(_cfg(app_cfg).get("build_dry"), dict) else {}
    try:
        mult = float(cfg.get("multiplier", 0.6))
    except (TypeError, ValueError):
        mult = 0.6
    if mult <= 0:
        return {}
    return {"dry_multiplier": mult, "dry_base": float(cfg.get("base", 1.75)),
            "dry_allowed_length": int(cfg.get("allowed_length", 10)), "dry_penalty_last_n": int(cfg.get("penalty_last_n", 1024))}

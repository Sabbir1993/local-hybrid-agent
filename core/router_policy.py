"""Agent-loop routing rules (executor vs main lane, when to escalate).

The keyword lists and limits used to be hard-coded in routes/agent.py; they now
live in APP_CONFIG["router"] (config/app.json) with the old values as defaults,
so behavior is unchanged until an admin edits them -- by hand or by applying a
core.router_tuner suggestion via POST /control/router.

Matching is plain substring on the lowercased query, exactly like before
("add" also matches "address"); the tuner works on the categories below, so a
noisy keyword shows up as a high escalation rate for its category.
"""

import re

DEFAULT_CREATION_KEYWORDS = ["make", "create", "generate", "write", "build", "code", "add", "fix",
                             "html", "script", "page"]
DEFAULT_ACTION_KEYWORDS = ["run", "install", "test", "check", "exec", "open", "read", "view",
                           "find", "grep"]
DEFAULT_REFUSAL_PHRASES = ["i cannot", "i can't", "i am unable", "as an ai", "i don't have access"]
DEFAULT_GREETINGS = ["hi", "hello", "hey", "help", "who are you", "what can you do", "good morning",
                     "good evening", "how are you", "test", "hi there"]

CATEGORIES = ("greeting", "creation", "action", "question", "other")

DEFAULTS = {
    "creation_keywords": DEFAULT_CREATION_KEYWORDS,
    "action_keywords": DEFAULT_ACTION_KEYWORDS,
    "refusal_phrases": DEFAULT_REFUSAL_PHRASES,
    "greetings": DEFAULT_GREETINGS,
    "repeat_streak_limit": 2,
    # categories whose step 0 skips the executor and starts on main
    "start_on_main_categories": [],
}

# keys an admin may edit through POST /control/router (plus confidence_threshold)
EDITABLE_KEYS = tuple(DEFAULTS) + ("confidence_threshold",)

_QUESTION_RX = re.compile(r"^(what|why|how|when|where|who|which|explain|describe|is|are|does|do|can|should)\b")


def rcfg() -> dict:
    """Router rules: config/app.json "router" block over DEFAULTS."""
    from .small_model import APP_CONFIG
    cfg = APP_CONFIG.get("router") or {}
    out = dict(DEFAULTS)
    for k in DEFAULTS:
        if k in cfg and cfg[k] is not None:
            out[k] = cfg[k]
    try:
        out["repeat_streak_limit"] = max(1, int(out["repeat_streak_limit"]))
    except (TypeError, ValueError):
        out["repeat_streak_limit"] = DEFAULTS["repeat_streak_limit"]
    return out


def _has_any(text: str, words) -> bool:
    t = (text or "").lower()
    return any(str(w).lower() in t for w in (words or []) if str(w))


def is_greeting(query: str, cfg: dict = None) -> bool:
    cfg = cfg or rcfg()
    q = (query or "").strip().lower()
    return q in {str(g).lower() for g in cfg["greetings"]} or (len(q) <= 3 and not q.startswith("/"))


def is_creation(query: str, cfg: dict = None) -> bool:
    return _has_any(query, (cfg or rcfg())["creation_keywords"])


def wants_action(query: str, cfg: dict = None) -> bool:
    return _has_any(query, (cfg or rcfg())["action_keywords"])


def is_refusal(content: str, cfg: dict = None) -> bool:
    return _has_any(content, (cfg or rcfg())["refusal_phrases"])


def classify_query(query: str, cfg: dict = None) -> str:
    """Coarse query category used for routing stats and start_on_main_categories.
    Only the category is ever stored -- never the query text."""
    cfg = cfg or rcfg()
    if is_greeting(query, cfg):
        return "greeting"
    if is_creation(query, cfg):
        return "creation"
    if wants_action(query, cfg):
        return "action"
    q = (query or "").strip().lower()
    if q.endswith("?") or _QUESTION_RX.match(q):
        return "question"
    return "other"


def start_on_main(category: str, cfg: dict = None) -> bool:
    return category in ((cfg or rcfg()).get("start_on_main_categories") or [])


def escalate_reason(*, step: int, content: str, tool_calls, query: str, is_loop: bool,
                    cfg: dict = None) -> str:
    """Why an executor step should be re-run on main ('' = keep the executor's answer).
    Same triggers, same order as the original inline should_escalate."""
    cfg = cfg or rcfg()
    if is_loop:
        return "loop"
    if step == 0 and not tool_calls:
        if is_creation(query, cfg):
            return "creation_no_tool"
        if wants_action(query, cfg):
            if "```" in (content or ""):
                return "tutorial_code"
            if is_refusal(content, cfg):
                return "refused"
        if not (content or "").strip():
            return "empty"
    return ""

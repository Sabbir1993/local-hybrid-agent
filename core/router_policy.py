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
    # categories whose step 0 runs on main to think and write the plan (create_plan); the
    # executor then carries the plan out. Unlike start_on_main_categories, main is held only
    # for the planning steps, not the whole run.
    "plan_first_categories": ["creation"],
    "plan_first_max_steps": 3,
    # ask the model server to REQUIRE a tool call on the first step of action requests
    "tool_choice_required": True,
    # the executor sees this request + this run's steps, not the whole chat
    "executor_fresh_context": True,
    # offer the finish(answer) tool: an unambiguous end-of-run signal (core/agent_loop/finish.py)
    "finish_tool": True,
    # learn from recent turns (core/lane_health.py): send a category to main while the
    # executor's success rate on it is below min_executor_success, or its breaker is open
    "adaptive": True,
    "min_executor_success": 0.7,
    "adaptive_cooldown_s": 300,
    # CPU-flavored tool shortcut on step 0 (Needle/Laya). Off because the classifier never
    # produced a tool call in the logged runs and cost 10-40s per request - but it MUST be
    # present here: rcfg() only carries DEFAULTS keys, so without this entry the flag an
    # admin sets in app.json never reaches the loop and the shortcut is silently dead.
    "tool_shortcut": False,
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


_KNOWLEDGE_RX = re.compile(r"^\W*(what|why|how|when|where|who|which|explain|describe|tell me about)\b")


def force_tool_call(category: str, step: int, nudges: int, query: str = "", cfg: dict = None) -> bool:
    """True when this model turn must be a tool call: an action request, on its
    first step or on the retry right after a nudge. Only the "action" category:
    a creation request may legitimately be answered in text (a poem, an essay),
    and "how do I read a file?" is a knowledge question that merely contains an
    action word."""
    cfg = cfg or rcfg()
    if not cfg.get("tool_choice_required", True):
        return False
    if _KNOWLEDGE_RX.match((query or "").strip().lower()):
        return False
    return category == "action" and (step == 0 or nudges > 0)


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


# Escalation reasons that mean "the executor did not act on this request". Retrying the executor
# later in the same run only repeats the failure (and can end the run on its narration), so a run
# that escalated for one of these stays on main. A "loop" is different - a transient degenerate
# reply - and keeps its one forgiven retry (ESC_STREAK_LIMIT in routes/agent/constants.py).
SUSTAIN_REASONS = ("no_tool_call", "empty", "creation_no_tool", "refused", "tutorial_code")


def plan_first_reason(category: str, cfg: dict = None) -> str:
    """'plan_first' when main should reason and plan before the executor starts, else ''."""
    cfg = cfg or rcfg()
    if category in (cfg.get("plan_first_categories") or []) and int(cfg.get("plan_first_max_steps") or 0) > 0:
        return "plan_first"
    return ""


def main_first_reason(category: str, cfg: dict = None, lane: str = "executor") -> str:
    """'' to let the executor take step 0, else why main should: the category is on the
    configured list, or recent turns show `lane` failing on it (core/lane_health.py)."""
    cfg = cfg or rcfg()
    if category in (cfg.get("start_on_main_categories") or []):
        return "start_on_main"
    if not cfg.get("adaptive", True):
        return ""
    from . import lane_health
    lane_health.ensure_hydrated()
    return lane_health.health.prefer_main(
        lane, category, min_success=float(cfg.get("min_executor_success", 0.7)),
        min_samples=int(cfg.get("adaptive_min_samples", 10)),
        cooldown_s=float(cfg.get("adaptive_cooldown_s", 300)))


def escalate_reason(*, step: int, content: str, tool_calls, query: str, is_loop: bool,
                    cfg: dict = None, category: str = None) -> str:
    """Why an executor step should be re-run on main ('' = keep the executor's answer).

    `category` is the request's category when the caller already resolved it (the classifier may
    have refined the keyword rules); without it the keyword rules decide, as before."""
    cfg = cfg or rcfg()
    if is_loop:
        return "loop"
    if step == 0 and not tool_calls:
        creation = (category == "creation") if category else is_creation(query, cfg)
        action = (category == "action") if category else wants_action(query, cfg)
        if creation:
            return "creation_no_tool"
        if action:
            if "```" in (content or ""):
                return "tutorial_code"
            if is_refusal(content, cfg):
                return "refused"
        if not (content or "").strip():
            return "empty"
        if action and not _KNOWLEDGE_RX.match((query or "").strip().lower()):
            # judged by what the turn did, not how it reads: an action request that
            # ended without a tool call was not acted on, whatever the text says
            # ("[Let me ...]", a plan, a guess). The small executor gets no second try.
            return "no_tool_call"
    return ""

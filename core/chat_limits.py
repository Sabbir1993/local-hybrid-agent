"""Tunable limits of CHAT mode (config: chat.*), the counterpart of core/agent_tools/limits.py for Agent mode.

Every reader clamps to the allowed range and falls back to the default when the key is missing or not a number, so a
hand-edited config/app.json can never push a chat turn out of bounds. The Settings -> Capabilities -> "Chat limits" page
reads this table (default, range, help) and writes the same keys through /control/chat_limits.
"""
from typing import Optional

from .small_model import APP_CONFIG

# key -> (default, min, max, help); the type of the default (int / float) is the type of the value
CHAT_LIMITS = {
    # tool loop
    "project_max_rounds": (40, 1, 100, "Most tool rounds for one turn that builds a website (a section per call)."),
    "doc_max_rounds": (24, 1, 100, "Most tool rounds for one turn that builds a workbook, report or deck (create, then edits / appends)."),
    "repeat_call_limit": (2, 1, 10, "The same tool call with the same arguments this many times in a turn is stopped as a loop."),
    "runaway_retries": (2, 0, 10, "Times a turn is restarted after the model ran away into a loop, before it is given up."),
    "auto_continue": (3, 0, 10, "Times a reply cut off by the length limit is continued automatically (0 = never)."),
    "run_timeout_s": (1800, 0, 86400, "Wall-clock limit for one chat turn, in seconds (0 = none)."),
    # web
    "web_soft_chars": (6000, 500, 50000, "Web page / search text kept per result before it is trimmed (characters)."),
    # documents, decks and sites
    "deck_research_calls": (10, 0, 40, "Web calls a deck / PDF build may use for research (0 = no extra budget)."),
    "deck_max_tokens": (8000, 0, 65536, "Cap for ONE turn while building a deck or PDF (0 = no cap)."),
    "project_max_tokens": (6000, 0, 65536, "Cap for ONE turn while building a website (0 = no cap)."),
    "deck_repeat_penalty": (1.0, 0.0, 2.0, "Repeat penalty while writing a deck (0 = leave the chat setting). Above 1.0 garbles URLs and names."),
    "build_temperature": (0.3, 0.0, 2.0, "Highest temperature while a turn writes a file (the sampler panel's value is lowered to this)."),
    # prompt
    "sticky_max_tokens": (6000, 500, 32000, "Tokens of manuals / document tools kept at the start of the prompt between turns."),
}

LEVELS = ("none", "low", "medium", "high", "extra", "deep")
LEVEL_FIELDS = {                 # field -> (min, max)
    "tool_calls": (0, 100),
    "token_budget": (500, 100000),
    "max_rounds": (0, 100),
}
# the per-thinking-level matrix (chat.thinking_budgets): tool / web calls, output tokens of one turn, tool rounds
DEFAULT_LEVELS = {
    "none": {"tool_calls": 0, "token_budget": 6000, "max_rounds": 0},
    "low": {"tool_calls": 4, "token_budget": 8000, "max_rounds": 4},
    "medium": {"tool_calls": 8, "token_budget": 12000, "max_rounds": 12},
    "high": {"tool_calls": 16, "token_budget": 16000, "max_rounds": 20},
    "extra": {"tool_calls": 25, "token_budget": 20000, "max_rounds": 30},
    "deep": {"tool_calls": 35, "token_budget": 25000, "max_rounds": 40},
}
# web calls by how much the Knowledge Base already covers (chat.web_budget[coverage][tier])
WEB_BUDGET_ROWS = ("full", "partial", "open")
WEB_BUDGET_TIERS = ("low", "medium", "high", "deep")
WEB_BUDGET_RANGE = (0, 100)
from .kb_coverage import DEFAULT_BUDGET as DEFAULT_WEB_BUDGET      # one table of defaults, owned by the code that reads it


def _cfg(app_cfg: Optional[dict]) -> dict:
    cfg = (app_cfg if app_cfg is not None else APP_CONFIG).get("chat")
    return cfg if isinstance(cfg, dict) else {}


def clamp_value(key: str, v):
    """`v` as the value `key` holds, inside its range. Raises ValueError / TypeError when it is not a number."""
    default, lo, hi, _ = CHAT_LIMITS[key]
    if isinstance(v, bool):
        raise ValueError("not a number")
    v = float(v) if isinstance(default, float) else int(v)
    v = min(max(v, lo), hi)
    return round(v, 2) if isinstance(default, float) else v


def chat_limit(key: str, app_cfg: Optional[dict] = None):
    """The clamped value of chat.<key> (the default when missing or invalid). `app_cfg` defaults to the live config."""
    try:
        return clamp_value(key, _cfg(app_cfg).get(key, CHAT_LIMITS[key][0]))
    except (TypeError, ValueError):
        return CHAT_LIMITS[key][0]


def _level_num(field: str, v, default: int) -> int:
    lo, hi = LEVEL_FIELDS[field]
    try:
        if isinstance(v, bool):
            raise ValueError
        return min(max(int(v), lo), hi)
    except (TypeError, ValueError):
        return default


def level_row(level: str, app_cfg: Optional[dict] = None) -> dict:
    """{tool_calls, token_budget, max_rounds} for a thinking level ("none".."extra", "deep"), clamped; a field that is
    missing or invalid takes the level's default."""
    base = DEFAULT_LEVELS.get(level) or DEFAULT_LEVELS["medium"]
    raw = _cfg(app_cfg).get("thinking_budgets")
    row = raw.get(level) if isinstance(raw, dict) else None
    row = row if isinstance(row, dict) else {}
    return {f: _level_num(f, row.get(f, base[f]), base[f]) for f in LEVEL_FIELDS}


def level_matrix(app_cfg: Optional[dict] = None) -> dict:
    return {lvl: level_row(lvl, app_cfg) for lvl in LEVELS}


def web_budget_matrix(app_cfg: Optional[dict] = None) -> dict:
    raw = _cfg(app_cfg).get("web_budget")
    raw = raw if isinstance(raw, dict) else {}
    lo, hi = WEB_BUDGET_RANGE
    out = {}
    for row in WEB_BUDGET_ROWS:
        cur = raw.get(row) if isinstance(raw.get(row), dict) else {}
        out[row] = {}
        for t in WEB_BUDGET_TIERS:
            d = DEFAULT_WEB_BUDGET[row][t]
            try:
                out[row][t] = d if cur.get(t) is None else min(max(int(cur[t]), lo), hi)
            except (TypeError, ValueError):
                out[row][t] = d
    return out


def turn_token_note() -> str:
    return ("Chat & Sampling max tokens, when set, overrides the level's Token budget; thinking counts against it.")


def view(app_cfg: Optional[dict] = None) -> dict:
    """Everything the Chat limits page shows: values, defaults, ranges and help for each limit and the level matrices."""
    return {
        "values": {k: chat_limit(k, app_cfg) for k in CHAT_LIMITS},
        "defaults": {k: v[0] for k, v in CHAT_LIMITS.items()},
        "ranges": {k: [v[1], v[2]] for k, v in CHAT_LIMITS.items()},
        "help": {k: v[3] for k, v in CHAT_LIMITS.items()},
        "levels": level_matrix(app_cfg),
        "level_defaults": DEFAULT_LEVELS,
        "level_ranges": {f: list(r) for f, r in LEVEL_FIELDS.items()},
        "web_budget": web_budget_matrix(app_cfg),
        "web_budget_defaults": DEFAULT_WEB_BUDGET,
        "web_budget_range": list(WEB_BUDGET_RANGE),
        "turn_token_note": turn_token_note(),
    }

"""What changes server-side when a request is a spoken turn (request field `voice: true`).

A voice turn has to start speaking within about a second and a half, so the things that cost seconds in typed chat
are switched off or narrowed here, in one place, and only for voice:

  effort            hidden reasoning off ("none"): thinking is the biggest delay before the first word
  max_tokens        a spoken answer is short; chat only (an agent tool call can need long arguments)
  web               tools only on an explicit "search / look up / URL", not on words like "today" or "current"
  kb                the knowledge base only for company-directed questions (no lookup for small talk)
  answer_check      off: a check that rewrites the answer after it was already spoken is useless
  guard_release     the output guard lets each finished sentence through instead of holding ~270 characters
  end_silence_ms / filler    read by the browser (static/js/voice.js) through GET /media/tts/status

Tunable under "voice" in config/app.json. Typed chat never reaches any of this.
"""
from typing import Optional

DEFAULTS = {
    "enabled": True,
    "effort": "none",
    "max_tokens": 300,
    "web": "explicit",              # explicit | normal
    "kb": "company",                # company | normal
    "answer_check": "off",          # off | normal
    "guard_release": "sentence",    # sentence | full
    "end_silence_ms": 500,
    "filler": True,
}


def cfg() -> dict:
    from .small_model import APP_CONFIG
    raw = APP_CONFIG.get("voice")
    out = dict(DEFAULTS)
    if isinstance(raw, dict):
        out.update({k: v for k, v in raw.items() if k in DEFAULTS})
    try:
        out["max_tokens"] = max(32, min(4000, int(out["max_tokens"])))
        out["end_silence_ms"] = max(250, min(2500, int(out["end_silence_ms"])))
    except (TypeError, ValueError):
        out["max_tokens"], out["end_silence_ms"] = DEFAULTS["max_tokens"], DEFAULTS["end_silence_ms"]
    return out


def active(req) -> bool:
    """True when `req` is a spoken turn and voice mode is not switched off by the admin."""
    return bool(getattr(req, "voice", False)) and bool(cfg()["enabled"])


def apply(req, cap_tokens: bool = True) -> bool:
    """Narrow a ChatRunRequest / AgentRequest for a spoken turn (mutates `req`); True when voice is on."""
    if not getattr(req, "voice", False):
        return False
    c = cfg()
    if not c["enabled"]:
        req.voice = False
        return False
    from . import reasoning
    if c["effort"] in reasoning.LEVELS:
        req.reasoning_effort = c["effort"]
    if cap_tokens:
        try:
            unset = req.max_tokens is None or int(req.max_tokens) <= 0
        except (TypeError, ValueError):
            unset = True
        if unset:
            req.max_tokens = c["max_tokens"]
    if c["answer_check"] == "off" and hasattr(req, "verify"):
        req.verify = "off"
    return True


def web_explicit_only(req) -> bool:
    return active(req) and cfg()["web"] == "explicit"


def kb_company_only(req) -> bool:
    return active(req) and cfg()["kb"] == "company"


def sentence_release(req) -> bool:
    return active(req) and cfg()["guard_release"] == "sentence"


def public() -> dict:
    """The part the browser needs."""
    c = cfg()
    return {"enabled": bool(c["enabled"]), "end_silence_ms": c["end_silence_ms"], "filler": bool(c["filler"])}

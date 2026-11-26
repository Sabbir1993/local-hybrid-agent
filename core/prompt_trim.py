"""Smaller prompts for turns that need almost nothing ("Hi"), behind config switches.

A plain greeting used to carry the whole agent system prompt (rules, skills and agent listings, tool-call examples, project
instructions: ~4K tokens) although the reply is one line and no tool can be called. At ~250 tokens/s of prefill that is
~16 s before the first word. For a pure greeting we send a short stub instead and keep what personalises the reply
(the user's memory block). Everything else - real tasks, custom agents, voice, plan mode - keeps the full prompt.

Agent mode only. Chat mode keeps its tool list on a greeting on purpose: the tools must stay byte-identical across turns so
llama.cpp's prefix cache carries over (tests/test_prompt_sticky.py); clearing them on "Hi" only moves the prefill to the next turn.

config/app.json:
    "prompt_trim": {"enabled": true, "greeting": true}      # both default to true; set false to restore the full prompt
"""

from typing import Optional

GREETING_PROMPT = (
    "You are a friendly AI assistant that can also work as a coding and automation agent. "
    "The user's message is a greeting or small talk: answer briefly and naturally, in the language the user wrote in, "
    "without using tools. Offer help in a short line if it fits."
)


def enabled(name: str, cfg: Optional[dict] = None) -> bool:
    """Whether trim `name` is on: both the master switch and the named one default to true."""
    if cfg is None:
        try:
            from core.small_model import APP_CONFIG
            cfg = APP_CONFIG.get("prompt_trim") or {}
        except Exception:
            cfg = {}
    if not isinstance(cfg, dict):
        return True
    return bool(cfg.get("enabled", True)) and bool(cfg.get(name, True))


def greeting_turn(msgs: list, rpol: dict, *, plan: bool = False, custom_agent=None, voice: bool = False,
                  request_system: str = "", cfg: Optional[dict] = None) -> bool:
    """True when this agent request is a plain greeting that may use the stub prompt. The same rule the router uses
    (router_policy.classify_query == "greeting"), so a trimmed prompt never reaches the tool loop; a custom agent, a
    request-level system prompt, voice and plan mode keep the full prompt because they carry instructions of their own."""
    if not enabled("greeting", cfg) or plan or custom_agent or voice or (request_system or "").strip():
        return False
    from core import router_policy
    last = next((str(m.get("content", "")) for m in reversed(msgs or []) if m.get("role") == "user"), "")
    if not last.strip():
        return False
    return router_policy.classify_query(last, rpol, msgs=msgs) == "greeting"

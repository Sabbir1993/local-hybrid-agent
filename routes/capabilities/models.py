"""Pydantic request models for capabilities routes."""

from typing import Optional
from pydantic import BaseModel


class CapToggleReq(BaseModel):
    section: str          # web | skills | mcp | plugins
    enabled: bool


class LibraryListReq(BaseModel):
    allow: Optional[list] = None
    deny: Optional[list] = None


class AgentLibraryReq(BaseModel):
    enabled: Optional[bool] = None
    default_policy: Optional[str] = None      # "deny" | "allow"
    agents: Optional[LibraryListReq] = None
    commands: Optional[LibraryListReq] = None
    multi_lanes: Optional[dict] = None         # {"backend": "main"|"executor", "frontend": ...}


class ShellSettingsReq(BaseModel):
    ask_first: Optional[bool] = None
    allow_patterns: Optional[list] = None
    timeout_s: Optional[int] = None


AGENT_STEPS_MIN, AGENT_STEPS_MAX = 5, 200
# 0 = no wall-clock ceiling; 24h is the practical upper bound
AGENT_TIMEOUT_MIN_S, AGENT_TIMEOUT_MAX_S = 0, 86400


class AgentSettingsReq(BaseModel):
    max_steps: Optional[int] = None
    run_timeout_s: Optional[int] = None


class RouterSettingsReq(BaseModel):
    creation_keywords: Optional[list] = None
    action_keywords: Optional[list] = None
    refusal_phrases: Optional[list] = None
    greetings: Optional[list] = None
    repeat_streak_limit: Optional[int] = None
    start_on_main_categories: Optional[list] = None
    confidence_threshold: Optional[float] = None

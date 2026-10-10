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
AGENT_BUDGET_MIN, AGENT_BUDGET_MAX = 0, 50_000_000


class AgentSettingsReq(BaseModel):
    max_steps: Optional[int] = None
    run_timeout_s: Optional[int] = None
    run_token_budget: Optional[int] = None    # new tokens one run may use (0 = off)


class AgentLimitsReq(BaseModel):
    """Every field is optional; only the ones sent are changed (and clamped to their range)."""
    write_file_max_tokens: Optional[int] = None
    chunk_target_tokens: Optional[int] = None
    read_file_default_limit: Optional[int] = None
    read_file_max_chars: Optional[int] = None
    verify_max_retries: Optional[int] = None
    executor_max_tokens: Optional[int] = None
    main_max_tokens: Optional[int] = None
    vision_max_tokens: Optional[int] = None
    plan_max_items: Optional[int] = None
    plan_item_max_chars: Optional[int] = None
    plan_item_max_steps: Optional[int] = None
    plan_required: Optional[str] = None
    python_prompt_nudge: Optional[int] = None
    executor_max_effort: Optional[str] = None
    mirror_to_workspace: Optional[bool] = None
    compaction_threshold: Optional[float] = None
    memory_enabled: Optional[bool] = None
    memory_allow_cloud: Optional[bool] = None
    memory_file_max_bytes: Optional[int] = None
    memory_max_files: Optional[int] = None


class RouterSettingsReq(BaseModel):
    creation_keywords: Optional[list] = None
    action_keywords: Optional[list] = None
    refusal_phrases: Optional[list] = None
    greetings: Optional[list] = None
    repeat_streak_limit: Optional[int] = None
    start_on_main_categories: Optional[list] = None
    plan_first_categories: Optional[list] = None
    plan_first_max_steps: Optional[int] = None
    confidence_threshold: Optional[float] = None
    tool_choice_required: Optional[bool] = None
    executor_fresh_context: Optional[bool] = None
    finish_tool: Optional[bool] = None
    adaptive: Optional[bool] = None
    min_executor_success: Optional[float] = None
    adaptive_min_samples: Optional[int] = None
    adaptive_cooldown_s: Optional[int] = None
    auto_apply: Optional[bool] = None
    classifier: Optional[dict] = None

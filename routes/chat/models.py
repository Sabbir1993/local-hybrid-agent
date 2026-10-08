from typing import Literal, Optional
from pydantic import BaseModel
from ..common.sampling_extra import SamplerFields


class ChatRunRequest(SamplerFields):
    messages: list
    web_search: bool = True
    deep_mode: bool = False
    temperature: Optional[float] = None   # None -> the custom agent's value, else 0.7
    max_tokens: int = -1
    system_prompt: Optional[str] = None
    # None (older clients) keeps the Deep-only behaviour; see core/reasoning.py
    reasoning_effort: Optional[Literal["none", "low", "medium", "high", "extra"]] = None
    # answer check for this request (shield toggle): off | badge | gate; None = saved setting
    verify: Optional[Literal["off", "badge", "gate"]] = None
    top_p: Optional[float] = None
    min_p: Optional[float] = None
    repeat_penalty: Optional[float] = None
    presence_penalty: Optional[float] = None
    top_k: Optional[int] = None
    custom_agent_id: Optional[int] = None
    voice: bool = False       # spoken conversation: answer in short plain sentences (core/voice_prompt.py)


class CompactRequest(BaseModel):
    messages: Optional[list] = None       # fallback when no session_id
    session_id: Optional[int] = None
    instructions: Optional[str] = None    # extra user instructions, e.g. "focus on the DB schema"
    keep_last: int = 2                    # recent messages kept verbatim after the summary
    use_executor: bool = False            # force the small executor model for the summary
    agent_mode: bool = False              # agent mode: requires an active project (project_id)
    project_id: Optional[int] = None

from typing import Literal, Optional, Union
from pydantic import BaseModel, Field
from ..common.sampling_extra import SamplerFields


class AttachedFile(BaseModel):
    name: str
    path: str
    preview: str = ""
    truncated: bool = False


class AgentRequest(SamplerFields):
    messages: list
    max_steps: Optional[int] = None   # None -> agent.max_steps from config/app.json
    temperature: Optional[float] = None   # None -> the custom agent's value, else 0.4
    max_tokens: int = -1
    large_model: Optional[str] = None
    mode: Optional[str] = "main"
    plan: bool = False
    session_id: Optional[int] = None
    attachments: list[AttachedFile] = []
    cloud_model_override: Optional[str] = None   # key of cloud model for executor/vision lanes
    reasoning_effort: Optional[Literal["none", "low", "medium", "high", "extra"]] = None
    custom_agent_id: Optional[int] = None
    personal: bool = False   # a Personal Agent run from Chat: read/run on the device, write only to the common folder
    top_p: Optional[float] = None
    min_p: Optional[float] = None
    repeat_penalty: Optional[float] = None
    presence_penalty: Optional[float] = None
    top_k: Optional[int] = None
    system_prompt: Optional[str] = None
    voice: bool = False   # spoken conversation: answer in short plain sentences (core/voice_prompt.py)
    verify: Optional[Literal["off", "badge", "gate"]] = None   # answer check (shield toggle)
    # composer Mode dropdown, enforced per request (never a global setting): plan = read-only, manual = ask before
    # every edit/command, accept_edits = edits run unasked, bypass = no permission cards for this user
    permission_mode: Optional[Literal["auto", "manual", "accept_edits", "plan", "bypass"]] = None
    # chosen by the client so Stop can address the run even before the first byte of the response arrives
    client_run_id: Optional[str] = Field(None, max_length=64)


class PermissionAnswerReq(BaseModel):
    req_id: str
    decision: str            # allow | project | user | always | deny
    pattern: Optional[str] = None
    project_id: Optional[Union[int, str]] = None


class RunFeedbackReq(BaseModel):
    run_id: str
    rating: int          # 1 = thumbs up, -1 = thumbs down, 0 = clear


class CommandExpandReq(BaseModel):
    name: str
    args: str = ""


class VisionReq(BaseModel):
    image_b64: str = Field(..., max_length=15 * 1024 * 1024)   # ~11 MB image
    mime: str = "image/png"
    question: str = "Describe this image in detail for a coding agent."
    cloud_model_override: Optional[str] = None

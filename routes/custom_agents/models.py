from typing import List, Optional
from pydantic import BaseModel, Field

PUBLISH_PERMISSION = "custom_agents.publish"
# tools a custom agent loses when it runs as a spawn_agent sub-agent (core/subagent.py)
_SUBAGENT_DENIED = ("run_shell", "run_python", "generate_image", "generate_video", "spawn_agent")


class CustomAgentCreateReq(BaseModel):
    name: str = Field(..., min_length=1, max_length=120)
    slug: Optional[str] = Field(None, max_length=80)
    description: str = Field(..., max_length=1000)
    icon: Optional[str] = Field("🤖", max_length=16)
    system_prompt: str = Field(..., min_length=1, max_length=20000)
    tool_allowlist: List[str] = Field(default_factory=list)
    input_template: Optional[str] = Field("", max_length=5000)
    preferred_lane: Optional[str] = Field("auto", max_length=32)
    reasoning_effort: Optional[str] = Field("medium", max_length=16)
    temperature: Optional[float] = Field(0.4, ge=0.0, le=2.0)
    is_public: Optional[bool] = False
    work_dir: Optional[str] = Field("", max_length=500)   # folder on the owner's machine the agent works in
    device_id: Optional[str] = Field("", max_length=120)
    device_name: Optional[str] = Field("", max_length=120)


class CustomAgentUpdateReq(BaseModel):
    name: Optional[str] = Field(None, min_length=1, max_length=120)
    slug: Optional[str] = Field(None, max_length=80)
    description: Optional[str] = Field(None, max_length=1000)
    icon: Optional[str] = Field(None, max_length=16)
    system_prompt: Optional[str] = Field(None, min_length=1, max_length=20000)
    tool_allowlist: Optional[List[str]] = None
    input_template: Optional[str] = Field(None, max_length=5000)
    preferred_lane: Optional[str] = Field(None, max_length=32)
    reasoning_effort: Optional[str] = Field(None, max_length=16)
    temperature: Optional[float] = Field(None, ge=0.0, le=2.0)
    is_public: Optional[bool] = None
    work_dir: Optional[str] = Field(None, max_length=500)
    device_id: Optional[str] = Field(None, max_length=120)
    device_name: Optional[str] = Field(None, max_length=120)


class ForkReq(BaseModel):
    name: Optional[str] = None
    work_dir: Optional[str] = Field("", max_length=500)   # the forker's own folder; nothing is copied from the source
    device_id: Optional[str] = Field("", max_length=120)
    device_name: Optional[str] = Field("", max_length=120)

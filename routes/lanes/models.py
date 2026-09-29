"""
routes/lanes/models.py - Pydantic request models for the lanes routes.
"""

from typing import Optional

from pydantic import BaseModel, Field


class LaneReq(BaseModel):
    name: str = Field(max_length=32)
    backend: str = "local"                 # local | cloud
    kind: Optional[str] = None             # chat | vision | embed
    label: Optional[str] = Field(default=None, max_length=40)
    fallback: Optional[str] = None
    # local
    model: Optional[str] = None
    mmproj: Optional[str] = None
    port: Optional[int] = None
    gpu: Optional[int] = None
    ctx: Optional[int] = None
    idle_unload_s: Optional[int] = None
    # cloud
    cloud: Optional[str] = None
    jobs: Optional[list[str]] = None       # wizard step 3: map these jobs to it
    timeout_s: Optional[int] = None
    # speech to text (whisper.cpp)
    threads: Optional[int] = None
    language: Optional[str] = Field(default=None, max_length=8)
    # image/video on this PC (stable-diffusion.cpp): engine "sdcpp"
    engine: Optional[str] = Field(default=None, max_length=16)
    diffusion_model: Optional[str] = Field(default=None, max_length=400)
    llm: Optional[str] = Field(default=None, max_length=400)
    vae: Optional[str] = Field(default=None, max_length=400)
    llm_vision: Optional[str] = Field(default=None, max_length=400)
    edit_refs: Optional[bool] = None
    max_refs: Optional[int] = None
    default_size: Optional[str] = Field(default=None, max_length=8)
    steps: Optional[int] = None
    cfg_scale: Optional[float] = None
    flow_shift: Optional[float] = None
    sampler: Optional[str] = Field(default=None, max_length=24)
    offload_to_cpu: Optional[bool] = None
    vae_tiling: Optional[bool] = None


class RolesReq(BaseModel):
    map: dict[str, Optional[str]]
    as_default: bool = False


class MediaSettingsReq(BaseModel):
    allow_cloud_audio: Optional[bool] = None
    image_per_day: Optional[int] = Field(default=None, ge=0, le=10000)
    video_per_day: Optional[int] = Field(default=None, ge=0, le=1000)


class VerifyReq(BaseModel):
    mode: Optional[str] = None
    apply_to: Optional[str] = None
    max_rounds: Optional[int] = Field(default=None, ge=0, le=3)
    min_length: Optional[int] = Field(default=None, ge=0, le=100000)

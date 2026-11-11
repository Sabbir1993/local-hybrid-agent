from typing import Optional
from pydantic import BaseModel


class SwitchRequest(BaseModel):
    profile: Optional[str] = None
    target: Optional[str] = None
    engine: Optional[str] = None          # "llama" or "vllm"; None = the model's saved engine


class KeepaliveRequest(BaseModel):
    enabled: bool


class ConfigRequest(BaseModel):
    updates: dict
    persist: bool = True
    restart: bool = True

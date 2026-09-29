from typing import Optional
from pydantic import BaseModel


class SwitchRequest(BaseModel):
    profile: Optional[str] = None
    target: Optional[str] = None


class KeepaliveRequest(BaseModel):
    enabled: bool


class ConfigRequest(BaseModel):
    updates: dict
    persist: bool = True
    restart: bool = True

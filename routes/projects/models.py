"""Pydantic request models for projects routes."""

from typing import Optional
from pydantic import BaseModel


class ProjectReq(BaseModel):
    name: str
    workspace_dir: Optional[str] = None
    device_id: Optional[str] = None
    device_name: Optional[str] = None


class UpdateWorkspaceReq(BaseModel):
    workspace_dir: str
    device_id: Optional[str] = None
    device_name: Optional[str] = None


class SessionReq(BaseModel):
    title: Optional[str] = None
    # the title is a placeholder from the first message: name the chat by its topic later (core/session_title.py)
    auto_title: Optional[bool] = None
    agent_id: Optional[int] = None
    model: Optional[str] = None
    is_pinned: Optional[bool] = None


class BrowseFolderReq(BaseModel):
    initial_dir: Optional[str] = ""


class MkdirReq(BaseModel):
    path: str
    name: str


class MessageReq(BaseModel):
    role: str
    content: str
    meta: Optional[dict] = None


class ForkReq(BaseModel):
    upto_message_id: Optional[int] = None


class RenameDeviceReq(BaseModel):
    new_name: str
    old_name: Optional[str] = None
    device_id: Optional[str] = None


class ProjectMemoryReq(BaseModel):
    key: str
    content: str


class SessionMemoryReq(BaseModel):
    content: str



class ShellExecReq(BaseModel):
    command: str
    cwd: Optional[str] = None
    timeout: Optional[int] = 60
    shell: Optional[str] = "powershell"
    elevated: bool = False          # run as Windows administrator: only inside a started administrator session



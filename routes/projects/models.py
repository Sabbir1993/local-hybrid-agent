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


class BrowseFolderReq(BaseModel):
    initial_dir: Optional[str] = ""


class MkdirReq(BaseModel):
    path: str
    name: str


class MessageReq(BaseModel):
    role: str
    content: str
    meta: Optional[dict] = None


class RenameDeviceReq(BaseModel):
    new_name: str
    old_name: Optional[str] = None
    device_id: Optional[str] = None

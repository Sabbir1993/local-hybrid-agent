"""Pydantic request/body models for admin RBAC routes."""

from pydantic import BaseModel


class CreateUserBody(BaseModel):
    username: str
    password: str
    display_name: str | None = None
    email: str | None = None
    roles: list[str] = []


class UpdateUserBody(BaseModel):
    is_active: bool | None = None
    roles: list[str] | None = None
    is_super_admin: bool | None = None
    unlock: bool | None = None       # clear a failed-login lockout


class CreateRoleBody(BaseModel):
    name: str
    description: str | None = None


class RolePermissionsBody(BaseModel):
    grant: list[str] = []
    revoke: list[str] = []


class ClearAllDataBody(BaseModel):
    confirm: str  # must equal "DELETE" -- a lightweight typed-confirmation guard

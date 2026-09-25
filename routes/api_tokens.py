"""routes/api_tokens.py - admin-issued API tokens for scripted / OpenAPI access.

Only users.manage holders issue or revoke tokens. A token acts as its owner, limited
to the scopes it was issued with (and never beyond the owner's current rights - see
core.auth.verify_api_token). The raw token is returned once, at creation.
"""

from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field

from core import auth_db
from core.audit import audit_log
from core.auth import (API_TOKEN_FORBIDDEN_PERMS, API_TOKEN_MAX_DAYS, Principal,
                       create_api_token, user_has_permission)
from core.deps import get_current_user, require_permission

router = APIRouter(prefix="/admin/api-tokens", tags=["admin"],
                   dependencies=[Depends(require_permission("users.manage"))])
self_router = APIRouter(prefix="/auth/tokens", tags=["auth"])


class CreateTokenBody(BaseModel):
    name: str = Field(min_length=1, max_length=80)
    user_id: int
    permissions: list[str] = []
    expires_days: int = Field(default=30, ge=1, le=API_TOKEN_MAX_DAYS)


def _ip(request: Request) -> Optional[str]:
    return request.client.host if request.client else None


def _owner_permission_keys(user_id: int) -> set:
    row = auth_db.get_user_by_id(user_id)
    if not row or not row["is_active"]:
        raise HTTPException(status_code=404, detail="user not found or inactive")
    return set(auth_db.PERMISSIONS) if row["is_super_admin"] else auth_db.get_user_permission_keys(user_id)


@router.get("")
async def list_tokens():
    return {"tokens": auth_db.list_api_tokens(),
            "grantable": sorted(set(auth_db.PERMISSIONS) - API_TOKEN_FORBIDDEN_PERMS),
            "max_days": API_TOKEN_MAX_DAYS}


@router.post("")
async def create_token(body: CreateTokenBody, request: Request,
                       user: Principal = Depends(get_current_user)):
    if user.via_token:
        raise HTTPException(status_code=403, detail="API tokens cannot issue tokens")
    perms = set(body.permissions)
    unknown = perms - set(auth_db.PERMISSIONS)
    if unknown:
        raise HTTPException(status_code=400, detail=f"unknown permissions: {sorted(unknown)}")
    forbidden = perms & API_TOKEN_FORBIDDEN_PERMS
    if forbidden:
        raise HTTPException(status_code=400, detail=f"not grantable to API tokens: {sorted(forbidden)}")
    if not perms:
        raise HTTPException(status_code=400, detail="choose at least one permission")
    # same rule as role assignment: you can't hand out rights you don't hold
    not_held = {p for p in perms if not user_has_permission(user, p)}
    if not_held:
        raise HTTPException(status_code=403, detail=f"you don't hold: {sorted(not_held)}")
    lacking = perms - _owner_permission_keys(body.user_id)
    if lacking:
        raise HTTPException(status_code=400, detail=f"the token's user doesn't hold: {sorted(lacking)}")
    raw, tid = create_api_token(body.user_id, body.name.strip(), perms, body.expires_days, user.id)
    audit_log(user, action="api_token.create", resource=f"token:{tid}", result="allow", ip=_ip(request),
              detail={"user_id": body.user_id, "permissions": sorted(perms), "days": body.expires_days})
    return {"id": tid, "token": raw,
            "note": "Copy this token now - it is not stored and can't be shown again."}


@router.delete("/{token_id}")
async def revoke_token(token_id: int, request: Request, user: Principal = Depends(get_current_user)):
    if not auth_db.get_api_token(token_id):
        raise HTTPException(status_code=404, detail="token not found")
    auth_db.revoke_api_token(token_id)
    audit_log(user, action="api_token.revoke", resource=f"token:{token_id}", result="allow", ip=_ip(request))
    return {"ok": True}


# ---- a user's own tokens (view + revoke; issuing stays admin-only) ----

@self_router.get("")
async def my_tokens(user: Principal = Depends(get_current_user)):
    return {"tokens": auth_db.list_api_tokens(user.id)}


@self_router.delete("/{token_id}")
async def revoke_my_token(token_id: int, request: Request, user: Principal = Depends(get_current_user)):
    row = auth_db.get_api_token(token_id)
    if not row or row["user_id"] != user.id:
        raise HTTPException(status_code=404, detail="token not found")
    auth_db.revoke_api_token(token_id)
    audit_log(user, action="api_token.revoke", resource=f"token:{token_id}", result="allow", ip=_ip(request))
    return {"ok": True}

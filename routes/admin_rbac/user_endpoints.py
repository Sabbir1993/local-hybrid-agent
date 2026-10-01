"""User management endpoints for the admin router."""

import time

from fastapi import APIRouter, Depends, HTTPException

from core import auth_db
from core.audit import audit_log
from core.auth import Principal, password_policy_error
from core.auth_provider import hash_password
from core.deps import require_verified
from .helpers import _check_assignable_roles, _check_can_modify, _user_public
from .models import CreateUserBody, UpdateUserBody

router = APIRouter()


@router.get("/users")
async def list_users(user: Principal = Depends(require_verified("users.manage"))):
    rows = auth_db.db().execute("SELECT * FROM users ORDER BY username").fetchall()
    return {"users": [_user_public(r) for r in rows]}


@router.post("/users")
async def create_user(body: CreateUserBody, user: Principal = Depends(require_verified("users.manage"))):
    if auth_db.get_user_by_username(body.username):
        raise HTTPException(status_code=409, detail="username already exists")
    _check_assignable_roles(user, body.roles or ["user"])
    err = password_policy_error(body.password, body.username)
    if err:
        raise HTTPException(status_code=400, detail=err)
    uid = auth_db.create_user(
        username=body.username, password_hash=hash_password(body.password),
        display_name=body.display_name, email=body.email,
    )
    # the admin chose this password: the user must replace it at first sign-in
    auth_db.db().execute("UPDATE users SET must_change_password = 1 WHERE id = ?", (uid,))
    auth_db.db().commit()
    for role_name in (body.roles or ["user"]):
        auth_db.assign_role(uid, role_name, assigned_by=user.id)
    audit_log(user, action="users.manage", resource=body.username, result="allow")
    return _user_public(auth_db.get_user_by_id(uid))


@router.patch("/users/{user_id}")
async def update_user(user_id: int, body: UpdateUserBody,
                      user: Principal = Depends(require_verified("users.manage"))):
    target = auth_db.get_user_by_id(user_id)
    if not target:
        raise HTTPException(status_code=404, detail="user not found")
    _check_can_modify(user, target)
    if body.roles is not None:
        if user_id == user.id and not user.is_super_admin:
            raise HTTPException(status_code=403, detail="cannot change your own roles")
        _check_assignable_roles(user, body.roles)
    before_roles = auth_db.get_user_role_names(user_id)

    if body.is_super_admin is not None:
        if not user.is_super_admin:
            raise HTTPException(status_code=403, detail="only a super admin can grant or revoke super admin")
        auth_db.db().execute("UPDATE users SET is_super_admin = ?, updated_at = ? WHERE id = ?",
                              (1 if body.is_super_admin else 0, time.time(), user_id))

    if body.is_active is not None:
        auth_db.db().execute("UPDATE users SET is_active = ?, updated_at = ? WHERE id = ?",
                              (1 if body.is_active else 0, time.time(), user_id))
        if not body.is_active:
            auth_db.revoke_all_sessions_for_user(user_id)

    if body.unlock:
        auth_db.unlock_user(user_id)

    if body.roles is not None:
        auth_db.db().execute("DELETE FROM user_roles WHERE user_id = ?", (user_id,))
        for role_name in body.roles:
            auth_db.assign_role(user_id, role_name, assigned_by=user.id)

    auth_db.db().commit()
    detail = {k: v for k, v in body.model_dump().items() if v is not None}
    if body.roles is not None:
        detail["roles_before"] = before_roles
    audit_log(user, action="users.manage", resource=target["username"], detail=detail, result="allow")
    return _user_public(auth_db.get_user_by_id(user_id))


@router.delete("/users/{user_id}")
async def delete_user(user_id: int, user: Principal = Depends(require_verified("users.manage"))):
    if user_id == user.id:
        raise HTTPException(status_code=400, detail="cannot delete your own account")
    target = auth_db.get_user_by_id(user_id)
    if not target:
        raise HTTPException(status_code=404, detail="user not found")
    _check_can_modify(user, target)
    auth_db.delete_user(user_id)
    audit_log(user, action="users.manage", resource=target["username"], detail={"deleted": True}, result="allow")
    return {"ok": True}


@router.post("/users/{user_id}/mfa/reset")
async def reset_user_mfa(user_id: int, user: Principal = Depends(require_verified("users.manage"))):
    """Lost-authenticator recovery: wipe the target's secret + backup codes.
    They re-enroll at next login (or immediately in Settings when enforcement
    is off). Mirrors the touch-super-admin rule: only a super admin may reset
    a super admin's second factor. Never usable on yourself -- use /mfa/disable."""
    target = auth_db.get_user_by_id(user_id)
    if not target:
        raise HTTPException(status_code=404, detail="user not found")
    if user_id == user.id:
        raise HTTPException(status_code=400, detail="reset your own MFA via /auth/mfa/disable")
    _check_can_modify(user, target)
    if target["is_super_admin"] and not user.is_super_admin:
        raise HTTPException(status_code=403, detail="only a super admin can reset a super admin's MFA")
    was_on = auth_db.totp_enabled(user_id)
    auth_db.admin_reset_mfa(user_id)
    audit_log(user, action="mfa.admin_reset", resource=target["username"],
              detail={"was_enabled": was_on}, result="allow")
    return {"ok": True}

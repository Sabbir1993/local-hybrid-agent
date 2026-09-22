"""routes/admin_rbac.py - user/role/permission management and audit log (admin-only)."""

import time

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from core import auth_db
from core.audit import audit_log
from core.auth import Principal, user_has_permission
from core.auth_provider import hash_password
from core.deps import get_current_user, require_permission
from core.db import db_clear_all_projects_data
from core.memory import clear_chat_history_chunks
from core import agent_tools

router = APIRouter(prefix="/admin", tags=["admin"])


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


class CreateRoleBody(BaseModel):
    name: str
    description: str | None = None


class RolePermissionsBody(BaseModel):
    grant: list[str] = []
    revoke: list[str] = []


def _user_public(row) -> dict:
    return {
        "id": row["id"], "username": row["username"], "display_name": row["display_name"],
        "email": row["email"], "is_active": bool(row["is_active"]),
        "is_super_admin": bool(row["is_super_admin"]), "auth_provider": row["auth_provider"],
        "roles": auth_db.get_user_role_names(row["id"]),
        "last_login_at": row["last_login_at"],
    }


@router.get("/users")
async def list_users(user: Principal = Depends(require_permission("users.manage"))):
    rows = auth_db.db().execute("SELECT * FROM users ORDER BY username").fetchall()
    return {"users": [_user_public(r) for r in rows]}


@router.post("/users")
async def create_user(body: CreateUserBody, user: Principal = Depends(require_permission("users.manage"))):
    if auth_db.get_user_by_username(body.username):
        raise HTTPException(status_code=409, detail="username already exists")
    uid = auth_db.create_user(
        username=body.username, password_hash=hash_password(body.password),
        display_name=body.display_name, email=body.email,
    )
    for role_name in (body.roles or ["user"]):
        auth_db.assign_role(uid, role_name, assigned_by=user.id)
    audit_log(user, action="users.manage", resource=body.username, result="allow")
    return _user_public(auth_db.get_user_by_id(uid))


@router.patch("/users/{user_id}")
async def update_user(user_id: int, body: UpdateUserBody,
                       user: Principal = Depends(require_permission("users.manage"))):
    target = auth_db.get_user_by_id(user_id)
    if not target:
        raise HTTPException(status_code=404, detail="user not found")

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

    if body.roles is not None:
        auth_db.db().execute("DELETE FROM user_roles WHERE user_id = ?", (user_id,))
        for role_name in body.roles:
            auth_db.assign_role(user_id, role_name, assigned_by=user.id)

    auth_db.db().commit()
    audit_log(user, action="users.manage", resource=target["username"], result="allow")
    return _user_public(auth_db.get_user_by_id(user_id))


@router.delete("/users/{user_id}")
async def delete_user(user_id: int, user: Principal = Depends(require_permission("users.manage"))):
    if user_id == user.id:
        raise HTTPException(status_code=400, detail="cannot delete your own account")
    target = auth_db.get_user_by_id(user_id)
    if not target:
        raise HTTPException(status_code=404, detail="user not found")
    auth_db.db().execute("DELETE FROM users WHERE id = ?", (user_id,))
    auth_db.db().commit()
    audit_log(user, action="users.manage", resource=target["username"], detail={"deleted": True}, result="allow")
    return {"ok": True}


@router.get("/role_names")
async def list_role_names(user: Principal = Depends(get_current_user)):
    """Bare role-name list for knowledge-base and sanitizer role-access pickers."""
    if not (user_has_permission(user, "knowledge.manage") or user_has_permission(user, "settings.input_guard") or user_has_permission(user, "roles.manage")):
        raise HTTPException(status_code=403, detail="missing permission: settings.input_guard")
    rows = auth_db.db().execute("SELECT name FROM roles ORDER BY name").fetchall()
    return {"roles": [r["name"] for r in rows]}


@router.get("/user_names")
async def list_user_names(user: Principal = Depends(get_current_user)):
    """Bare username list from users table for sanitizer and targeting pickers."""
    if not (user_has_permission(user, "settings.input_guard") or user_has_permission(user, "users.manage")):
        raise HTTPException(status_code=403, detail="missing permission: settings.input_guard")
    rows = auth_db.db().execute("SELECT id, username, display_name FROM users WHERE is_active = 1 ORDER BY username").fetchall()
    return {"users": [{"id": r["id"], "username": r["username"], "display_name": r["display_name"] or r["username"]} for r in rows]}


@router.get("/roles")
async def list_roles(user: Principal = Depends(require_permission("roles.manage"))):
    roles = auth_db.db().execute("SELECT * FROM roles ORDER BY name").fetchall()
    out = []
    for r in roles:
        perms = auth_db.db().execute(
            "SELECT p.key FROM role_permissions rp JOIN permissions p ON p.id = rp.permission_id "
            "WHERE rp.role_id = ?", (r["id"],),
        ).fetchall()
        out.append({"id": r["id"], "name": r["name"], "description": r["description"],
                    "is_builtin": bool(r["is_builtin"]), "permissions": [p["key"] for p in perms]})
    return {"roles": out}


@router.get("/permissions")
async def list_permissions(user: Principal = Depends(get_current_user)):
    # Any authenticated user can see the static catalogue (needed to render admin UI checkboxes);
    # actually editing grants still requires roles.manage.
    rows = auth_db.db().execute("SELECT key, description FROM permissions ORDER BY key").fetchall()
    return {"permissions": [dict(r) for r in rows]}


@router.post("/roles")
async def create_role(body: CreateRoleBody, user: Principal = Depends(require_permission("roles.manage"))):
    existing = auth_db.db().execute("SELECT id FROM roles WHERE name = ?", (body.name,)).fetchone()
    if existing:
        raise HTTPException(status_code=409, detail="role already exists")
    auth_db.db().execute("INSERT INTO roles (name, description, is_builtin, created_at) VALUES (?, ?, 0, ?)",
                          (body.name, body.description, time.time()))
    auth_db.db().commit()
    audit_log(user, action="roles.manage", resource=body.name, result="allow")
    return {"ok": True}


@router.patch("/roles/{role_id}/permissions")
async def update_role_permissions(role_id: int, body: RolePermissionsBody,
                                   user: Principal = Depends(require_permission("roles.manage"))):
    role = auth_db.db().execute("SELECT * FROM roles WHERE id = ?", (role_id,)).fetchone()
    if not role:
        raise HTTPException(status_code=404, detail="role not found")
    now = time.time()
    for key in body.grant:
        perm = auth_db.db().execute("SELECT id FROM permissions WHERE key = ?", (key,)).fetchone()
        if perm:
            auth_db.db().execute(
                "INSERT OR IGNORE INTO role_permissions (role_id, permission_id, granted_at, granted_by) "
                "VALUES (?, ?, ?, ?)", (role_id, perm["id"], now, user.id),
            )
    for key in body.revoke:
        perm = auth_db.db().execute("SELECT id FROM permissions WHERE key = ?", (key,)).fetchone()
        if perm:
            auth_db.db().execute(
                "DELETE FROM role_permissions WHERE role_id = ? AND permission_id = ?", (role_id, perm["id"]),
            )
    auth_db.db().commit()
    audit_log(user, action="roles.manage", resource=role["name"],
              detail={"grant": body.grant, "revoke": body.revoke}, result="allow")
    return {"ok": True}


class ClearAllDataBody(BaseModel):
    confirm: str  # must equal "DELETE" -- a lightweight typed-confirmation guard


@router.post("/clear_all_data")
async def clear_all_data(body: ClearAllDataBody, user: Principal = Depends(get_current_user)):
    """Danger zone: wipe every user's chat/session/project/plan-item history
    and indexed chat/workspace memory. Does NOT touch files on disk under the
    workspace root -- scoped to database records only. Super-admin only --
    this is a destructive, irreversible action, so it bypasses the normal
    permission-grant system entirely and checks the is_super_admin column
    directly."""
    if not user.is_super_admin:
        raise HTTPException(status_code=403, detail="super admin only")
    if body.confirm != "DELETE":
        raise HTTPException(status_code=400, detail='type "DELETE" to confirm')

    counts = db_clear_all_projects_data()
    clear_chat_history_chunks()

    agent_tools._active_project.clear()
    agent_tools._ws_changes.clear()

    audit_log(user, action="data.clear_all", resource="projects+sessions+plan_items+memory",
              detail={"db_rows": counts}, result="allow")
    return {"ok": True, "cleared": counts}


@router.get("/audit_log")
async def get_audit_log(since: float = 0.0, user_id: int | None = None,
                         user: Principal = Depends(require_permission("audit.view"))):
    return {"entries": auth_db.list_audit(since=since, user_id=user_id)}

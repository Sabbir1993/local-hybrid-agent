"""Role and permission management endpoints for the admin router."""

import time

from fastapi import APIRouter, Depends, HTTPException

from core import auth_db
from core.audit import audit_log
from core.auth import Principal, user_has_permission
from core.deps import get_current_user, require_verified
from .helpers import _check_assignable_roles
from .models import CreateRoleBody, RolePermissionsBody

router = APIRouter()


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
async def list_roles(user: Principal = Depends(require_verified("roles.manage"))):
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
    from core.auth_db.common import PERMISSION_META, PERMISSION_MODULES
    rows = auth_db.db().execute("SELECT key, description FROM permissions ORDER BY key").fetchall()
    perms = []
    for r in rows:
        d = dict(r)
        module, kind, title, help_ = PERMISSION_META.get(d["key"], ("other", "action", d["key"], d.get("description") or ""))
        d.update({"module": module, "kind": kind, "title": title, "help": help_})
        perms.append(d)
    return {"permissions": perms,
            "modules": [{"id": m, "label": l} for m, l in PERMISSION_MODULES] + [{"id": "other", "label": "Other"}]}


@router.post("/roles")
async def create_role(body: CreateRoleBody, user: Principal = Depends(require_verified("roles.manage"))):
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
                                  user: Principal = Depends(require_verified("roles.manage"))):
    role = auth_db.db().execute("SELECT * FROM roles WHERE id = ?", (role_id,)).fetchone()
    if not role:
        raise HTTPException(status_code=404, detail="role not found")
    if not user.is_super_admin:
        missing = set(body.grant) - user.permission_keys
        if missing:
            raise HTTPException(status_code=403,
                                detail=f"cannot grant a permission you don't hold: {sorted(missing)[0]}")
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

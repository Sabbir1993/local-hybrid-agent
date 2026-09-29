"""Helper functions for admin RBAC routes."""

from fastapi import HTTPException

from core import auth_db
from core.auth import Principal


def _role_permission_keys(role_name: str) -> set | None:
    rows = auth_db.db().execute(
        "SELECT p.key FROM roles r JOIN role_permissions rp ON rp.role_id = r.id "
        "JOIN permissions p ON p.id = rp.permission_id WHERE r.name = ?", (role_name,),
    ).fetchall()
    if not rows and not auth_db.db().execute("SELECT 1 FROM roles WHERE name = ?", (role_name,)).fetchone():
        return None
    return {r["key"] for r in rows}


def _check_assignable_roles(user: Principal, role_names: list[str]) -> None:
    """A caller may only hand out roles whose permissions they already hold --
    otherwise users.manage alone is a path to admin."""
    if user.is_super_admin:
        return
    for name in role_names:
        keys = _role_permission_keys(name)
        if keys is None:
            raise HTTPException(status_code=400, detail=f"unknown role '{name}'")
        missing = keys - user.permission_keys
        if missing:
            raise HTTPException(status_code=403,
                                detail=f"cannot assign role '{name}': you lack {sorted(missing)[0]}")


def _check_can_modify(user: Principal, target) -> None:
    if target["is_super_admin"] and not user.is_super_admin:
        raise HTTPException(status_code=403, detail="only a super admin can modify a super admin")


def _user_public(row) -> dict:
    return {
        "id": row["id"], "username": row["username"], "display_name": row["display_name"],
        "email": row["email"], "is_active": bool(row["is_active"]),
        "is_super_admin": bool(row["is_super_admin"]), "auth_provider": row["auth_provider"],
        "roles": auth_db.get_user_role_names(row["id"]),
        "last_login_at": row["last_login_at"],
        "locked": auth_db.is_locked(row),
    }


def _audit_filters(since: float, until: float | None, user_id: int | None, action: str | None,
                   result: str | None, ip: str | None, q: str | None) -> dict:
    clean = lambda s: (s or "").strip()[:200] or None
    return {"since": since, "until": until, "user_id": user_id, "action": clean(action),
            "result": clean(result), "ip": clean(ip), "text": clean(q)}

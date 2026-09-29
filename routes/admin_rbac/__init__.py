"""routes/admin_rbac - user/role/permission management and audit log (admin-only)."""

from fastapi import APIRouter

from .audit_endpoints import (
    clear_all_data,
    export_audit_log,
    get_audit_facets,
    get_audit_log,
    router as audit_router,
)
from .helpers import (
    _audit_filters,
    _check_assignable_roles,
    _check_can_modify,
    _role_permission_keys,
    _user_public,
)
from .models import (
    ClearAllDataBody,
    CreateRoleBody,
    CreateUserBody,
    RolePermissionsBody,
    UpdateUserBody,
)
from .role_endpoints import (
    create_role,
    list_permissions,
    list_role_names,
    list_roles,
    list_user_names,
    router as role_router,
    update_role_permissions,
)
from .user_endpoints import (
    create_user,
    delete_user,
    list_users,
    router as user_router,
    update_user,
)

router = APIRouter(prefix="/admin", tags=["admin"])
router.include_router(user_router)
router.include_router(role_router)
router.include_router(audit_router)

__all__ = [
    "router",
    # models
    "CreateUserBody", "UpdateUserBody", "CreateRoleBody", "RolePermissionsBody", "ClearAllDataBody",
    # helpers
    "_role_permission_keys", "_check_assignable_roles", "_check_can_modify", "_user_public", "_audit_filters",
    # user endpoints
    "list_users", "create_user", "update_user", "delete_user",
    # role endpoints
    "list_role_names", "list_user_names", "list_roles", "list_permissions", "create_role",
    "update_role_permissions",
    # audit endpoints
    "clear_all_data", "get_audit_log", "get_audit_facets", "export_audit_log",
]

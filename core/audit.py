"""core/audit.py - append-only audit trail helper (Bangladesh Bank / PCI-DSS auditor visibility)."""

import json
from typing import Optional

from . import auth_db
from .auth import Principal


def audit_log(principal: Optional[Principal], action: str, resource: Optional[str] = None,
               permission_key: Optional[str] = None, result: str = "allow",
               detail: Optional[dict] = None, ip: Optional[str] = None) -> None:
    auth_db.insert_audit(
        user_id=principal.id if principal else None,
        username=principal.username if principal else None,
        action=action, resource=resource, permission_key=permission_key, result=result,
        detail=json.dumps(detail) if detail else None, ip=ip,
    )

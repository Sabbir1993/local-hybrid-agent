"""core/audit.py - append-only audit trail helper (Bangladesh Bank / PCI-DSS auditor visibility)."""

import json
from typing import Optional
from contextvars import ContextVar

from . import auth_db
from .auth import Principal

# Thread-local / async context storage for the client IP of the current active HTTP request
_client_ip_ctx: ContextVar[Optional[str]] = ContextVar("audit_client_ip", default=None)

def set_current_ip(ip: Optional[str]) -> None:
    """Store the client IP for the current request execution context."""
    if ip:
        _client_ip_ctx.set(ip)

def get_current_ip() -> Optional[str]:
    """Retrieve the client IP associated with the current request execution context."""
    return _client_ip_ctx.get()

def audit_log(principal: Optional[Principal], action: str, resource: Optional[str] = None,
               permission_key: Optional[str] = None, result: str = "allow",
               detail: Optional[dict] = None, ip: Optional[str] = None) -> None:
    # Ensure IP is never empty or None - fallback to context IP or loopback
    resolved_ip = ip or get_current_ip() or "127.0.0.1"
    auth_db.insert_audit(
        user_id=principal.id if principal else None,
        username=principal.username if principal else None,
        action=action, resource=resource, permission_key=permission_key, result=result,
        detail=json.dumps(detail) if detail else None, ip=resolved_ip,
    )

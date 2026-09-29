"""Audit log and data management endpoints for the admin router."""

import csv
import io
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import Response

from core import auth_db, agent_tools
from core.audit import audit_log
from core.auth import Principal
from core.db import db_clear_all_projects_data
from core.deps import get_current_user, require_permission
from core.memory import clear_chat_history_chunks
from .helpers import _audit_filters
from .models import ClearAllDataBody

router = APIRouter()


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
async def get_audit_log(since: float = 0.0, until: float | None = None, user_id: int | None = None,
                        action: str | None = None, result: str | None = None,
                        ip: str | None = None, q: str | None = None,
                        page: int = 1, page_size: int = 50,
                        user: Principal = Depends(require_permission("audit.view"))):
    page = max(1, page)
    page_size = max(10, min(200, page_size))
    filters = _audit_filters(since, until, user_id, action, result, ip, q)
    out = auth_db.query_audit(page=page, page_size=page_size, **filters)
    return {**out, "page": page, "page_size": page_size}


@router.get("/audit_log/facets")
async def get_audit_facets(user: Principal = Depends(require_permission("audit.view"))):
    return auth_db.audit_facets()


@router.get("/audit_log/export")
async def export_audit_log(since: float = 0.0, until: float | None = None, user_id: int | None = None,
                           action: str | None = None, result: str | None = None,
                           ip: str | None = None, q: str | None = None,
                           user: Principal = Depends(require_permission("audit.view"))):
    filters = _audit_filters(since, until, user_id, action, result, ip, q)
    rows = auth_db.iter_audit(**filters)
    # exporting the trail is itself an auditable event
    audit_log(user, action="audit.export", permission_key="audit.view", result="allow",
              detail={"rows": len(rows), **{k: v for k, v in filters.items() if v}})
    buf = io.StringIO()
    w = csv.writer(buf)
    cols = ["id", "ts", "time_utc", "user_id", "username", "action", "resource",
            "permission_key", "result", "ip", "detail"]
    w.writerow(cols)
    for r in rows:
        stamp = datetime.fromtimestamp(r["ts"], tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
        # neutralize spreadsheet formula injection (=, +, -, @ at cell start)
        vals = [r["id"], r["ts"], stamp] + [r.get(c) for c in cols[3:]]
        w.writerow([("'" + v) if isinstance(v, str) and v[:1] in ("=", "+", "-", "@") else v for v in vals])
    fname = f"audit_log_{datetime.now().strftime('%Y%m%d_%H%M%S')}.csv"
    return Response(buf.getvalue(), media_type="text/csv",
                    headers={"Content-Disposition": f'attachment; filename="{fname}"',
                             "Cache-Control": "no-store"})

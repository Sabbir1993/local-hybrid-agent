import hashlib
import mimetypes
import uuid
from pathlib import Path
from typing import Optional
from fastapi import APIRouter, Depends, File as FastAPIFile, UploadFile
from fastapi.responses import FileResponse, JSONResponse
from core import auth_db, knowledge_ingest, knowledge_rules
from core.audit import audit_log
from core.auth import Principal
from core.config import KNOWLEDGE_UPLOADS_DIR
from core.deps import require_permission
from core.memory import delete_knowledge_chunks
from .chunk_upload import router as chunk_router
from .helpers import (
    _finish_ingest,
    _source_public,
)
from .models import (
    MAX_KB_FILE_BYTES,
    BulkDeleteBody,
    CategoryBody,
    CloudAccessBody,
    RuleBody,
    RuleEnabledBody,
    RuleTestBody,
    SourceCategoryBody,
    WebPolicyBody,
    RoleAccessBody,
    TextBody,
    UrlBody,
)

router = APIRouter(prefix="/knowledge", tags=["knowledge"])
router.include_router(chunk_router)


@router.get("")
async def list_sources(user: Principal = Depends(require_permission("knowledge.manage"))):
    from core.knowledge_access import kb_local_only
    return {"sources": [_source_public(r) for r in auth_db.list_knowledge_sources()],
            "cloud_policy": "local_only" if kb_local_only() else "allow"}


@router.post("/text")
async def add_text_source(body: TextBody, user: Principal = Depends(require_permission("knowledge.manage"))):
    source_id = auth_db.create_knowledge_source(title=body.title, kind="text", created_by=user.id)
    result = await _finish_ingest(source_id, body.text, user)
    return {**result, "source": _source_public(auth_db.get_knowledge_source(source_id))}


@router.post("/url")
async def add_url_source(body: UrlBody, user: Principal = Depends(require_permission("knowledge.manage"))):
    source_id = auth_db.create_knowledge_source(title=body.title, kind="url", origin=body.url, created_by=user.id)
    try:
        text = await knowledge_ingest.extract_url(body.url)
    except Exception as e:
        auth_db.update_knowledge_source_status(source_id, "error", str(e))
        return JSONResponse({"ok": False, "error": str(e)}, status_code=400)
    result = await _finish_ingest(source_id, text, user)
    return {**result, "source": _source_public(auth_db.get_knowledge_source(source_id))}


@router.post("/upload")
async def upload_source(file: UploadFile = FastAPIFile(...), title: Optional[str] = None,
                        user: Principal = Depends(require_permission("knowledge.manage"))):
    fname = Path(file.filename or "upload").name
    ext = Path(fname).suffix.lower()
    if ext not in knowledge_ingest.EXTRACTORS:
        return JSONResponse({"error": f"unsupported file type: {ext}"}, status_code=400)
    KNOWLEDGE_UPLOADS_DIR.mkdir(parents=True, exist_ok=True)
    data = await file.read(MAX_KB_FILE_BYTES + 1)
    if len(data) > MAX_KB_FILE_BYTES:
        return JSONResponse({"error": "file too large - use the chunked upload"}, status_code=413)
    content_hash = hashlib.sha256(data).hexdigest()
    stored_name = f"{uuid.uuid4().hex}{ext}"
    dest = KNOWLEDGE_UPLOADS_DIR / stored_name
    dest.write_bytes(data)
    source_id = auth_db.create_knowledge_source(
        title=title or fname, kind="file", origin=fname, stored_path=str(dest),
        content_hash=content_hash, created_by=user.id,
    )
    try:
        text = knowledge_ingest.extract_file(dest)
    except Exception as e:
        auth_db.update_knowledge_source_status(source_id, "error", str(e))
        return JSONResponse({"ok": False, "error": str(e)}, status_code=400)
    result = await _finish_ingest(source_id, text, user)
    return {**result, "source": _source_public(auth_db.get_knowledge_source(source_id))}


@router.put("/{source_id}/access")
async def set_access(source_id: int, body: RoleAccessBody,
                     user: Principal = Depends(require_permission("knowledge.manage"))):
    if not auth_db.get_knowledge_source(source_id):
        return JSONResponse({"error": "source not found"}, status_code=404)
    auth_db.set_source_role_access(source_id, body.roles, granted_by=user.id)
    audit_log(user, action="knowledge.access", resource=str(source_id), detail={"roles": body.roles}, result="allow")
    return {"ok": True, "source": _source_public(auth_db.get_knowledge_source(source_id))}


@router.put("/{source_id}/cloud")
async def set_cloud_access(source_id: int, body: CloudAccessBody,
                           user: Principal = Depends(require_permission("knowledge.manage"))):
    """Admin decides per source whether cloud models may read it (default: local models only)."""
    if not auth_db.get_knowledge_source(source_id):
        return JSONResponse({"error": "source not found"}, status_code=404)
    auth_db.set_source_cloud_ok(source_id, body.allowed)
    audit_log(user, action="knowledge.cloud_access", resource=str(source_id),
              detail={"cloud_ok": body.allowed}, result="allow")
    return {"ok": True, "source": _source_public(auth_db.get_knowledge_source(source_id))}


_MGR = require_permission("knowledge.manage")


@router.get("/policy")
async def cloud_policy(user: Principal = Depends(_MGR)):
    """Categories + sensitive-content rules that decide what cloud models may read."""
    from core.knowledge_access import kb_local_only
    return {"cloud_policy": "local_only" if kb_local_only() else "allow",
            "categories": auth_db.list_knowledge_categories(),
            "rules": auth_db.list_knowledge_rules(),
            "web": _web_policy()}


def _web_policy() -> dict:
    from core import kb_coverage
    from core.small_model import APP_CONFIG
    custom = (APP_CONFIG.get("chat") or {}).get("web_budget")
    custom = custom if isinstance(custom, dict) else {}
    budget = {row: {t: kb_coverage.web_budget("none" if row == "open" else row, t, explicit=(row == "open"))
                    for t in kb_coverage.TIERS} for row in kb_coverage.DEFAULT_BUDGET}
    return {"gap_fill": kb_coverage.gap_fill_enabled(),
            "full_cos": float((APP_CONFIG.get("knowledge") or {}).get("full_cos", 0.70)),
            "budget": budget, "tiers": list(kb_coverage.TIERS)}


@router.put("/web-policy")
async def save_web_policy(body: WebPolicyBody, user: Principal = Depends(_MGR)):
    """When the web is offered next to the knowledge base, and how many calls each reasoning tier may use."""
    from core import kb_coverage
    from core.config import update_app_config
    from core.small_model import APP_CONFIG
    clean = None
    if body.budget is not None:
        clean = {}
        for row, tiers in body.budget.items():
            if row not in kb_coverage.DEFAULT_BUDGET or not isinstance(tiers, dict):
                return JSONResponse({"error": f"unknown budget row: {row}"}, status_code=400)
            clean[row] = {}
            for t, v in tiers.items():
                try:
                    n = int(v)
                except (TypeError, ValueError):
                    return JSONResponse({"error": f"{row}/{t} must be a whole number"}, status_code=400)
                if t not in kb_coverage.TIERS or not 0 <= n <= 100:
                    return JSONResponse({"error": f"{row}/{t}: use 0-100 for low, medium, high or deep"}, status_code=400)
                clean[row][t] = n
    if body.full_cos is not None and not 0.5 <= body.full_cos <= 0.95:
        return JSONResponse({"error": "full match score must be between 0.50 and 0.95"}, status_code=400)

    def _apply(cfg):
        k = cfg.setdefault("knowledge", {})
        if body.gap_fill is not None:
            k["web_gap_fill"] = body.gap_fill
        if body.full_cos is not None:
            k["full_cos"] = round(body.full_cos, 2)
        if clean is not None:
            wb = cfg.setdefault("chat", {}).setdefault("web_budget", {})
            for row, tiers in clean.items():
                wb.setdefault(row, {}).update(tiers)

    cfg = update_app_config(_apply)
    APP_CONFIG["knowledge"] = cfg.get("knowledge", {})
    APP_CONFIG["chat"] = cfg.get("chat", {})
    audit_log(user, action="knowledge.web_policy", resource="web_gap_fill",
              detail={"gap_fill": body.gap_fill, "full_cos": body.full_cos, "budget": clean}, result="allow")
    return {"ok": True, "web": _web_policy()}


@router.put("/categories")
async def save_category(body: CategoryBody, user: Principal = Depends(_MGR)):
    name = " ".join(body.name.split())[:40]
    if not name:
        return JSONResponse({"error": "give the category a name"}, status_code=400)
    auth_db.upsert_knowledge_category(name, body.cloud_ok)
    audit_log(user, action="knowledge.category", resource=name, detail={"cloud_ok": body.cloud_ok}, result="allow")
    return {"ok": True, "categories": auth_db.list_knowledge_categories()}


@router.delete("/categories/{name}")
async def delete_category(name: str, user: Principal = Depends(_MGR)):
    auth_db.delete_knowledge_category(name)
    audit_log(user, action="knowledge.category_delete", resource=name, result="allow")
    return {"ok": True, "categories": auth_db.list_knowledge_categories()}


@router.put("/{source_id}/category")
async def set_source_category(source_id: int, body: SourceCategoryBody, user: Principal = Depends(_MGR)):
    if not auth_db.get_knowledge_source(source_id):
        return JSONResponse({"error": "source not found"}, status_code=404)
    cat = (body.category or "").strip() or None
    if cat and cat not in {c["name"] for c in auth_db.list_knowledge_categories()}:
        return JSONResponse({"error": "unknown category"}, status_code=400)
    auth_db.set_source_category(source_id, cat)
    audit_log(user, action="knowledge.source_category", resource=str(source_id), detail={"category": cat}, result="allow")
    return {"ok": True, "source": _source_public(auth_db.get_knowledge_source(source_id))}


@router.post("/rules")
async def add_rule(body: RuleBody, user: Principal = Depends(_MGR)):
    name, pattern = " ".join(body.name.split())[:60], body.pattern.strip() if body.kind == "keywords" else body.pattern
    if not name:
        return JSONResponse({"error": "give the rule a name"}, status_code=400)
    why = knowledge_rules.validate(body.kind, pattern)
    if why:
        return JSONResponse({"error": why}, status_code=400)
    try:
        rid = auth_db.add_knowledge_rule(name, body.kind, pattern)
    except Exception:
        return JSONResponse({"error": "a rule with that name already exists"}, status_code=409)
    audit_log(user, action="knowledge.rule_add", resource=name, detail={"kind": body.kind}, result="allow")
    return {"ok": True, "id": rid, "rules": auth_db.list_knowledge_rules()}


@router.put("/rules/{rule_id}")
async def toggle_rule(rule_id: int, body: RuleEnabledBody, user: Principal = Depends(_MGR)):
    if not auth_db.set_knowledge_rule_enabled(rule_id, body.enabled):
        return JSONResponse({"error": "rule not found"}, status_code=404)
    audit_log(user, action="knowledge.rule_toggle", resource=str(rule_id), detail={"enabled": body.enabled}, result="allow")
    return {"ok": True, "rules": auth_db.list_knowledge_rules()}


@router.delete("/rules/{rule_id}")
async def delete_rule(rule_id: int, user: Principal = Depends(_MGR)):
    if not auth_db.delete_knowledge_rule(rule_id):
        return JSONResponse({"error": "rule not found, or it is a built-in rule (turn it off instead)"}, status_code=404)
    audit_log(user, action="knowledge.rule_delete", resource=str(rule_id), result="allow")
    return {"ok": True, "rules": auth_db.list_knowledge_rules()}


@router.post("/rules/test")
async def test_rules(body: RuleTestBody, user: Principal = Depends(_MGR)):
    """Which enabled rules a pasted snippet trips (nothing is stored)."""
    return {"matches": knowledge_rules.matches(body.text[:20000])}


@router.get("/{source_id}/file")
async def get_source_file(source_id: int, user: Principal = Depends(require_permission("knowledge.manage"))):
    """Serve the original uploaded file for preview/download in the admin UI."""
    row = auth_db.get_knowledge_source(source_id)
    if not row or row["kind"] != "file" or not row["stored_path"]:
        return JSONResponse({"error": "no file for this source"}, status_code=404)
    p = Path(row["stored_path"])
    if not p.exists():
        return JSONResponse({"error": "file missing on disk"}, status_code=404)
    media_type = mimetypes.guess_type(row["origin"] or p.name)[0] or "application/octet-stream"
    return FileResponse(str(p), media_type=media_type, filename=row["origin"] or p.name,
                        content_disposition_type="inline")


@router.post("/{source_id}/reindex")
async def reindex_source(source_id: int, user: Principal = Depends(require_permission("knowledge.manage"))):
    row = auth_db.get_knowledge_source(source_id)
    if not row:
        return JSONResponse({"error": "source not found"}, status_code=404)
    if row["kind"] == "file" and row["stored_path"] and Path(row["stored_path"]).exists():
        try:
            text = knowledge_ingest.extract_file(Path(row["stored_path"]))
        except Exception as e:
            auth_db.update_knowledge_source_status(source_id, "error", str(e))
            return JSONResponse({"ok": False, "error": str(e)}, status_code=400)
    elif row["kind"] == "url" and row["origin"]:
        try:
            text = await knowledge_ingest.extract_url(row["origin"])
        except Exception as e:
            auth_db.update_knowledge_source_status(source_id, "error", str(e))
            return JSONResponse({"ok": False, "error": str(e)}, status_code=400)
    else:
        return JSONResponse({"error": "this source kind cannot be reindexed automatically (re-add pasted text instead)"}, status_code=400)
    result = await _finish_ingest(source_id, text, user)
    return {**result, "source": _source_public(auth_db.get_knowledge_source(source_id))}


@router.post("/bulk-delete")
async def bulk_delete_sources(body: BulkDeleteBody, user: Principal = Depends(require_permission("knowledge.manage"))):
    deleted = 0
    for source_id in body.ids:
        row = auth_db.get_knowledge_source(source_id)
        if not row:
            continue
        if row["kind"] == "file" and row["stored_path"]:
            try:
                Path(row["stored_path"]).unlink(missing_ok=True)
            except Exception:
                pass
        delete_knowledge_chunks(source_id)
        auth_db.delete_knowledge_source(source_id)
        audit_log(user, action="knowledge.delete", resource=row["title"], result="allow")
        deleted += 1
    return {"ok": True, "deleted": deleted}


@router.delete("/{source_id}")
async def delete_source(source_id: int, user: Principal = Depends(require_permission("knowledge.manage"))):
    row = auth_db.get_knowledge_source(source_id)
    if not row:
        return JSONResponse({"error": "source not found"}, status_code=404)
    if row["kind"] == "file" and row["stored_path"]:
        try:
            Path(row["stored_path"]).unlink(missing_ok=True)
        except Exception:
            pass
    delete_knowledge_chunks(source_id)
    auth_db.delete_knowledge_source(source_id)
    audit_log(user, action="knowledge.delete", resource=row["title"], result="allow")
    return {"ok": True}

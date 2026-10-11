import base64
import hashlib
import io
from typing import Any, Dict, Optional, Tuple

from .. import companion_bridge
from ..request_context import get_current_user_id
from ..small_model import describe_image_bytes
from .workspace import _remote_uid, _ws_resolve

MAX_IMAGE_BYTES = 10 * 1024 * 1024
MAX_IMAGE_DIM = 1536
_IMAGE_CACHE_MAX = 50
_image_analysis_cache: Dict[Tuple[str, str], str] = {}


def optimize_image_bytes(raw: bytes, max_dim: int = MAX_IMAGE_DIM) -> Tuple[bytes, Dict[str, Any]]:
    """Downsample high-resolution images to conserve vision tokens and prevent latency timeouts.

    Maintains aspect ratio and respects alpha channels. Returns (optimized_bytes, meta).
    """
    if not raw:
        return raw, {"scaled": False}
    try:
        from PIL import Image
        with Image.open(io.BytesIO(raw)) as img:
            orig_w, orig_h = img.size
            if orig_w <= max_dim and orig_h <= max_dim:
                return raw, {"orig_w": orig_w, "orig_h": orig_h, "w": orig_w, "h": orig_h, "scaled": False}

            scale = min(max_dim / float(orig_w), max_dim / float(orig_h))
            new_w = max(1, int(orig_w * scale))
            new_h = max(1, int(orig_h * scale))

            resample_filter = getattr(Image.Resampling, "LANCZOS", getattr(Image, "LANCZOS", 1))
            resized = img.resize((new_w, new_h), resample=resample_filter)

            out = io.BytesIO()
            fmt = (img.format or "PNG").upper()
            if fmt in ("JPEG", "JPG"):
                if resized.mode in ("RGBA", "P"):
                    resized = resized.convert("RGB")
                resized.save(out, format="JPEG", quality=85, optimize=True)
            else:
                resized.save(out, format="PNG", optimize=True)

            scaled_bytes = out.getvalue()
            return scaled_bytes, {
                "orig_w": orig_w,
                "orig_h": orig_h,
                "w": new_w,
                "h": new_h,
                "scaled": True,
            }
    except Exception:
        return raw, {"scaled": False}


async def tool_analyze_image(args: dict) -> str:
    p = _ws_resolve(args["path"])
    data = await companion_bridge.call(_remote_uid(), "fs.read_b64", {"path": str(p)})
    if not data.get("data"):
        return f"error: File not found: '{args['path']}'"
    raw = base64.b64decode(data["data"])
    if len(raw) > MAX_IMAGE_BYTES:
        return f"error: image too large ({len(raw)} bytes, max {MAX_IMAGE_BYTES})"

    question = str(args.get("question", "Describe this image in detail for a coding agent.")).strip()

    # Pre-scale image to fit vision token budget
    optimized_bytes, _meta = optimize_image_bytes(raw, max_dim=MAX_IMAGE_DIM)

    # Hash-based cache lookup
    img_hash = hashlib.sha256(raw).hexdigest()
    cache_key = (img_hash, question)
    if cache_key in _image_analysis_cache:
        return _image_analysis_cache[cache_key]

    desc = await describe_image_bytes(optimized_bytes, question)
    if desc and not desc.startswith("error:"):
        if len(_image_analysis_cache) >= _IMAGE_CACHE_MAX:
            _image_analysis_cache.pop(next(iter(_image_analysis_cache)))
        _image_analysis_cache[cache_key] = desc
    return desc


async def tool_search_memory(args: dict) -> str:
    query = args.get("query", "")
    if not query:
        raise ValueError("query required")
    try:
        from ..memory import ensure_indexed, search_memory_hybrid
        await ensure_indexed(max_age_s=900)
        results = await search_memory_hybrid(query, k=8, requesting_user_id=get_current_user_id())
    except Exception as e:
        return f"error: memory search unavailable: {type(e).__name__}: {e}"
    if not results:
        return "(no matches in project memory - the index may still be building)"
    lines = []
    for r in results:
        where = r["path"] if r["source"] == "workspace" else f"{r['path']} (past session)"
        snippet = " ".join(str(r["text"]).split())[:220]
        lines.append(f"[{where}] score {r['score']:.2f}\n  {snippet}")
    return "\n".join(lines)


async def tool_search_knowledge_base(args: dict) -> str:
    query = args.get("query", "")
    if not query:
        raise ValueError("query required")
    from ..knowledge_access import KB_CLOUD_BLOCKED_MSG, kb_cloud_blocked, cloud_ok_ids
    uid = get_current_user_id()
    from ..auth import _to_principal
    from ..knowledge_access import allowed_source_ids_for
    user = _to_principal(uid) if uid is not None else None
    kb_ids = allowed_source_ids_for(user)
    if kb_cloud_blocked():                 # a cloud model reads the result: only admin-approved sources
        kb_ids = cloud_ok_ids(kb_ids)
        if not kb_ids:
            return KB_CLOUD_BLOCKED_MSG
    if not kb_ids:
        return "(no accessible company knowledge base sources for this user account)"
    from ..knowledge_router import fetch_company_knowledge
    hits, _ = await fetch_company_knowledge(query, allowed_source_ids=kb_ids, k=6)
    if kb_cloud_blocked():                 # also drop chunks that trip a sensitive-content rule
        from ..knowledge_access import cloud_safe_hits
        held = len(hits)
        hits = cloud_safe_hits(hits)
        if held and not hits:
            return KB_CLOUD_BLOCKED_MSG
    from ..knowledge_access import partial_notice
    note = partial_notice(kb_ids)
    if not hits:
        return f"(no matching company knowledge base records found for '{query}')" + (f"\n{note}" if note else "")
    lines = []
    for h in hits:
        title = h.get("title") or f"Source #{h.get('source_id')}"
        lines.append(f"[{title}] (score: {h['score']:.2f})\n{h['text']}")
    return "\n\n---\n\n".join(lines) + (f"\n\n{note}" if note else "")


async def tool_kb_find_mentions(args: dict) -> str:
    """Every document line that mentions a person or term, not just the 6 best semantic chunks."""
    name = " ".join(str(args.get("name") or "").split())
    if not name:
        raise ValueError("name required")
    from ..knowledge_access import KB_CLOUD_BLOCKED_MSG, kb_cloud_blocked, cloud_ok_ids, allowed_source_ids_for
    from ..auth import _to_principal
    uid = get_current_user_id()
    user = _to_principal(uid) if uid is not None else None
    kb_ids = allowed_source_ids_for(user)
    if kb_cloud_blocked():
        kb_ids = cloud_ok_ids(kb_ids)
        if not kb_ids:
            return KB_CLOUD_BLOCKED_MSG
    if not kb_ids:
        return "(no accessible company knowledge base sources for this user account)"
    from .. import kb_mentions
    from ..auth_db import list_knowledge_sources
    from ..memory import _cached_entries, _knowledge_id_from_path
    titles = {ks["id"]: ks["title"] for ks in list_knowledge_sources()
              if ks["id"] in kb_ids and ks["status"] == "ready"}
    result = kb_mentions.find_mentions(name, _cached_entries()["entries"], titles,
                                       _knowledge_id_from_path, set(titles))
    if kb_cloud_blocked():                 # a cloud model reads the result: drop sensitive lines like the search tool does
        from ..knowledge_access import cloud_safe_hits
        for d in result["docs"]:
            kept = cloud_safe_hits([{"source_id": d["source_id"], "text": ln} for ln in d["lines"]])
            d["lines"] = [h["text"] for h in kept]
        result["docs"] = [d for d in result["docs"] if d["lines"]]
    try:                                   # who looked up whom is personal-data access: audit it, store only a hash
        import hashlib
        from ..audit import audit_log
        audit_log(user, "kb.find_mentions", resource="knowledge",
                  detail={"query_sha256": hashlib.sha256(name.lower().encode()).hexdigest()[:16],
                          "documents": len(result["docs"])})
    except Exception:
        pass
    from ..knowledge_access import partial_notice
    note = partial_notice(set(titles))
    return kb_mentions.render(name, result) + (f"\n\n{note}" if note else "")


async def tool_kb_query(args: dict) -> str:
    """Exact counts, lists and group-bys over the Excel/CSV sources the user may read."""
    import json as _json
    from ..knowledge_access import KB_CLOUD_BLOCKED_MSG, kb_cloud_blocked, cloud_ok_ids, allowed_source_ids_for
    from ..auth import _to_principal
    uid = get_current_user_id()
    user = _to_principal(uid) if uid is not None else None
    kb_ids = allowed_source_ids_for(user)
    cloud = kb_cloud_blocked()
    if cloud:
        kb_ids = cloud_ok_ids(kb_ids)
        if not kb_ids:
            return KB_CLOUD_BLOCKED_MSG
    if not kb_ids:
        return "(no accessible company knowledge base sources for this user account)"
    from .. import kb_tables
    from ..auth_db import list_knowledge_sources
    titles = {ks["id"]: ks["title"] for ks in list_knowledge_sources()
              if ks["id"] in kb_ids and ks["status"] == "ready"}
    spec = {k: args.get(k) for k in ("source", "sheet", "filters", "select", "group_by", "count", "limit")}
    if args.get("describe") or not any(spec.get(k) for k in ("filters", "select", "group_by", "count")):
        return kb_tables.describe(set(titles), titles, str(spec.get("source") or ""))
    try:
        res = kb_tables.run_query(spec, set(titles), titles)
    except ValueError as e:
        return f"error: {e}"
    if cloud:                              # drop rows a sensitive-content rule holds back, as the search tool does
        from ..knowledge_access import cloud_safe_hits
        for t in res["tables"]:
            kept = cloud_safe_hits([{"source_id": 0, "text": " | ".join(map(str, v))} for _, v in t["rows"]])
            keep = {h["text"] for h in kept}
            t["rows"] = [(n, v) for n, v in t["rows"] if " | ".join(map(str, v)) in keep]
    try:                                   # filter values can be names: audit that a query ran, store only a hash
        import hashlib
        from ..audit import audit_log
        digest = hashlib.sha256(_json.dumps(spec, sort_keys=True, default=str).encode()).hexdigest()[:16]
        audit_log(user, "kb.query", resource="knowledge",
                  detail={"spec_sha256": digest, "tables": len(res["tables"]), "rows": res["total"]})
    except Exception:
        pass
    from ..knowledge_access import partial_notice
    note = partial_notice(set(titles))
    return kb_tables.render(spec, res) + (f"\n\n{note}" if note else "")

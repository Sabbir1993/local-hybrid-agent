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
    if not hits:
        return f"(no matching company knowledge base records found for '{query}')"
    lines = []
    for h in hits:
        title = h.get("title") or f"Source #{h.get('source_id')}"
        lines.append(f"[{title}] (score: {h['score']:.2f})\n{h['text']}")
    return "\n\n---\n\n".join(lines)

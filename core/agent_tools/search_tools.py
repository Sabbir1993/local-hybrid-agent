import base64

from .. import companion_bridge
from ..request_context import get_current_user_id
from ..small_model import describe_image_bytes
from .workspace import _remote_uid, _ws_resolve

MAX_IMAGE_BYTES = 10 * 1024 * 1024


async def tool_analyze_image(args: dict) -> str:
    p = _ws_resolve(args["path"])
    data = await companion_bridge.call(_remote_uid(), "fs.read_b64", {"path": str(p)})
    if not data.get("data"):
        return f"error: File not found: '{args['path']}'"
    raw = base64.b64decode(data["data"])
    if len(raw) > MAX_IMAGE_BYTES:
        return f"error: image too large ({len(raw)} bytes, max {MAX_IMAGE_BYTES})"
    return await describe_image_bytes(raw, args.get("question", "Describe this image in detail for a coding agent."))


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
    from ..knowledge_access import KB_CLOUD_BLOCKED_MSG, kb_cloud_blocked
    if kb_cloud_blocked():
        return KB_CLOUD_BLOCKED_MSG
    uid = get_current_user_id()
    from ..auth import _to_principal
    from ..knowledge_access import allowed_source_ids_for
    user = _to_principal(uid) if uid is not None else None
    kb_ids = allowed_source_ids_for(user)
    if not kb_ids:
        return "(no accessible company knowledge base sources for this user account)"
    from ..knowledge_router import fetch_company_knowledge
    hits, _ = await fetch_company_knowledge(query, allowed_source_ids=kb_ids, k=6)
    if not hits:
        return f"(no matching company knowledge base records found for '{query}')"
    lines = []
    for h in hits:
        title = h.get("title") or f"Source #{h.get('source_id')}"
        lines.append(f"[{title}] (score: {h['score']:.2f})\n{h['text']}")
    return "\n\n---\n\n".join(lines)

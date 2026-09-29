import re
import sys
from typing import Optional
from fastapi import Depends
from fastapi.responses import JSONResponse
from core.auth import Principal
from core.deps import get_current_user
from core.audit import audit_log
from core import input_guard
from core import output_guard
from core import cloud
from core.agent_loop import estimate_prompt_tokens
from core.db import db_load_messages, db_append_message

from .base import router
from .models import CompactRequest


_COMPACT_SYSTEM_PROMPT = (
    "You are a conversation summarizer. Your task is to create a detailed summary of the "
    "conversation so far, written so that a successor assistant can seamlessly continue the "
    "work with full context. Produce a structured markdown summary with these sections:\n"
    "1. **Primary Requests and Intent** — what the user asked for across the conversation, "
    "including verbatim key phrases of the original asks.\n"
    "2. **Key Technical Concepts & Details** — files, paths, function names, schemas, "
    "commands, configuration values, and any errors encountered (with how they were resolved).\n"
    "3. **Actions Taken & Results** — tools that were called, what succeeded or failed.\n"
    "4. **Decisions & Open Questions** — choices made and their rationale; anything unresolved.\n"
    "5. **Next Steps** — explicit, actionable continuation points.\n"
    "Rules: be dense and factual; preserve exact names/paths/numbers; do not omit constraints "
    "the user stated; do not add commentary or greet anyone; output ONLY the summary."
)


def _strip_think(text: str) -> str:
    m = re.search(r"<think>[\s\S]*?</think>", text or "")
    return (m.group(0)[7:-8] if m else (text or "")).strip()


async def _summarize_history(convo: list, instructions: Optional[str], use_executor: bool,
                              user_id: Optional[int] = None) -> str:
    """One non-streaming LLM call that writes the conversation summary.
    Main model when running, else the on-demand executor small model."""
    transcript_lines = []
    for m in convo:
        c = str(m.get("content") or "").strip()
        if len(c) > 4000:
            c = c[:4000] + "\n... (truncated)"
        transcript_lines.append(f"[{m.get('role', '?').upper()}]\n{c}")
    user_payload = (
        "Summarize the conversation below.\n\n"
        + (f"Extra instructions from the user (honor these): {instructions}\n\n" if instructions else "")
        + "CONVERSATION:\n" + "\n\n".join(transcript_lines)
    )
    payload = {
        "messages": [
            {"role": "system", "content": _COMPACT_SYSTEM_PROMPT},
            {"role": "user", "content": user_payload},
        ],
        "max_tokens": 2048,
        "temperature": 0.1,
        "stream": False,
    }

    # "Summarizing chats" job (core/lanes.py). Unless the user mapped that job to
    # a model in Settings -> Models, the main model goes first (then the helper),
    # as before; use_executor forces the job's own route.
    from core import lanes
    if use_executor or "summarize" in cloud.role_map(user_id):
        data, used = await lanes.post_chat("summarize", payload, user_id)
    else:
        try:
            data, used = await lanes.post_chat("agent.reason", payload, user_id)
        except RuntimeError:
            data, used = await lanes.post_chat("summarize", payload, user_id)
    source = used.describe()
    summary = _strip_think(lanes.message_text(data))
    # Output sanitizer: summaries are user-facing and may replay earlier chat
    # content. user=None means role-targeted rules don't apply (global rules do).
    summary, _og = output_guard.redact_full(summary, None, source.startswith("cloud"))
    if not summary.strip():
        raise RuntimeError(f"summarizer returned an empty response ({source})")
    print(f"[chat/compact] summarized {len(convo)} messages via {source} "
          f"({len(summary)} chars summary)")
    return summary


@router.post("/chat/compact")
async def chat_compact(req: CompactRequest, user: Principal = Depends(get_current_user)):
    # Project gate: in agent mode /compact only works with an active project
    # (mirrors the frontend curProject gate — defense in depth).
    if req.agent_mode and not req.project_id:
        return JSONResponse(
            {"error": "Select a project first — /compact in agent mode requires an active project"},
            status_code=400)

    # Source of truth: the DB session (preserves acts/reasoning meta); fallback
    # to the posted messages for sessions that were never persisted.
    if req.session_id:
        try:
            src = db_load_messages(req.session_id, owner_user_id=user.id)
        except PermissionError:
            return JSONResponse({"error": "session not found"}, status_code=404)
    else:
        src = [dict(m) for m in (req.messages or [])]
    convo = [m for m in src
             if m.get("role") in ("user", "assistant")
             and str(m.get("content") or "").strip()]
    if len(convo) < 2:
        return JSONResponse({"error": "Nothing to compact yet — send a few messages first"},
                            status_code=400)

    # the whole history (+ instructions) goes to a model, possibly a cloud one:
    # same input rules as /chat/run, or compaction is a way around them
    _any_cloud = bool(cloud.cloud_lane("executor", user.id) or cloud.cloud_lane("main", user.id))
    _hit = await input_guard.check_async(
        input_guard.message_texts(convo) + [str(req.instructions or "")], user, any_cloud_lane=_any_cloud)
    if _hit:
        audit_log(user, action="input_guard.block", resource=_hit.get("name"),
                  detail={"scope": _hit.get("scope"), "endpoint": "chat/compact",
                          "pattern": _hit.get("_matched_pattern")}, result="deny")
        return JSONResponse({"error": _hit.get("message")}, status_code=403)

    before_tokens = estimate_prompt_tokens(convo)
    try:
        summary = await _summarize_history(convo, req.instructions, req.use_executor, user.id)
    except Exception as e:
        print(f"[chat/compact] failed: {type(e).__name__}: {e}", file=sys.stderr)
        return JSONResponse({"error": "compact failed - see server log"}, status_code=500)

    keep_n = max(0, min(int(req.keep_last or 0), len(convo) - 1))
    kept = convo[-keep_n:] if keep_n else []

    compact_message = {
        "role": "system",
        "content": "[COMPACTED CONTEXT SUMMARY]\n" + summary,
        "meta": {"compact": True, "before_tokens": before_tokens,
                 "kept_messages": keep_n},
    }
    # after_tokens reflects what future turns will actually send: the summary
    # plus the verbatim kept tail (the tail isn't duplicated in storage — it
    # already exists in its original position; this is only for the badge).
    after_tokens = estimate_prompt_tokens([compact_message] + kept)
    reduction_pct = max(0, round((1 - after_tokens / before_tokens) * 100)) if before_tokens > 0 else 0
    compact_message["meta"]["after_tokens"] = after_tokens
    compact_message["meta"]["reduction_pct"] = reduction_pct

    if req.session_id:
        # Append-only: the compact marker is a new row, nothing is deleted, so
        # the full transcript stays visible/reloadable. See buildContextMessages()
        # in static/js/compact.js for how future turns pick up only the marker
        # forward instead of the full history.
        db_append_message(req.session_id, compact_message["role"],
                           compact_message["content"], compact_message["meta"],
                           owner_user_id=user.id)

    return {
        "summary": summary,
        "compact_message": compact_message,
        "before_tokens": before_tokens,
        "after_tokens": after_tokens,
        "reduction_pct": reduction_pct,
    }

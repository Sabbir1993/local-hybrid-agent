import json
import re
from typing import Optional
from core.audit import audit_log
from core.small_model import APP_CONFIG
from core.agent_tools import pop_file_diff
from core.grammar import build_tool_call_grammar


# ---------------- output sanitizer helpers ----------------
def _guard_flush_events(redactor, streamed_content) -> list:
    """Flush the output-guard holdback at end of a lane stream. Returns SSE
    chunks the caller must yield (tail delta + one-time guard notice)."""
    chunks = []
    _tail = redactor.flush()
    if _tail:
        streamed_content.append(_tail)
        chunks.append(f"event: delta\ndata: {json.dumps({'text': _tail})}\n\n")
    if redactor.matched:
        chunks.append(f"event: guard\ndata: "
                      + json.dumps({"rule": redactor.matched.get("name"),
                                    "message": redactor.matched.get("message")}) + "\n\n")
    return chunks


def _guard_audit(redactor, user, endpoint: str) -> None:
    if redactor.matched:
        audit_log(user, action="output_guard.redact", resource=redactor.matched.get("name"),
                  detail={"endpoint": endpoint, "scope": redactor.matched.get("scope"),
                          "hits": redactor.hits}, result="deny")


# Grammar-constrained tool calls for the executor lane (small-model reliability).
# Auto-disabled for the process lifetime if the llama-server build rejects the
# grammar or the envelope can't be verified. Kill switch: config/app.json
# router.executor_grammar: false.
_executor_grammar_disabled = False


def _executor_grammar(tools_for_lane: list) -> Optional[str]:
    """GBNF grammar constraining the executor's tool-call JSON (or None)."""
    global _executor_grammar_disabled
    if _executor_grammar_disabled or not APP_CONFIG.get("router", {}).get("executor_grammar", True):
        return None
    if not tools_for_lane:
        # nothing to constrain for this request (e.g. a custom agent's allowlist
        # matched no registered tool) -- not a sign the server rejects grammars
        return None
    g = build_tool_call_grammar(tools_for_lane)
    if g is None:
        _executor_grammar_disabled = True
    return g


_DOWNLOAD_MARKER_RE = re.compile(r"[ \t]*\[DOWNLOAD:[^\]]*\][ \t]*\n?")


def _strip_download_markers(text: str, was_synth: bool) -> tuple[str, bool]:
    """Agent files are written straight into the user's project, so the chat-mode
    [DOWNLOAD: x] preview/download badges don't apply (they only serve server
    common space). Returns (text, was_synth)."""
    cleaned = _DOWNLOAD_MARKER_RE.sub("", text or "").rstrip()
    if cleaned != (text or "").rstrip():
        return cleaned, True
    return text, was_synth


def _with_diff(payload: dict, args) -> dict:
    """Attach the per-call diff of a successful write_file/edit_file to its tool_result,
    and a screenshot thumbnail to browser/mobile screenshots (shown live, never persisted)."""
    if payload.get("ok") and payload.get("name") in ("write_file", "edit_file"):
        d = pop_file_diff(args if isinstance(args, dict) else {})
        if d:
            payload["diff"] = d
    if payload.get("name") in ("browser_screenshot", "mobile_screenshot"):
        from core.browser_tools import pop_thumbnail
        img = pop_thumbnail(payload["name"], args if isinstance(args, dict) else {})
        if img and payload.get("ok"):
            payload["image"] = img
    return payload

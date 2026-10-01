import re
from typing import Optional
from core.small_model import APP_CONFIG
from core.agent_tools import FILE_WRITE_TOOLS, pop_file_diff
from core.grammar import build_tool_call_grammar


# ---------------- output sanitizer helpers ----------------
# Moved to core/agent_loop/stream.py (single copy used by every lane stream).
# Re-exported here so existing importers (routes/agent/__init__.py, run.py) keep working.
from core.agent_loop.stream import guard_audit as _guard_audit
from core.agent_loop.stream import guard_flush_events as _guard_flush_events


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
    if payload.get("ok") and payload.get("name") in FILE_WRITE_TOOLS:
        d = pop_file_diff(args if isinstance(args, dict) else {})
        if d:
            payload["diff"] = d
    if payload.get("name") in ("browser_screenshot", "mobile_screenshot"):
        from core.browser_tools import pop_thumbnail
        img = pop_thumbnail(payload["name"], args if isinstance(args, dict) else {})
        if img and payload.get("ok"):
            payload["image"] = img
    return payload

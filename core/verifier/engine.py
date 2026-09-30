import asyncio
import re
import sys
from typing import Optional
from .config import _parse, effective_mode, evidence_from, settings
from .constants import MAX_DRAFT_CHARS, VERIFY_TIMEOUT_S, _REVISE, _SYSTEM


def _checker_label(t) -> str:
    try:
        from ..lanes import registry
        d = registry().get(t.lane) or {}
        if t.cm is not None:
            return t.cm.display
        return str(d.get("label") or t.lane)
    except Exception:
        return t.lane


async def verify_answer(question: str, draft: str, evidence: str = "",
                        user_id: Optional[int] = None) -> dict:
    """-> {verdict: pass|fail|unverified, issues, checker, source, confidence}. Never raises."""
    from .. import lanes, pan
    pan_issue = []
    try:
        if pan.contains_pan(draft):
            pan_issue = [{"severity": "major", "text": "The answer contains what looks like a card number."}]
    except Exception:
        pass
    payload = {
        "messages": [
            {"role": "system", "content": _SYSTEM},
            {"role": "user", "content": (
                f"QUESTION:\n{(question or '')[:4000]}\n\n"
                f"EVIDENCE:\n{evidence or '(none)'}\n\n"
                f"DRAFT:\n{(draft or '')[:MAX_DRAFT_CHARS]}")},
        ],
        "temperature": 0.0,
        "max_tokens": 1024,
        "stream": False,
    }
    checker, source = "", "local"
    try:
        data, t = await asyncio.wait_for(lanes.post_chat("verify", payload, user_id),
                                         timeout=VERIFY_TIMEOUT_S)
        checker, source = _checker_label(t), t.source
        parsed = _parse(lanes.message_text(data))
    except Exception as e:
        print(f"[verifier] check unavailable: {type(e).__name__}: {e}", file=sys.stderr)
        parsed = None
    if parsed is None:
        out = {"verdict": "fail" if pan_issue else "unverified", "issues": pan_issue, "confidence": None}
    else:
        out = parsed
        if pan_issue:
            out["verdict"] = "fail"
            out["issues"] = pan_issue + out["issues"]
    out.update(checker=checker or "answer checker", source=source)
    return out


def revision_messages(msgs: list, draft: str, issues: list) -> list:
    """The generator's own conversation + its draft + the reviewer's issues."""
    listed = "\n".join(f"- ({i.get('severity', 'minor')}) {i.get('text')}" for i in issues) or "- (unspecified)"
    # Only what a rewrite needs: the system prompt and the request being answered (plus the draft and the issues).
    # The whole conversation used to be re-sent here on the generator's (often cloud) client, for every attempt.
    plain = [m for m in msgs if m.get("role") in ("system", "user", "assistant") and not m.get("tool_calls")]
    sys_msg = next((m for m in plain if m.get("role") == "system"), None)
    users = [m for m in plain if m.get("role") == "user"]
    asked = next((m for m in reversed(users) if not str(m.get("content") or "").lstrip().startswith("[")),
                 users[-1] if users else None)        # skip loop-injected "[continue]" style turns
    base = [m for m in (sys_msg, asked) if m is not None]
    return base + [{"role": "assistant", "content": draft},
                   {"role": "user", "content": _REVISE.format(issues=listed)}]


async def revise(client, msgs: list, draft: str, issues: list, max_tokens: int = 4096) -> Optional[str]:
    """One non-streaming rewrite on the generator's own client. None on failure."""
    try:
        r = await client.post("/v1/chat/completions", json={
            "messages": revision_messages(msgs, draft, issues),
            "temperature": 0.2, "max_tokens": max_tokens, "stream": False}, timeout=None)
        r.raise_for_status()
        from ..lanes import message_text
        text = re.sub(r"<think>[\s\S]*?</think>", "", message_text(r.json())).strip()
        return text or None
    except Exception as e:
        print(f"[verifier] revision failed: {type(e).__name__}: {e}", file=sys.stderr)
        return None


async def check_and_fix(question: str, draft: str, msgs: list, client, mode: str,
                        user_id: Optional[int] = None):
    """Run the configured check. Yields ("verify_result", dict) and, in gate mode
    after a successful fix, ("revised", text) before the final verdict."""
    cfg = settings(user_id)
    evidence = evidence_from(msgs)
    result = await verify_answer(question, draft, evidence, user_id)
    rounds = cfg["max_rounds"] if mode == "gate" else 0
    fixed = 0
    current = draft
    while result["verdict"] == "fail" and rounds > 0 and client is not None:
        rounds -= 1
        new = await revise(client, msgs, current, result["issues"])
        if not new or new.strip() == current.strip():
            break
        fixed += len([i for i in result["issues"] if i.get("severity") == "major"]) or 1
        current = new
        yield ("revised", current)
        result = await verify_answer(question, current, evidence, user_id)
    result["fixed"] = fixed
    result["original"] = draft if fixed else None
    yield ("verify_result", result)


async def sse_events(user, surface: str, question: str, answer: str, msgs: list,
                     client, override: Optional[str], cloud_out: bool):
    """SSE chunks for chat/agent routes: verify_start, then (gate mode, on FAIL) a
    delta_reset + the revised answer, then verify_result. Nothing when mode is off."""
    from .. import lanes, output_guard
    from ..sse import sse
    mode = effective_mode(surface, answer, user.id, override)
    if mode == "off":
        return
    t = lanes.primary("verify", user.id)
    if t is None:
        yield sse("verify_result", {"verdict": "unverified", "issues": [], "checker": "",
                                    "note": "no model is available to check answers"})
        return
    yield sse("verify_start", {"mode": mode, "checker": _checker_label(t), "source": t.source})
    async for ev, val in check_and_fix(question, answer, msgs, client, mode, user.id):
        if ev == "revised":
            safe, _og = output_guard.redact_full(val, user, cloud_out)
            yield sse("delta_reset", {})
            yield sse("delta", {"text": safe})
        else:
            if val.get("original"):
                val["original"], _og = output_guard.redact_full(val["original"], user, cloud_out)
            yield sse("verify_result", val)

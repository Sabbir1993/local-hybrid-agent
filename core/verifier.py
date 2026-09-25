"""
core/verifier.py - optional "check the answer" pass before/after it reaches the user.

The "Checking answers" job (core/lanes.py; the main model by default) reads the
question, the draft answer and the evidence the answer was built from (tool
results, knowledge/web snippets), and returns a strict-JSON verdict:

  {"verdict": "pass" | "fail" | "unverified", "issues": [{"severity", "text"}],
   "checker": "<model label>", "source": "local" | "cloud"}

Modes (per user in Settings -> Models, per request via the shield toggle):
  off    - nothing runs
  badge  - the answer streams as usual; the verdict arrives afterwards as a badge
  gate   - the answer is held back ("Double-checking..."); on FAIL the generator
           revises once per round (max_rounds) and the fixed answer is shown

The check never blocks delivery: a slow, broken or unparseable checker yields
"unverified" and the answer is shown as-is. Card numbers in a draft are always
reported (core/pan.py), whatever the checker says.
"""

import asyncio
import json
import re
import sys
from typing import Optional

DEFAULTS = {"mode": "off", "apply_to": "both", "max_rounds": 1, "min_length": 120}
MODES = ("off", "badge", "gate")
APPLY_TO = ("both", "chat", "agent")
VERIFY_TIMEOUT_S = 90
MAX_EVIDENCE_CHARS = 6000
MAX_DRAFT_CHARS = 12000

_SYSTEM = (
    "You are a strict answer checker. You are given a user's QUESTION, the EVIDENCE the "
    "assistant had (tool results, documents, search results; may be empty) and the "
    "assistant's DRAFT answer. Check the draft for: (1) not actually answering the question; "
    "(2) claims contradicted by, or missing from, the evidence when the evidence should cover "
    "them (general knowledge is fine); (3) wrong code, math or logic; (4) leaked secrets, "
    "passwords, API keys or card numbers. Ignore style and length.\n"
    "Reply with ONLY one JSON object, no markdown:\n"
    '{"verdict": "PASS" or "FAIL", "issues": [{"severity": "major" or "minor", "text": '
    '"<one plain sentence>"}], "confidence": <0..1>}\n'
    "Use FAIL only for at least one major issue. PASS may list minor issues."
)

_REVISE = (
    "A reviewer found problems in your previous answer. Rewrite the complete answer so it "
    "fixes every issue below, keeps everything that was correct, and follows the same format. "
    "Output only the corrected answer - do not mention the review.\n\nISSUES:\n{issues}"
)


def settings(user_id: Optional[int] = None) -> dict:
    from . import cloud
    out = dict(DEFAULTS)
    try:
        out.update({k: v for k, v in (cloud.verification(user_id) or {}).items() if k in DEFAULTS})
    except Exception:
        pass
    if out["mode"] not in MODES:
        out["mode"] = "off"
    if out["apply_to"] not in APPLY_TO:
        out["apply_to"] = "both"
    out["max_rounds"] = max(0, min(3, int(out.get("max_rounds") or 0)))
    out["min_length"] = max(0, int(out.get("min_length") or 0))
    return out


def effective_mode(surface: str, answer: str, user_id: Optional[int] = None,
                   override: Optional[str] = None) -> str:
    """Mode for this answer: the request's shield toggle wins over the saved setting."""
    cfg = settings(user_id)
    mode = override if override in MODES else cfg["mode"]
    if mode == "off":
        return "off"
    if override not in MODES and cfg["apply_to"] not in ("both", surface):
        return "off"
    if len((answer or "").strip()) < cfg["min_length"]:
        return "off"
    return mode


def evidence_from(msgs: list) -> str:
    """Tool results + injected context from a chat/agent message list, newest first."""
    parts = []
    for m in reversed(msgs or []):
        role = m.get("role")
        c = m.get("content")
        if isinstance(c, list):
            c = " ".join(str(p.get("text") or "") for p in c if isinstance(p, dict))
        c = str(c or "").strip()
        if not c:
            continue
        if role == "tool":
            parts.append(f"[tool result]\n{c[:2000]}")
        elif role == "system" and ("KNOWLEDGE" in c.upper() or "SEARCH RESULT" in c.upper() or "SOURCE" in c.upper()):
            parts.append(f"[context]\n{c[-2500:]}")
        if sum(len(p) for p in parts) > MAX_EVIDENCE_CHARS:
            break
    return "\n\n".join(parts)[:MAX_EVIDENCE_CHARS]


def _parse(text: str) -> Optional[dict]:
    text = re.sub(r"<think>[\s\S]*?</think>", "", text or "").strip()
    text = re.sub(r"^```(?:json)?|```$", "", text, flags=re.M).strip()
    m = re.search(r"\{[\s\S]*\}", text)
    if not m:
        return None
    try:
        d = json.loads(m.group(0))
    except json.JSONDecodeError:
        return None
    if not isinstance(d, dict):
        return None
    verdict = str(d.get("verdict") or "").strip().lower()
    if verdict not in ("pass", "fail"):
        return None
    issues = []
    for it in d.get("issues") or []:
        if isinstance(it, dict) and str(it.get("text") or "").strip():
            sev = str(it.get("severity") or "minor").lower()
            issues.append({"severity": sev if sev in ("major", "minor") else "minor",
                           "text": str(it["text"]).strip()[:400]})
        elif isinstance(it, str) and it.strip():
            issues.append({"severity": "minor", "text": it.strip()[:400]})
    try:
        conf = float(d.get("confidence"))
    except (TypeError, ValueError):
        conf = None
    return {"verdict": verdict, "issues": issues[:8], "confidence": conf}


async def verify_answer(question: str, draft: str, evidence: str = "",
                        user_id: Optional[int] = None) -> dict:
    """-> {verdict: pass|fail|unverified, issues, checker, source, confidence}. Never raises."""
    from . import lanes, pan
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


def _checker_label(t) -> str:
    try:
        from .lanes import registry
        d = registry().get(t.lane) or {}
        if t.cm is not None:
            return t.cm.display
        return str(d.get("label") or t.lane)
    except Exception:
        return t.lane


def revision_messages(msgs: list, draft: str, issues: list) -> list:
    """The generator's own conversation + its draft + the reviewer's issues."""
    listed = "\n".join(f"- ({i.get('severity', 'minor')}) {i.get('text')}" for i in issues) or "- (unspecified)"
    base = [m for m in msgs if m.get("role") in ("system", "user", "assistant")
            and not m.get("tool_calls")]
    return base + [{"role": "assistant", "content": draft},
                   {"role": "user", "content": _REVISE.format(issues=listed)}]


async def revise(client, msgs: list, draft: str, issues: list, max_tokens: int = 4096) -> Optional[str]:
    """One non-streaming rewrite on the generator's own client. None on failure."""
    try:
        r = await client.post("/v1/chat/completions", json={
            "messages": revision_messages(msgs, draft, issues),
            "temperature": 0.2, "max_tokens": max_tokens, "stream": False}, timeout=None)
        r.raise_for_status()
        from .lanes import message_text
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
    from . import lanes, output_guard
    from .sse import sse
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

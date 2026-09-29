import json
import re
from typing import Optional
from .constants import APPLY_TO, DEFAULTS, MAX_EVIDENCE_CHARS, MODES


def settings(user_id: Optional[int] = None) -> dict:
    from .. import cloud
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

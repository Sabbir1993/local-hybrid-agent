import re
from typing import Optional
from .config_store import enabled, guard_cfg
from .constants import DEFAULT_MESSAGE, _MAX_PATTERN_LEN
from .matching import _clean_list, _compile, _rule_applies_to, is_semantic
from .semantic import semantic_hit


def message_texts(messages) -> list:
    """Every piece of text in client-supplied OpenAI-style messages, whatever the
    role: a client can put a card number or cloud-restricted text in an
    'assistant' / 'system' / 'tool' message just as easily as in a 'user' one."""
    texts = []
    for m in messages or []:
        if not isinstance(m, dict):
            continue
        c = m.get("content")
        if isinstance(c, str):
            texts.append(c)
        elif isinstance(c, list):
            texts.extend(str(p.get("text", "")) for p in c if isinstance(p, dict) and "text" in p)
        for tc in m.get("tool_calls") or []:
            fn = tc.get("function") if isinstance(tc, dict) else None
            if isinstance(fn, dict) and fn.get("arguments"):
                texts.append(str(fn["arguments"]))
    return texts


def check(texts: list, user, any_cloud_lane: bool) -> Optional[dict]:
    """Evaluate the prompt texts against all enabled rules.

    texts: list of strings to scan (all user messages + attachment previews).
    any_cloud_lane: True when at least one lane of this request resolves to a
        cloud model (after mode resolution -- 'all-local' is never cloud).
    user: Principal (for role/user-targeted rules).

    Returns the matched rule dict (with .get("message") for the human-readable
    error) or None when allowed.
    """
    if not texts:
        return None
    # Built-in PCI rule: runs even when the admin rule set is disabled.
    from .. import pan
    if pan.enabled("pan_input") and any(pan.contains_pan(t) for t in texts):
        return {"name": pan.RULE_NAME, "scope": "block_all",
                "message": pan.BLOCK_MESSAGE, "_matched_pattern": "builtin:pan"}
    # Built-in credential floor. Same deal, and it exists because the shipped rule set had
    # zero regex patterns, which left a single fail-open 4-token LLM call as the entire input
    # policy. Runs before `enabled()` on purpose: this is not an admin-tunable rule set.
    from .. import secrets
    for t in texts:
        hit = secrets.scan(t, any_cloud_lane=any_cloud_lane)
        if hit:
            scope = "cloud_only" if hit["mode"] == "cloud_only" else "block_all"
            return {"name": f"{secrets.RULE_NAME}: {hit['label']}", "scope": scope,
                    "message": secrets.BLOCK_MESSAGE.format(label=hit["label"]),
                    "_matched_pattern": f"builtin:secret:{hit['tier']}"}
    if not enabled():
        return None
    for rule in guard_cfg().get("rules") or []:
        if not isinstance(rule, dict) or not rule.get("enabled", True):
            continue
        if is_semantic(rule):
            continue        # semantic rules need the async path (check_async)
        scope = str(rule.get("scope") or "block_all")
        if scope == "cloud_only" and not any_cloud_lane:
            continue            # point #1: local models are allowed
        if not _rule_applies_to(rule, user):
            continue
        patterns = rule.get("patterns") or []
        for pat in patterns[:64]:
            if not isinstance(pat, str) or not pat.strip():
                continue
            if len(pat) > _MAX_PATTERN_LEN:
                continue
            rx = _compile(pat)
            if rx is None:
                continue        # invalid regex: fail open, skip rule pattern
            for text in texts:
                if text and rx.search(str(text)):
                    hit = dict(rule)
                    hit.setdefault("message", DEFAULT_MESSAGE.format(
                        rule_name=rule.get("name") or scope))
                    hit["_matched_pattern"] = pat
                    return hit
    return None


async def check_async(texts: list, user, any_cloud_lane: bool) -> Optional[dict]:
    """Async variant of check(): regex rules first (sync, cheap), then any
    semantic (natural-language) rules via the local classifier."""
    hit = check(texts, user, any_cloud_lane)
    if hit:
        return hit
    if not enabled() or not texts:
        return None
    for rule in guard_cfg().get("rules") or []:
        if not isinstance(rule, dict) or not rule.get("enabled", True):
            continue
        if not is_semantic(rule):
            continue
        scope = str(rule.get("scope") or "block_all")
        if scope == "cloud_only" and not any_cloud_lane:
            continue
        if not _rule_applies_to(rule, user):
            continue
        hit = await semantic_hit(texts, rule)
        if hit:
            return hit
    return None


def validate_rules(rules: list) -> tuple[list, list]:
    """Admin-side validation for the PUT endpoint.

    Returns (clean_rules, problems): clean rules have defaults applied and
    invalid entries dropped with a human-readable reason in problems.
    """
    clean, problems = [], []
    for i, raw in enumerate(rules or []):
        if not isinstance(raw, dict):
            problems.append(f"rule #{i + 1}: not an object, skipped")
            continue
        name = str(raw.get("name") or "").strip() or f"rule-{i + 1}"
        scope = str(raw.get("scope") or "block_all").strip().lower()
        if scope not in ("cloud_only", "block_all"):
            problems.append(f"'{name}': unknown scope '{scope}' (use cloud_only or block_all)")
            continue
        rtype = str(raw.get("type") or "regex").strip().lower()
        if rtype not in ("regex", "semantic"):
            problems.append(f"'{name}': unknown type '{rtype}' (use regex or semantic)")
            continue
        if rtype == "semantic":
            desc = str(raw.get("description") or "").strip()
            if not desc:
                problems.append(f"'{name}': semantic rule needs a policy description, skipped")
                continue
            clean.append({
                "id": str(raw.get("id") or "").strip() or f"rule-{len(clean) + 1}",
                "name": name,
                "type": "semantic",
                "description": desc[:2000],
                "scope": scope,
                "roles": _clean_list(raw.get("roles")),
                "users": _clean_list(raw.get("users")),
                "message": str(raw.get("message") or "").strip(),
                "enabled": bool(raw.get("enabled", True)),
            })
            continue
        pats = []
        for p in (raw.get("patterns") or []):
            p = str(p).strip()
            if not p:
                continue
            if len(p) > _MAX_PATTERN_LEN:
                problems.append(f"'{name}': pattern longer than {_MAX_PATTERN_LEN} chars dropped")
                continue
            try:
                re.compile(p, re.IGNORECASE)
            except re.error as e:
                problems.append(f"'{name}': invalid regex '{p[:60]}' ({e})")
                continue
            pats.append(p)
        if not pats:
            problems.append(f"'{name}': no valid patterns, skipped")
            continue
        clean.append({
            "id": str(raw.get("id") or "").strip() or f"rule-{len(clean) + 1}",
            "name": name,
            "type": "regex",
            "patterns": pats[:64],
            "scope": scope,
            "roles": _clean_list(raw.get("roles")),
            "users": _clean_list(raw.get("users")),
            "message": str(raw.get("message") or "").strip(),
            "replacement": str(raw.get("replacement") or "").strip(),
            "enabled": bool(raw.get("enabled", True)),
        })
    return clean, problems

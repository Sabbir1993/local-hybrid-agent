"""
core/input_guard.py - Configurable input sanitizer (three enforcement layers):

  1. cloud_only rules: admin-defined data patterns that are blocked when the
     request would be served by a CLOUD model (local models are allowed).
  2. block_all rules: globally prohibited prompt types, regardless of model.
  3. Role/user-bound rules: any rule (cloud_only or block_all) can be
     restricted to specific roles and/or usernames.

Rules live in config/app.json under "input_guard" (hot-reloadable via
APP_CONFIG, same pattern as capabilities.shell.allow_patterns) and are managed
by admins through routes/input_guard.py + the settings UI card.
"""

import re
import sys
import time
from typing import Optional

# Regex size cap: guards against pathological admin input at config time.
_MAX_PATTERN_LEN = 500

# Compiled-pattern cache: (pattern, flags) -> compiled regex | None (invalid).
_compile_cache: dict[tuple, Optional[re.Pattern]] = {}
_cache_ts: float = 0.0
_CACHE_TTL_S = 30.0   # re-read APP_CONFIG at most this often

DEFAULT_MESSAGE = (
    "Your prompt was blocked by an administrator-defined input policy "
    "({rule_name})."
)

SEMANTIC_TIMEOUT_S = 20   # local classifier budget per rule
_MAX_SEM_TEXT = 8000      # chars of scanned text sent to the classifier
_NULL_WORDS = {"", "null", "all", "none", "everyone"}


def guard_cfg() -> dict:
    """Live input_guard config block from the hot-reloaded APP_CONFIG."""
    from .small_model import APP_CONFIG
    cfg = APP_CONFIG.get("input_guard")
    if not isinstance(cfg, dict):
        from .config import CONFIG_FILE
        try:
            if CONFIG_FILE.exists():
                import json
                file_cfg = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
                if isinstance(file_cfg.get("input_guard"), dict):
                    cfg = file_cfg["input_guard"]
                    APP_CONFIG["input_guard"] = cfg
        except Exception:
            pass
    return cfg if isinstance(cfg, dict) else {}


def rules_for(cfg_key: str) -> list:
    """Enabled rules for a guard config block ("input_guard"/"output_guard").
    Shared by both the input and output sanitizers."""
    from .small_model import APP_CONFIG
    cfg = APP_CONFIG.get(cfg_key)
    if not isinstance(cfg, dict):
        from .config import CONFIG_FILE
        try:
            if CONFIG_FILE.exists():
                import json
                file_cfg = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
                if isinstance(file_cfg.get(cfg_key), dict):
                    cfg = file_cfg[cfg_key]
                    APP_CONFIG[cfg_key] = cfg
        except Exception:
            pass
    if not isinstance(cfg, dict) or not cfg.get("enabled", False):
        return []
    return [r for r in (cfg.get("rules") or [])
            if isinstance(r, dict) and r.get("enabled", True)]


def enabled() -> bool:
    return bool(guard_cfg().get("enabled", False))


def _compile(pattern: str) -> Optional[re.Pattern]:
    """Cached regex compile; invalid patterns return None (rule skipped)."""
    global _cache_ts, _compile_cache
    now = time.time()
    if now - _cache_ts > _CACHE_TTL_S:
        _compile_cache.clear()      # pick up config edits
        _cache_ts = now
    key = (pattern, re.IGNORECASE)
    if key not in _compile_cache:
        try:
            _compile_cache[key] = re.compile(pattern, re.IGNORECASE)
        except re.error:
            _compile_cache[key] = None
    return _compile_cache[key]


def _clean_list(values) -> list:
    """Null-tolerant list coercion: 'null'/'all'/'none'/'' mean NO restriction
    (the admin left the filter unset) and are dropped."""
    return [str(v).strip() for v in (values or [])
            if str(v).strip() and str(v).strip().lower() not in _NULL_WORDS]


def _rule_applies_to(rule: dict, user) -> bool:
    """Point #3: role/user targeting. Empty lists apply to everyone."""
    roles = _clean_list(rule.get("roles"))
    users = _clean_list(rule.get("users"))
    if not roles and not users:
        return True
    if users:
        uname = getattr(user, "username", None)
        if uname and str(uname) in {str(u) for u in users}:
            return True
    if roles:
        user_roles = {str(r) for r in (getattr(user, "role_names", None) or [])}
        if user_roles and user_roles & {str(r) for r in roles}:
            return True
    return False


def is_semantic(rule: dict) -> bool:
    return str(rule.get("type") or "regex").strip().lower() == "semantic"


async def _classify(policy: str, text: str) -> bool:
    """Ask the LOCAL executor model (never cloud) whether `text` violates the
    natural-language policy. Module-level so tests can monkeypatch it."""
    import asyncio
    from .small_model import small_models
    inst = small_models.instances.get("executor")
    if not inst or not inst.available:
        return False                      # fail open: no local model, no block
    payload = {
        "messages": [
            {"role": "system",
             "content": ("You are a strict compliance filter. Answer with "
                         "exactly one word: YES or NO. No explanation.")},
            {"role": "user",
             "content": (f"Policy: {policy}\n\n"
                         "Does the following content violate this policy?\n\n"
                         f"CONTENT:\n{text[:_MAX_SEM_TEXT]}\n\n"
                         "Answer YES or NO only.")},
        ],
        "temperature": 0.0,
        "max_tokens": 4,
        "stream": False,
    }
    try:
        await inst.ensure_loaded()
        r = await asyncio.wait_for(
            inst.client.post("/v1/chat/completions", json=payload),
            timeout=SEMANTIC_TIMEOUT_S)
        r.raise_for_status()
        data = r.json()
        ans = str(data["choices"][0]["message"]["content"] or "").strip().upper()
        return ans.startswith("YES")
    except Exception as e:
        print(f"[input_guard] semantic classifier unavailable (fail-open): {e}",
              file=sys.stderr)
        return False


async def semantic_hit(texts: list, rule: dict) -> Optional[dict]:
    """Evaluate one semantic rule against the texts. Returns the hit dict or
    None. Fail-open: any classifier problem means the rule does not fire."""
    policy = str(rule.get("description") or "").strip()
    if not policy:
        return None
    joined = "\n---\n".join(str(t) for t in texts if t)[:_MAX_SEM_TEXT * 2]
    if not joined.strip():
        return None
    if await _classify(policy, joined):
        hit = dict(rule)
        hit.setdefault("message", DEFAULT_MESSAGE.format(
            rule_name=rule.get("name") or "policy"))
        return hit
    return None

def check(texts: list, user, any_cloud_lane: bool) -> Optional[dict]:
    """Evaluate the prompt texts against all enabled rules.

    texts: list of strings to scan (all user messages + attachment previews).
    any_cloud_lane: True when at least one lane of this request resolves to a
        cloud model (after mode resolution -- 'all-local' is never cloud).
    user: Principal (for role/user-targeted rules).

    Returns the matched rule dict (with .get("message") for the human-readable
    error) or None when allowed.
    """
    if not enabled() or not texts:
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

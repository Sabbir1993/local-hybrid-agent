import asyncio
import sys
from typing import Optional
from .constants import DEFAULT_MESSAGE, SEMANTIC_TIMEOUT_S, _MAX_SEM_TEXT


async def _classify(policy: str, text: str) -> bool:
    """Ask a LOCAL model (never cloud) whether `text` violates the natural-language
    policy -- the "Policy checks" job (core/lanes.py; the executor by default).
    Module-level so tests can monkeypatch it."""
    from .. import lanes, reasoning
    from ..lanes import routing
    route = lanes.targets("input_guard")      # local_only job: no cloud targets
    t = route[0] if route else None
    if t is None or not t.available():
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
        # A reasoning model spends a 4-token budget on its think block and returns an empty
        # answer, which read as "NO" and let every answer through. Thinking is switched off for
        # this call (the classifier is local only, so these llama-server fields are always valid).
        "max_tokens": 8,
        "stream": False,
        **reasoning.local_fields("none"),
    }
    rid, t0 = routing._account_begin("input_guard", t)
    try:
        client = await t.client()
        r = await asyncio.wait_for(
            client.post("/v1/chat/completions", json=payload),
            timeout=SEMANTIC_TIMEOUT_S)
        r.raise_for_status()
        data = r.json()
        routing._account_end("input_guard", t, rid, t0, 200, data)
        msg = data["choices"][0]["message"]
        ans = str(msg.get("content") or "").strip().upper()
        if not ans:
            # nothing usable came back: say so, because "no answer" is not "no violation"
            print("[input_guard] semantic classifier returned an empty answer (fail-open)", file=sys.stderr)
        return ans.startswith("YES")
    except Exception as e:
        routing._account_end("input_guard", t, rid, t0, 500, None)
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
    classify_fn = getattr(sys.modules.get("core.input_guard"), "_classify", _classify)
    if await classify_fn(policy, joined):
        hit = dict(rule)
        hit.setdefault("message", DEFAULT_MESSAGE.format(
            rule_name=rule.get("name") or "policy"))
        return hit
    return None

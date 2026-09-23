"""
core/output_guard.py - Configurable OUTPUT sanitizer: redacts model responses
before they reach the user / chat history. Same three rule layers as the input
sanitizer (core/input_guard.py):

  1. cloud_only rules: redact only when the response came from a CLOUD model.
  2. block_all rules:  always redact, regardless of model.
  3. roles/users:      restrict any rule to specific roles or usernames.

Rules live in config/app.json under "output_guard" (same schema as
"input_guard", plus an optional per-rule "replacement" string). Because tokens
arrive as deltas, OutputRedactor holds back a rolling tail (longest pattern
length, capped) so patterns spanning token boundaries are caught before they
are ever sent to the client.
"""

import re
from typing import Optional

from . import input_guard

DEFAULT_REPLACEMENT = "█████"
_MAX_HOLD = 400          # never hold back more than this many chars
_MAX_PENDING = 50_000    # safety valve for pathologically long pending matches
_TRAILING_NUM_RX = re.compile(r"[\d -]{1,40}$")


def guard_cfg() -> dict:
    from .small_model import APP_CONFIG
    cfg = APP_CONFIG.get("output_guard")
    if not isinstance(cfg, dict):
        from .config import CONFIG_FILE
        try:
            if CONFIG_FILE.exists():
                import json
                file_cfg = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
                if isinstance(file_cfg.get("output_guard"), dict):
                    cfg = file_cfg["output_guard"]
                    APP_CONFIG["output_guard"] = cfg
        except Exception:
            pass
    return cfg if isinstance(cfg, dict) else {}


def _replacement(rule: dict) -> str:
    r = str(rule.get("replacement") or "").strip()
    return r or DEFAULT_REPLACEMENT


def _active_rules(user, any_cloud_lane: bool) -> list:
    """[(rule_dict, [(compiled_rx, replacement), ...]), ...] for this request."""
    out = []
    for rule in input_guard.rules_for("output_guard"):
        scope = str(rule.get("scope") or "block_all")
        if scope == "cloud_only" and not any_cloud_lane:
            continue            # response came from a local model: allowed
        if not input_guard._rule_applies_to(rule, user):
            continue
        pats = []
        for p in (rule.get("patterns") or [])[:64]:
            if isinstance(p, str) and p.strip() and len(p) <= input_guard._MAX_PATTERN_LEN:
                rx = input_guard._compile(p)
                if rx is not None:
                    pats.append((rx, _replacement(rule)))
        if pats:
            out.append({"name": rule.get("name") or scope, "scope": scope,
                        "message": rule.get("message") or "", "patterns": pats})
    from . import pan
    if pan.enabled("pan_output"):
        # Built-in PCI rule, independent of admin rules; last so an admin rule
        # with its own replacement wins. "builtin" marks it for the streaming
        # holdback (only a trailing run of digits needs holding).
        out.append({"name": pan.RULE_NAME, "scope": "block_all", "message": "",
                    "builtin": "pan", "patterns": [(pan.PAN_RX, pan.mask_match)]})
    return out


def redact_full(text: str, user, any_cloud_lane: bool) -> tuple[str, Optional[dict]]:
    """One-shot redaction of fully buffered text (summaries, final answers).

    Returns (redacted_text, first_matched_rule_or_None)."""
    if not text:
        return text, None
    matched = None
    for rule in _active_rules(user, any_cloud_lane):
        for rx, repl in rule["patterns"]:
            new = rx.sub(repl, text)
            if new != text:
                if matched is None:
                    matched = rule
                text = new
    return text, matched


async def semantic_check(text: str, user, any_cloud_lane: bool) -> Optional[dict]:
    """Evaluate semantic (natural-language) rules against a completed response.
    Called at turn end / finalization, since semantic rules cannot redact
    mid-stream. Returns the hit dict or None (fail-open)."""
    if not text or not str(text).strip():
        return None
    for rule in input_guard.rules_for("output_guard"):
        if not input_guard.is_semantic(rule):
            continue
        scope = str(rule.get("scope") or "block_all")
        if scope == "cloud_only" and not any_cloud_lane:
            continue            # response came from a local model: allowed
        if not input_guard._rule_applies_to(rule, user):
            continue
        hit = await input_guard.semantic_hit([text], rule)
        if hit:
            return hit
    return None


class OutputRedactor:
    """Streaming redaction filter: feed() deltas, flush() at end of stream.

    Correctness rule: a chunk is only emitted once no active pattern can still
    match into it. _safe_upto() scans the pending buffer for matches that end
    inside the holdback zone (they may still grow as more tokens arrive) and
    holds everything from their start, so a match spanning token boundaries is
    redacted in full before any of its characters are ever sent to the client.
    When no rules are active, feed() is a zero-overhead pass-through."""

    def __init__(self, user, any_cloud_lane: bool):
        self.rules = _active_rules(user, any_cloud_lane)
        admin = [r for r in self.rules if not r.get("builtin")]
        self.pan = any(r.get("builtin") == "pan" for r in self.rules)
        if admin:
            longest = max(len(rx.pattern) for r in admin for rx, _ in r["patterns"])
            # matches can be longer than the pattern text (\d{13,19} matches 19
            # chars); 3x + 16 covers bounded quantifiers and common literals
            self.hold = min(longest * 3 + 16, _MAX_HOLD)
        else:
            self.hold = 0
        self.buf = ""
        self.matched: Optional[dict] = None   # first matching rule (name/message)
        self.hits = 0

    def feed(self, text: str) -> str:
        """Consume a delta; return the safe-to-display prefix (may be '')."""
        if not self.rules or not text:
            return text
        self.buf += text
        return self._drain()

    def flush(self) -> str:
        """End of stream: release everything still held (redacted)."""
        if not self.buf:
            return ""
        out = self._redact(self.buf)
        self.buf = ""
        return out

    def reset(self) -> None:
        """delta_reset: the UI discards streamed text, so drop any pending
        holdback too (it was never displayed). Match/hit bookkeeping stays."""
        self.buf = ""

    def _safe_upto(self) -> int:
        """How many leading chars of buf are certainly settled."""
        hold = self.hold
        if self.pan:
            # a PAN can only still be growing inside the trailing digit run
            m = _TRAILING_NUM_RX.search(self.buf)
            hold = max(hold, len(m.group(0)) if m else 0)
        limit = len(self.buf) - hold
        if limit <= 0:
            return 0
        if limit > _MAX_PENDING:      # safety valve: never buffer unbounded
            return len(self.buf) - self.hold
        cut = limit
        for rule in self.rules:
            for rx, _ in rule["patterns"]:
                for m in rx.finditer(self.buf):
                    if m.end() > limit:
                        # match reaches into the hold zone: it may still grow,
                        # so nothing from its start on may be emitted yet
                        cut = min(cut, m.start())
                        break     # finditer is ordered; later matches start later
        return cut

    def _drain(self) -> str:
        cut = self._safe_upto()
        if cut <= 0:
            return ""
        emit, self.buf = self.buf[:cut], self.buf[cut:]
        return self._redact(emit)

    def _redact(self, s: str) -> str:
        for rule in self.rules:
            for rx, repl in rule["patterns"]:
                new = rx.sub(repl, s)
                if new != s:
                    self.hits += 1
                    if self.matched is None:
                        self.matched = rule
                    s = new
        return s

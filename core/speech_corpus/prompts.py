"""The sentences people read: import from .jsonl and the per-session pick.

A session gets a fixed mix of categories. Within a category the lines with the fewest accepted recordings come first
(ties broken at random), so the whole pool gets covered; a speaker never gets a line from any of their earlier sessions.
"""
import json
import random
import re
import time

from .store import CorpusError, CorpusStore

CATEGORIES = ("standard", "domain", "banglish", "numbers")
# share of a session per category (scaled to lines_per_session)
MIX = {"standard": 5, "domain": 3, "banglish": 4, "numbers": 3}
MIN_LEN, MAX_LEN = 3, 220
MAX_IMPORT_LINES = 20000


def _mix(n: int) -> dict:
    total = sum(MIX.values())
    out = {c: (n * w) // total for c, w in MIX.items()}
    for c in sorted(MIX, key=lambda c: -MIX[c]):      # hand out the remainder, biggest share first
        if sum(out.values()) >= n:
            break
        out[c] += 1
    return out


def pick(store: CorpusStore, contributor_id: int, n: int, rng: random.Random = None) -> list:
    """-> list of {id, text, category}, shuffled, at most n. Raises CorpusError when nothing is left for this speaker."""
    rng = rng or random.Random()
    seen = store.seen_prompt_ids(contributor_id)
    counts = {r["id"]: (r["category"], r["text"], r["n"]) for r in store.db.execute(
        "SELECT p.id, p.category, p.text, (SELECT COUNT(*) FROM recordings r WHERE r.prompt_id = p.id AND r.status = 'ok') AS n "
        "FROM prompts p WHERE p.active = 1")}
    pool = {c: [] for c in CATEGORIES}
    for pid, (cat, text, k) in counts.items():
        if pid not in seen and cat in pool:
            pool[cat].append((k, rng.random(), pid))
    for c in pool:
        pool[c].sort()
    chosen = []
    want = _mix(n)
    for c in CATEGORIES:
        take, pool[c] = pool[c][:want[c]], pool[c][want[c]:]
        chosen += take
    if len(chosen) < n:                                   # a category ran dry: fill from the rest, least recorded first
        rest = sorted(x for c in CATEGORIES for x in pool[c])
        chosen += rest[: n - len(chosen)]
    if not chosen:
        raise CorpusError("Thank you! You have already recorded every line we have. Please check back later.", 409)
    rng.shuffle(chosen)
    return [{"id": pid, "text": counts[pid][1], "category": counts[pid][0]} for _k, _r, pid in chosen]


def import_jsonl(store: CorpusStore, text: str) -> dict:
    """Each line: {"text": "...", "category": "standard|domain|banglish|numbers"}. Lines that would carry a card-like
    number are refused (the corpus must never teach or store one). -> {added, duplicates, rejected: [{line, reason}]}"""
    from ..pan import contains_pan
    added = dup = 0
    rejected = []
    lines = str(text or "").splitlines()
    if len(lines) > MAX_IMPORT_LINES:
        raise CorpusError(f"At most {MAX_IMPORT_LINES} lines per import.")
    now = time.time()
    with store._lock:
        for i, raw in enumerate(lines, 1):
            raw = raw.strip()
            if not raw:
                continue
            try:
                obj = json.loads(raw)
                t = re.sub(r"\s+", " ", str(obj.get("text") or "")).strip()
                cat = str(obj.get("category") or "").strip().lower()
            except (ValueError, AttributeError):
                rejected.append({"line": i, "reason": "not a JSON object"})
                continue
            if cat not in CATEGORIES:
                rejected.append({"line": i, "reason": f"category must be one of {', '.join(CATEGORIES)}"})
                continue
            if not (MIN_LEN <= len(t) <= MAX_LEN):
                rejected.append({"line": i, "reason": f"text must be {MIN_LEN}-{MAX_LEN} characters"})
                continue
            if contains_pan(t) or re.search(r"\d[\d \-]{11,}\d", t):
                rejected.append({"line": i, "reason": "contains a card-like or long account-like number"})
                continue
            cur = store.db.execute("INSERT OR IGNORE INTO prompts (text, category, created_at) VALUES (?,?,?)", (t, cat, now))
            if cur.rowcount:
                added += 1
            else:
                dup += 1
        store.db.commit()
    return {"added": added, "duplicates": dup, "rejected": rejected[:200], "rejected_count": len(rejected)}

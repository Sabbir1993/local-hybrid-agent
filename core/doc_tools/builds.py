"""Durable bookkeeping for decks / PDFs built in pieces (doc_create total=N, then doc_append).

The slide count the user asked for and the model's own slide plan used to live only in memory, so a restart lost both:
the checks went off and "continue" had nothing to resume from. They are kept here (projects.db), keyed by the same
(user id, file key) the in-memory caches use. Only titles, counts and file names are stored -- no slide text; the
deck itself already lives in doc_store.source_spec.

A build is "open" until its last doc_append (done=true) or until it is a day old; opening a new one for the same
user does not delete the old one, it is simply no longer the latest.
"""

import json
import re
import sys
import time
from typing import Optional

from ..db import _projects_db

OPEN_TTL_S = 24 * 3600
CONTINUE_INTENT = re.compile(
    r"^\s*(?:ok(?:ay)?[,. ]*)?(?:please\s+)?(?:continue|carry on|go on|keep going|resume|proceed|finish (?:it|the (?:deck|pdf|presentation|slides?|document))|"
    r"complete (?:it|the (?:deck|pdf|presentation|slides?|document)))"
    r"(?:[\s,]+(?:it|now|please|then|the|deck|pdf|slides?|presentation|document|again|thanks?|thank you))*[\s.!]*$", re.I)


def _init() -> None:
    _projects_db.execute("""CREATE TABLE IF NOT EXISTS deck_builds (
        user_id INTEGER NOT NULL,
        file_key TEXT NOT NULL,
        total INTEGER,
        plan_json TEXT,
        have INTEGER DEFAULT 0,
        status TEXT NOT NULL DEFAULT 'open',
        created_at REAL NOT NULL,
        updated_at REAL NOT NULL,
        PRIMARY KEY (user_id, file_key)
    )""")
    _projects_db.commit()


try:
    _init()
except Exception as e:
    print(f"[deck_builds] init failed: {e}", file=sys.stderr)


def save(uid: int, file_key: str, *, total: Optional[int] = None, plan: Optional[list] = None,
         have: Optional[int] = None) -> None:
    """Create or update the open build for this file. Fields left None keep their stored value."""
    try:
        now = time.time()
        _projects_db.execute(
            "INSERT INTO deck_builds (user_id, file_key, total, plan_json, have, status, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, COALESCE(?, 0), 'open', ?, ?) "
            "ON CONFLICT(user_id, file_key) DO UPDATE SET total = COALESCE(excluded.total, total), "
            "plan_json = COALESCE(excluded.plan_json, plan_json), have = COALESCE(?, have), "
            "status = 'open', updated_at = excluded.updated_at",
            (uid, file_key, total, json.dumps(plan) if plan else None, have, now, now, have))
        _projects_db.commit()
    except Exception as e:
        print(f"[deck_builds] save failed: {e}", file=sys.stderr)


def load(uid: int, file_key: str) -> Optional[dict]:
    """The open, not-expired build for this file: {total, plan: [(title, purpose)], have, updated_at}."""
    try:
        r = _projects_db.execute(
            "SELECT total, plan_json, have, updated_at FROM deck_builds "
            "WHERE user_id = ? AND file_key = ? AND status = 'open' AND updated_at > ?",
            (uid, file_key, time.time() - OPEN_TTL_S)).fetchone()
    except Exception as e:
        print(f"[deck_builds] load failed: {e}", file=sys.stderr)
        return None
    if not r:
        return None
    plan = []
    try:
        plan = [tuple(x) for x in json.loads(r["plan_json"] or "[]")]
    except (ValueError, TypeError):
        pass
    return {"total": r["total"], "plan": plan, "have": r["have"] or 0, "updated_at": r["updated_at"]}


def close(uid: int, file_key: str, status: str = "done") -> None:
    try:
        _projects_db.execute("UPDATE deck_builds SET status = ?, updated_at = ? WHERE user_id = ? AND file_key = ?",
                             (status, time.time(), uid, file_key))
        _projects_db.commit()
    except Exception as e:
        print(f"[deck_builds] close failed: {e}", file=sys.stderr)


def latest_open(uid: int) -> Optional[dict]:
    try:
        r = _projects_db.execute(
            "SELECT file_key FROM deck_builds WHERE user_id = ? AND status = 'open' AND updated_at > ? "
            "ORDER BY updated_at DESC LIMIT 1", (uid, time.time() - OPEN_TTL_S)).fetchone()
    except Exception as e:
        print(f"[deck_builds] latest failed: {e}", file=sys.stderr)
        return None
    return dict(load(uid, r["file_key"]) or {}, file=r["file_key"]) if r else None


def is_continue(text: str) -> bool:
    return bool(CONTINUE_INTENT.match(str(text or "").strip()[:120]))


def resume_note(uid: int, text: str) -> str:
    """System note for a bare "continue" when the user has a deck / PDF build left unfinished, else ''."""
    if not is_continue(text):
        return ""
    b = latest_open(uid)
    if not b or not b.get("total") or b["have"] >= b["total"]:
        return ""
    plan = b.get("plan") or []
    left = plan[b["have"]:b["have"] + 4]
    steps = ("; ".join(f"{b['have'] + i + 1}. {t}" for i, (t, _) in enumerate(left)) + ". ") if left else ""
    return (f"[system note] The user's build of {b['file']} was interrupted: it has {b['have']} of {b['total']} "
            f"slides/sections. Continue it now with doc_append(file='{b['file']}', content=<next 2-3 slides/sections>) "
            f"from number {b['have'] + 1}. {steps}Keep the same theme and style. Do NOT restart, ask questions again "
            f"or search the web again; finish with done=true after the last one.")

"""Name a chat after its topic instead of its first words.

A new chat is titled from the first message right away (frontend generateTopicTitle), so a chat that opens with "Hi"
was called "Hi" for good. After each saved reply the client asks here once more while the title is still automatic
(sessions.title_auto = 1): a local model reads the first turns and answers with a short topic title, or NONE while the
chat is still greetings and small talk. The first real title ends it (title_auto = 0), as does a rename by the user,
or MAX_USER_TURNS without a topic.

Privacy: the "session_title" job is local_only, so chat text never leaves this PC for a title, and it only uses a model
that is already running (no model is loaded just to name a chat). The title is PAN-masked at rest like every title.
"""

import asyncio
import re
import sys
from typing import Optional

from . import lanes
from .db import db_load_messages, db_session_title_state, db_set_auto_title

MAX_USER_TURNS = 4        # give up after this many questions without a topic: the placeholder stays
USER_CHARS = 500          # per user message shown to the model
REPLY_CHARS = 300         # per reply: enough to tell the topic, short enough to stay cheap
MAX_TITLE_CHARS = 60
TIMEOUT_S = 60

_in_flight: set = set()

# whole messages that carry no topic: greetings, thanks, acknowledgements (English and Bangla, romanised or not)
_SMALL_TALK = re.compile(
    r"^\s*(hi+|hello+|hey+|hiya|yo|good\s+(morning|afternoon|evening|night)|salam|assalamu?\s*alaikum|"
    r"as-?salamu\s+alaykum|namaskar|kemon\s+acho|how\s+are\s+you|what'?s\s+up|sup|thanks?|thank\s+you|"
    r"thx|ok(ay)?|cool|nice|great|test(ing)?|হাই|হ্যালো|আসসালামু\s*আলাইকুম|ধন্যবাদ|কেমন\s+আছ(েন|ো)?)"
    r"[\s!.?,~😀-🙏]*$",
    re.I,
)

_SYSTEM = (
    "You name chat conversations for a sidebar. Reply with only a title of 3 to 6 words that names the topic the "
    "user is asking about. Use the language the user writes in. No quotes, no ending punctuation, no emoji, no "
    "'Title:' prefix. If the user has not asked about anything yet (only greetings or small talk), reply exactly: NONE"
)


def _is_small_talk(text: str) -> bool:
    return bool(_SMALL_TALK.match(text or ""))


def _strip_think(text: str) -> str:
    text = re.sub(r"<think>[\s\S]*?</think>", "", text or "")
    return re.sub(r"^[\s\S]*?</think>", "", text)        # an unclosed block that only shows its end


def clean_title(raw: str) -> Optional[str]:
    """The model's answer as a sidebar title, or None when it gave no usable one (NONE, empty, a sentence)."""
    lines = [l.strip() for l in _strip_think(raw).splitlines() if l.strip()]
    if not lines:
        return None
    t = lines[0]
    t = t.replace("**", "").replace("__", "").lstrip("#>- ").strip()
    t = re.sub(r"^(chat\s+)?title\s*[:\-]\s*", "", t, flags=re.I)
    t = t.strip("\"'`“”‘’«»「」 ")
    t = re.sub(r"[.!?。,;:]+$", "", t).strip()
    t = re.sub(r"\s+", " ", t)
    if not t or t.upper().startswith("NONE") or len(t) < 2:
        return None
    if len(t) > MAX_TITLE_CHARS:
        cut = t[:MAX_TITLE_CHARS].rsplit(" ", 1)[0]
        t = (cut or t[:MAX_TITLE_CHARS]).rstrip(" ,;:-") + "…"
    return t


def _transcript(messages: list) -> tuple:
    """(text for the model, user turns read, whether a topic was found). The title names the chat's FIRST topic: the
    model sees only the first question that is not small talk and the reply to it. Later turns are left out, so a chat
    that drifts to another subject keeps the name of what it was opened for (a 9B model given both picks the later)."""
    parts, turns, topical = [], 0, False
    for m in messages:
        role, content = m.get("role"), m.get("content")
        if not isinstance(content, str) or not content.strip():
            continue
        if role == "user":
            if topical:
                break                          # first topic and its reply are in: stop here
            turns += 1
            if turns > MAX_USER_TURNS:
                break
            if _is_small_talk(content):
                continue
            topical = True
            parts.append("User: " + content.strip()[:USER_CHARS])
        elif role == "assistant" and topical:
            parts.append("Assistant: " + _strip_think(content).strip()[:REPLY_CHARS])
    return "\n\n".join(parts), turns, topical


async def _ask_model(transcript: str, user_id: int, model_url: Optional[str] = None) -> Optional[str]:
    payload = {
        "messages": [{"role": "system", "content": _SYSTEM},
                     {"role": "user", "content": "Conversation:\n\n" + transcript + "\n\nTitle:"}],
        # room for a model that thinks anyway; thinking is turned off where the template allows it
        "max_tokens": 512,
        "temperature": 0.2,
        "stream": False,
        "chat_template_kwargs": {"enable_thinking": False},
        "thinking_budget_tokens": 0,
    }
    if model_url:                          # the backfill command, outside the app: a local llama-server named by URL
        import httpx
        async with httpx.AsyncClient(base_url=_local_url(model_url), timeout=TIMEOUT_S) as c:
            r = await c.post("/v1/chat/completions", json=payload)
            r.raise_for_status()
            data = r.json()
    else:
        data, _t = await asyncio.wait_for(
            lanes.post_chat("session_title", payload, user_id, only_if_running=True, timeout=TIMEOUT_S), TIMEOUT_S + 5)
    return clean_title(lanes.message_text(data))


def _local_url(url: str) -> str:
    """Only a model on this machine may read chats for a title (the job is local_only for the same reason)."""
    from urllib.parse import urlparse
    u = urlparse(url)
    if u.scheme not in ("http", "https") or (u.hostname or "") not in ("127.0.0.1", "localhost", "::1"):
        raise ValueError(f"--model-url must be a local address (127.0.0.1 / localhost), got {url!r}")
    return url.rstrip("/")


async def suggest_title(sid: int, user_id: int, model_url: Optional[str] = None) -> dict:
    """Retitle the chat by topic when it is still automatic. {"title", "changed", "auto"}; raises PermissionError."""
    state = db_session_title_state(sid, user_id)
    out = {"title": state["title"], "changed": False, "auto": state["auto"]}
    if not state["auto"] or sid in _in_flight:
        return out
    _in_flight.add(sid)
    try:
        text, _read, topical = _transcript(db_load_messages(sid, user_id))
        turns = state["user_turns"]           # every question in the chat, not only those read for the title
        if not topical:
            if turns >= MAX_USER_TURNS:        # small talk all the way: keep the placeholder, stop asking
                db_set_auto_title(sid, user_id, None)
                out["auto"] = False
            return out
        try:
            title = await _ask_model(text, user_id, model_url)
        except Exception as e:                 # no local model up, timeout: the next reply tries again
            print(f"[session_title] chat {sid}: no title this turn ({type(e).__name__}: {e})", file=sys.stderr)
            out["error"] = f"{type(e).__name__}: {e}"
            return out
        if title and db_set_auto_title(sid, user_id, title):
            out.update(title=db_session_title_state(sid, user_id)["title"], changed=True, auto=False)
        elif not title and turns >= MAX_USER_TURNS:     # the model read it and found no topic: stop asking
            db_set_auto_title(sid, user_id, None)
            out["auto"] = False
        return out
    finally:
        _in_flight.discard(sid)


# ---------------------------------------------------------------------------------------------------------------
# One-time backfill for chats made before topic titles (python -m core.session_title --backfill [--apply])
# ---------------------------------------------------------------------------------------------------------------

_GENERIC_TITLES = {"new chat", "chat", "new session", "task"}


def placeholder_title(prompt: str) -> str:
    """Python twin of the frontend's generateTopicTitle (frontend/src/stores/useAppStore.ts): the title a chat got from
    its first message. Kept in step with it so the backfill can tell an automatic title from one a user typed."""
    if not prompt:
        return "New Chat"
    clean = prompt.strip()
    clean = re.sub(r"```[\s\S]*?```", "", clean)
    clean = re.sub(r"^[#>\-*\s]+", "", clean).strip()
    clean = re.sub(r"^(act as an?|act as|pretend you are an?|you are an?)\s+([a-zA-Z\s]+?)(?:[.,:;\n]+|\s+and\s+|\s+to\s+)",
                   "", clean, count=1, flags=re.I)
    clean = re.sub(r"^(please\s+)?(help\s+me\s+)?(write|create|draft|generate|provide|build|make|summarise|summarize|explain|"
                   r"show\s+me)\s+(a\s+|an\s+|the\s+)?", "", clean, count=1, flags=re.I)
    clean = re.sub(r"^(can\s+you\s+)(please\s+)?(help\s+me\s+)?(write|create|draft|generate|provide|build|make|summarise|"
                   r"summarize|explain)\s+(a\s+|an\s+|the\s+)?", "", clean, count=1, flags=re.I)
    clean = re.sub(r"^(here\s+is\s+(a\s+|an\s+|the\s+)?)", "", clean, count=1, flags=re.I)
    clean = re.sub(r"^(i\s+want\s+you\s+to\s+|i\s+need\s+(a\s+|an\s+|to\s+)?)", "", clean, count=1, flags=re.I)
    first = re.split(r"[.\n;!?]", clean)[0].strip()
    final = first or clean
    if not final:
        return "New Chat"
    formatted = final[0].upper() + final[1:]
    return formatted[:32].strip() + "…" if len(formatted) > 32 else formatted


def is_placeholder(title: str, first_user: str) -> bool:
    """True when the title is what the app wrote on its own: never one a user picked (that would be lost)."""
    t = (title or "").strip()
    if not t:
        return True
    if t.lower() in _GENERIC_TITLES:
        return True
    expected = placeholder_title(first_user or "")
    if t == expected:
        return True
    # titles saved by older builds: the same text, cut, or with a stored PAN mask / unprintable end
    if expected.endswith("…") and t.rstrip("…").strip() and expected.startswith(t.rstrip("…").strip()[:24]):
        return True
    return False


async def backfill(apply: bool = False, user_id: Optional[int] = None, log=print, model_url: Optional[str] = None) -> dict:
    """Retitle chats made before topic titles. Dry run unless apply=True. Skips chats with no question, custom agent
    chats, forks, and any title that does not match the automatic one (it may have been typed by its owner)."""
    from .db import _projects_db
    from .db.sessions import _has_title_auto_column
    if model_url:
        _local_url(model_url)              # refuse a remote address before any chat is read
    if not _has_title_auto_column():
        raise RuntimeError("sessions.title_auto is missing: start the app once so its migration adds it")
    has_fork = "fork_of" in [c[1] for c in _projects_db.execute("PRAGMA table_info(sessions)")]
    sql = ("SELECT s.id, s.user_id, s.title, s.agent_id, " + ("s.fork_of" if has_fork else "NULL") + " AS fork_of, "
           "(SELECT content FROM messages m WHERE m.session_id = s.id AND m.role = 'user' ORDER BY m.id LIMIT 1) AS first_user "
           "FROM sessions s WHERE s.user_id IS NOT NULL" + (" AND s.user_id = ?" if user_id is not None else "") + " ORDER BY s.id")
    rows = _projects_db.execute(sql, (user_id,) if user_id is not None else ()).fetchall()
    stats = {"checked": len(rows), "candidates": 0, "renamed": 0, "kept": 0, "failed": 0, "skipped": 0}
    for r in rows:
        if not r["first_user"] or r["agent_id"] is not None or r["fork_of"] is not None \
                or not is_placeholder(r["title"], r["first_user"]):
            stats["skipped"] += 1
            continue
        stats["candidates"] += 1
        if not apply:
            log(f"  would retitle chat {r['id']} (user {r['user_id']}): {r['title']!r}")
            continue
        _projects_db.execute("UPDATE sessions SET title_auto = 1 WHERE id = ?", (r["id"],))
        _projects_db.commit()
        res = await suggest_title(r["id"], r["user_id"], model_url)
        if res.get("error"):
            stats["failed"] += 1
            log(f"  chat {r['id']}: no model answered ({res['error']}); it retitles on its next reply")
            if stats["failed"] >= 3 and not stats["renamed"]:
                log("  stopping: the model is not answering (is it loaded? see --model-url)")
                break
        elif res["changed"]:
            stats["renamed"] += 1
            log(f"  chat {r['id']}: {r['title']!r} -> {res['title']!r}")
        else:
            stats["kept"] += 1
            # small talk only, or no model answered: keep the title; it stays automatic only if the chat may still
            # get a topic (suggest_title already stopped it when the chat is over its turn budget)
            log(f"  chat {r['id']}: kept {r['title']!r}" + ("" if res["auto"] else " (no topic)"))
    return stats


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser(description="Name chats made before topic titles by their topic.")
    ap.add_argument("--backfill", action="store_true", required=True)
    ap.add_argument("--apply", action="store_true", help="really retitle (default: only list what would change)")
    ap.add_argument("--user", type=int, default=None, help="only this user's chats")
    ap.add_argument("--model-url", default=None,
                    help="local llama-server to ask, e.g. http://127.0.0.1:8090 (needed outside the app, which only "
                         "sees the models it started itself)")
    a = ap.parse_args()
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    print("Retitling chats" if a.apply else "Dry run (add --apply to retitle)")
    print(asyncio.run(backfill(apply=a.apply, user_id=a.user, model_url=a.model_url)))

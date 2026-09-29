import json
import sqlite3
import time
from typing import Optional

from .common import db

_TOKEN_COLS = (
    "t.id, t.prefix, t.name, t.user_id, u.username, t.created_by, t.permissions, "
    "t.created_at, t.expires_at, t.last_used_at, t.last_used_ip, t.revoked_at"
)


def create_session_row(session_id_hash: str, user_id: int, expires_at: float,
                       ip: Optional[str], user_agent: Optional[str]) -> None:
    now = time.time()
    db().execute(
        "INSERT INTO auth_sessions (id, user_id, created_at, last_seen_at, expires_at, ip, user_agent) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (session_id_hash, user_id, now, now, expires_at, ip, user_agent),
    )
    db().commit()


def get_session_row(session_id_hash: str) -> Optional[sqlite3.Row]:
    return db().execute("SELECT * FROM auth_sessions WHERE id = ?", (session_id_hash,)).fetchone()


def touch_session(session_id_hash: str, expires_at: float) -> None:
    db().execute(
        "UPDATE auth_sessions SET last_seen_at = ?, expires_at = ? WHERE id = ?",
        (time.time(), expires_at, session_id_hash),
    )
    db().commit()


def revoke_session_row(session_id_hash: str) -> None:
    db().execute("UPDATE auth_sessions SET revoked_at = ? WHERE id = ?", (time.time(), session_id_hash))
    db().commit()


def revoke_all_sessions_for_user(user_id: int) -> None:
    db().execute("UPDATE auth_sessions SET revoked_at = ? WHERE user_id = ? AND revoked_at IS NULL",
                 (time.time(), user_id))
    db().commit()


def create_api_token_row(token_hash: str, prefix: str, name: str, user_id: int,
                         created_by: Optional[int], permissions: list, expires_at: float) -> int:
    cur = db().execute(
        "INSERT INTO api_tokens (token_hash, prefix, name, user_id, created_by, permissions, "
        "created_at, expires_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (token_hash, prefix, name, user_id, created_by, json.dumps(sorted(permissions)),
         time.time(), expires_at),
    )
    db().commit()
    return cur.lastrowid


def get_api_token_by_hash(token_hash: str) -> Optional[sqlite3.Row]:
    return db().execute("SELECT * FROM api_tokens WHERE token_hash = ?", (token_hash,)).fetchone()


def get_api_token(token_id: int) -> Optional[sqlite3.Row]:
    return db().execute("SELECT * FROM api_tokens WHERE id = ?", (token_id,)).fetchone()


def list_api_tokens(user_id: Optional[int] = None) -> list:
    sql = f"SELECT {_TOKEN_COLS} FROM api_tokens t LEFT JOIN users u ON u.id = t.user_id"
    args: tuple = ()
    if user_id is not None:
        sql += " WHERE t.user_id = ?"
        args = (user_id,)
    rows = db().execute(sql + " ORDER BY t.created_at DESC", args).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        d["permissions"] = json.loads(d["permissions"] or "[]")
        out.append(d)
    return out


def touch_api_token(token_id: int, ip: Optional[str]) -> None:
    db().execute("UPDATE api_tokens SET last_used_at = ?, last_used_ip = ? WHERE id = ?",
                 (time.time(), ip, token_id))
    db().commit()


def revoke_api_token(token_id: int) -> None:
    db().execute("UPDATE api_tokens SET revoked_at = ? WHERE id = ? AND revoked_at IS NULL",
                 (time.time(), token_id))
    db().commit()

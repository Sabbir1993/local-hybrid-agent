import sqlite3
import time
from typing import Optional

from .common import db


def create_companion_device(user_id: int, name: str, device_hash: str, key_hash: str) -> int:
    cur = db().execute(
        "INSERT INTO companion_devices (user_id, name, device_hash, key_hash, created_at) "
        "VALUES (?, ?, ?, ?, ?)", (user_id, name, device_hash, key_hash, time.time()))
    db().commit()
    return cur.lastrowid


def get_companion_device_by_key(key_hash: str) -> Optional[sqlite3.Row]:
    return db().execute("SELECT * FROM companion_devices WHERE key_hash = ?", (key_hash,)).fetchone()


def get_companion_device(device_row_id: int) -> Optional[sqlite3.Row]:
    return db().execute("SELECT * FROM companion_devices WHERE id = ?", (device_row_id,)).fetchone()


def list_companion_devices(user_id: Optional[int] = None) -> list:
    sql = ("SELECT d.id, d.user_id, u.username, d.name, d.created_at, d.last_seen_at, d.revoked_at "
           "FROM companion_devices d LEFT JOIN users u ON u.id = d.user_id")
    args: tuple = ()
    if user_id is not None:
        sql += " WHERE d.user_id = ?"
        args = (user_id,)
    return [dict(r) for r in db().execute(sql + " ORDER BY d.created_at DESC", args).fetchall()]


def revoke_companion_devices(user_id: int, device_hash: str) -> None:
    """Re-pairing a machine retires its earlier keys."""
    db().execute("UPDATE companion_devices SET revoked_at = ? WHERE user_id = ? AND device_hash = ? "
                 "AND revoked_at IS NULL", (time.time(), user_id, device_hash))
    db().commit()


def revoke_companion_device(device_row_id: int) -> None:
    db().execute("UPDATE companion_devices SET revoked_at = ? WHERE id = ? AND revoked_at IS NULL",
                 (time.time(), device_row_id))
    db().commit()


def touch_companion_device(device_row_id: int) -> None:
    db().execute("UPDATE companion_devices SET last_seen_at = ? WHERE id = ?", (time.time(), device_row_id))
    db().commit()

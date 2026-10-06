"""Per-run git checkpoints and rewind (Phase A2).

Before an agent run's first write lands, a lightweight git snapshot is taken
with `git stash create` (which writes a commit object WITHOUT touching the
worktree or the stash list - the ideal checkpoint primitive). The resulting
commit hash is recorded in the `checkpoints` table keyed by run_id, and the
run can be rewound to any of its checkpoints with `git reset --hard <ref>`.

Safety rules, enforced here and re-enforced in the companion gitops layer:
  * only refs this module created may be reset - a user/other ref never is;
  * the workspace's real git repo is never touched except stash create /
    reset onto a ref this module recorded;
  * checkpoints are pruned to checkpoint_max per (user, session).

The git execution is injected (`git` callable) so this module stays pure and
unit-testable; the real caller wires it to core/git_tools._run.
"""

import sys
import time
import sqlite3
from typing import Optional

from ..config import USAGE_DB_FILE
from ..sqlite_util import ThreadLocalDB

CHECKPOINT_PREFIX = "checkpoints/run_"
CHECKPOINT_MAX_DEFAULT = 5


def _init_checkpoint_db() -> ThreadLocalDB:
    conn = ThreadLocalDB(USAGE_DB_FILE, row_factory=sqlite3.Row)
    conn.execute("""CREATE TABLE IF NOT EXISTS checkpoints (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        run_id TEXT NOT NULL,
        user_id INTEGER NOT NULL,
        session_id TEXT,
        step INTEGER DEFAULT 0,
        ref TEXT NOT NULL,
        ts REAL NOT NULL
    )""")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_checkpoints_run ON checkpoints(run_id)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_checkpoints_user ON checkpoints(user_id)")
    conn.commit()
    return conn


_checkpoint_db_conn = _init_checkpoint_db()


def _checkpoint_db() -> ThreadLocalDB:
    # USAGE_DB_FILE is patched by tests/other modules; re-initialise if it
    # moved to another path (the singleton is keyed to the path at init).
    global _checkpoint_db_conn
    if _checkpoint_db_conn.path != str(USAGE_DB_FILE):
        try:
            _checkpoint_db_conn.close()
        except Exception:
            pass
        _checkpoint_db_conn = _init_checkpoint_db()
    return _checkpoint_db_conn


def checkpoint_ref(run_id: str, step: int) -> str:
    """The ref shape for a checkpoint: namespaced so a reset can only ever
    target what this module created."""
    return f"{CHECKPOINT_PREFIX}{run_id}_step{step}"


async def create_checkpoint(run_id: str, user_id: int, session_id: Optional[str],
                            step: int, git) -> Optional[str]:
    """Take a checkpoint via `git stash create` and record it. Returns the
    commit hash, or None when git is unavailable / there is nothing to
    snapshot."""
    try:
        code, out, err = await git(["stash", "create"])
        commit = (out or "").strip()
        if code != 0 or not commit:
            return None
        conn = _checkpoint_db()
        conn.execute(
            "INSERT INTO checkpoints (run_id, user_id, session_id, step, ref, ts) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (run_id, user_id, session_id, step, commit, time.time()))
        conn.commit()
        _prune(user_id, session_id)
        return commit
    except Exception as e:
        print(f"[checkpoint] create failed: {e}", file=sys.stderr)
        return None


async def rewind(run_id: str, step: int, user_id: int, git) -> Optional[str]:
    """Reset the workspace to a recorded checkpoint for (run_id, step).
    Refuses anything not in this module's table. Returns the commit hash on
    success, None when the checkpoint does not exist / git is unavailable."""
    conn = _checkpoint_db()
    row = conn.execute(
        "SELECT ref FROM checkpoints WHERE run_id=? AND step=? AND user_id=? "
        "ORDER BY id DESC LIMIT 1",
        (run_id, step, user_id)).fetchone()
    if row is None:
        return None
    commit = str(row["ref"])
    code, _out, _err = await git(["reset", "--hard", commit])
    if code != 0:
        return None
    return commit


def list_checkpoints(run_id: str, user_id: int) -> list:
    conn = _checkpoint_db()
    rows = conn.execute(
        "SELECT step, ref, ts FROM checkpoints WHERE run_id=? AND user_id=? ORDER BY step",
        (run_id, user_id)).fetchall()
    return [dict(r) for r in rows]


def _prune(user_id: int, session_id: Optional[str], max_keep: int = CHECKPOINT_MAX_DEFAULT) -> None:
    """Keep the newest `max_keep` checkpoints per (user, session); drop older."""
    if not session_id:
        return
    conn = _checkpoint_db()
    rows = conn.execute(
        "SELECT id FROM checkpoints WHERE user_id=? AND session_id=? ORDER BY ts DESC, id DESC",
        (user_id, session_id)).fetchall()
    for r in rows[max_keep:]:
        conn.execute("DELETE FROM checkpoints WHERE id=?", (r["id"],))
    conn.commit()

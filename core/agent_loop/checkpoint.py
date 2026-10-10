"""Per-run git checkpoints and rewind (Phase A2).

Before an agent run's first write lands, a lightweight git snapshot is taken: `git stash create` (a commit of
the tracked changes that touches neither the worktree nor the stash list) or, on a clean tree, HEAD. The commit
hash and the list of untracked files are recorded in the `checkpoints` table keyed by run_id.

Rewinding restores the tracked files from that commit with `git restore --source=<commit> --staged --worktree`.
It never uses `reset --hard`, `clean` or `update-ref`, which both the server and the companion refuse on
purpose, so the safety guards stay as strict as before. Around the restore:
  * the current state is saved first (`git stash create`) and its hash returned, so a rewind can itself be undone;
  * untracked files are never deleted: the ones created since the checkpoint are listed for the user;
  * only commits this module recorded can be restored, and checkpoints are pruned per (user, session).

The git execution is injected (`git` callable) so this module stays pure and unit-testable; the real caller
wires it to core/git_tools._run.
"""

import json
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
    if "untracked" not in {r[1] for r in conn.execute("PRAGMA table_info(checkpoints)").fetchall()}:
        conn.execute("ALTER TABLE checkpoints ADD COLUMN untracked TEXT")
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


MAX_UNTRACKED_RECORDED = 500


async def _untracked(git) -> list:
    code, out, _ = await git(["ls-files", "--others", "--exclude-standard"])
    return [ln.strip() for ln in (out or "").splitlines() if ln.strip()] if code == 0 else []


async def create_checkpoint(run_id: str, user_id: int, session_id: Optional[str],
                            step: int, git) -> Optional[str]:
    """Take a checkpoint and record it. Returns the commit hash, or None when git is unavailable or the
    repository has no commit to anchor to.

    `git stash create` prints nothing when the tracked files are clean, which is the common case at the start of
    a run; HEAD is the checkpoint then (restoring to it means "the tracked files as committed")."""
    try:
        code, out, _err = await git(["stash", "create"])
        commit = (out or "").strip() if code == 0 else ""
        if not commit:
            code, out, _err = await git(["rev-parse", "HEAD"])
            commit = (out or "").strip() if code == 0 else ""
        if not commit:
            return None
        untracked = (await _untracked(git))[:MAX_UNTRACKED_RECORDED]
        conn = _checkpoint_db()
        conn.execute(
            "INSERT INTO checkpoints (run_id, user_id, session_id, step, ref, ts, untracked) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (run_id, user_id, session_id, step, commit, time.time(), json.dumps(untracked)))
        conn.commit()
        _prune(user_id, session_id)
        return commit
    except Exception as e:
        print(f"[checkpoint] create failed: {e}", file=sys.stderr)
        return None


async def rewind(run_id: str, step: int, user_id: int, git) -> Optional[dict]:
    """Restore the tracked files to a recorded checkpoint for (run_id, step).

    None when no such checkpoint belongs to this user. Otherwise a dict: on success
    {"commit", "backup" (hash of the state just before the rewind, or None when nothing had changed),
     "left_untracked" (files created since the checkpoint, left in place)}; on failure {"error": reason}."""
    conn = _checkpoint_db()
    row = conn.execute(
        "SELECT ref, untracked FROM checkpoints WHERE run_id=? AND step=? AND user_id=? "
        "ORDER BY id DESC LIMIT 1",
        (run_id, step, user_id)).fetchone()
    if row is None:
        return None
    commit = str(row["ref"])
    code, _out, _err = await git(["rev-parse", "--verify", "--quiet", commit + "^{commit}"])
    if code != 0:
        return {"error": "the checkpoint's git object no longer exists (git removed it); it cannot be restored"}
    try:
        before = set(json.loads(row["untracked"] or "[]"))
    except (TypeError, ValueError):
        before = set()
    created = [f for f in await _untracked(git) if f not in before]
    # save what is there now, so the rewind can be undone with `git restore --source=<backup> --staged --worktree -- .`
    # (not `stash apply`: it conflicts with the files that were just restored)
    code, out, _err = await git(["stash", "create"])
    backup = (out or "").strip() if code == 0 else ""
    code, _out, err = await git(["restore", "--source=" + commit, "--staged", "--worktree", "--", "."])
    if code != 0:
        return {"error": f"git restore failed: {(err or '').strip()[:200]}", "backup": backup or None}
    return {"commit": commit, "backup": backup or None, "left_untracked": created[:100]}


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

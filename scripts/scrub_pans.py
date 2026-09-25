"""scripts/scrub_pans.py - one-off: mask card numbers (PANs) already stored at rest.

core/db.py now masks PANs on every write, but rows saved before that change may
still hold them in the clear (PCI DSS 3.4):
  - projects.db  messages.content / messages.meta / sessions.title  -> masked in place
  - memory.db    "session" chunks containing a PAN                   -> deleted; the
                 memory indexer rebuilds them from the (now masked) sessions
Knowledge-base chunks are left alone by policy (see core/pan.py).

Dry run by default. Stop the server first, then (--apply backs each database
up to db_backups/ before changing it):
    python scripts/scrub_pans.py            # report only
    python scripts/scrub_pans.py --apply    # rewrite
"""

import argparse
import sqlite3
from contextlib import closing
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core import pan  # noqa: E402
from core.config import MEMORY_DB_FILE, PROJECTS_DB_FILE  # noqa: E402


def _scrub_projects(conn: sqlite3.Connection, apply: bool) -> int:
    n = 0
    for table, key, cols in (("messages", "id", ("content", "meta")), ("sessions", "id", ("title",))):
        for row in conn.execute(f"SELECT {key}, {', '.join(cols)} FROM {table}").fetchall():
            updates = {}
            for i, col in enumerate(cols, start=1):
                masked, k = pan.mask_pans(row[i])
                if k:
                    updates[col] = masked
                    n += k
            if updates and apply:
                sets = ", ".join(f"{c} = ?" for c in updates)
                conn.execute(f"UPDATE {table} SET {sets} WHERE {key} = ?", (*updates.values(), row[0]))
    return n


def _scrub_memory(conn: sqlite3.Connection, apply: bool) -> int:
    ids = [r[0] for r in conn.execute("SELECT rowid, text FROM chunks WHERE source = 'session'").fetchall()
           if pan.contains_pan(r[1])]
    if apply and ids:
        conn.executemany("DELETE FROM chunks WHERE rowid = ?", [(i,) for i in ids])
    return len(ids)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--apply", action="store_true", help="rewrite the databases (default: report only)")
    args = ap.parse_args()
    verb = "masked" if args.apply else "would mask"
    if args.apply:
        from core.db_repair import backup
        for path in (PROJECTS_DB_FILE, MEMORY_DB_FILE):
            if path.exists():
                print(f"backup: {backup(path, 'pre-scrub')}")
    if PROJECTS_DB_FILE.exists():
        conn = sqlite3.connect(PROJECTS_DB_FILE)
        with closing(conn), conn:
            print(f"projects.db: {verb} {_scrub_projects(conn, args.apply)} card number(s)")
    if MEMORY_DB_FILE.exists():
        conn = sqlite3.connect(MEMORY_DB_FILE)
        with closing(conn), conn:
            verb2 = "deleted" if args.apply else "would delete"
            print(f"memory.db: {verb2} {_scrub_memory(conn, args.apply)} session chunk(s) containing a card number")
    if not args.apply:
        print("dry run - re-run with --apply after stopping the server")


if __name__ == "__main__":
    main()

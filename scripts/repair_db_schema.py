"""scripts/repair_db_schema.py - report or fix the SQLite foreign-key schema.

  python scripts/repair_db_schema.py           dry run: what would change (read-only)
  python scripts/repair_db_schema.py --apply   back up, then repair (stop the server first)

The server does the same repair on every start (core/db_repair.py), so this
is only needed to look before restarting, or to repair while it's stopped.
"""

import argparse
import json
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from core.config import AUTH_DB_FILE, PROJECTS_DB_FILE  # noqa: E402

# core.db_repair imports core.db / core.auth_db only inside enable_foreign_keys(),
# so a dry run never opens the databases for writing.
from core import db_repair  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--apply", action="store_true", help="back up and repair")
    args = ap.parse_args()
    rc = 0
    for path in (PROJECTS_DB_FILE, AUTH_DB_FILE):
        if not path.exists():
            print(f"{path.name}: not found")
            continue
        try:
            res = db_repair.repair(path) if args.apply else db_repair.inspect(path)
        except sqlite3.Error as e:
            print(f"{path.name}: {e}")
            rc = 1
            continue
        print(json.dumps(res, indent=2))
        if args.apply and not res.get("ok"):
            rc = 1
    return rc


if __name__ == "__main__":
    sys.exit(main())

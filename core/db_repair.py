"""core/db_repair.py - make the SQLite foreign keys usable, then switch them on.

SQLite ships with foreign keys off, so the schema's ON DELETE CASCADE rules
never ran. Two things stop simply switching them on:

  * broken references - an old rename-and-rebuild migration made SQLite rewrite
    child tables to point at "projects_old" / "sessions_old", which were then
    dropped. With foreign keys on, every insert into those children fails.
  * orphan rows left behind by deletes that ran with foreign keys off.

repair() fixes both, one database at a time, after a backup:
  * a table whose REFERENCES name a missing table is rebuilt with the name
    corrected (build new table, copy, drop old, rename new into place);
  * an orphan row gets its link set to NULL when the column allows it
    (audit_log keeps every row and its username - PCI DSS 10), and is
    deleted when it can't exist without its parent (e.g. a session token).

It runs at server start (server_manager), never at import, and
scripts/repair_db_schema.py runs it by hand (dry run by default). Foreign
keys are switched on for a database only when its check comes back clean.
"""

import re
import sqlite3
import sys
import time
from pathlib import Path

from .config import BASE_DIR

BACKUP_DIR = BASE_DIR / "db_backups"
_REF_RX = re.compile(r'(REFERENCES\s+)(["`\[]?)(\w+)(["`\]]?)', re.I)


def _tables(conn) -> set:
    return {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}


def broken_references(conn) -> dict:
    """{table: {missing_parent: replacement}} for REFERENCES to a table that
    no longer exists. The replacement is the name without an _old/_new suffix,
    when that table exists."""
    names = _tables(conn)
    out = {}
    for t in sorted(names):
        if t.startswith("sqlite_"):
            continue
        for fk in conn.execute(f'PRAGMA foreign_key_list("{t}")'):
            parent = fk[2]
            if parent in names:
                continue
            fixed = re.sub(r"_(old|new)$", "", parent)
            out.setdefault(t, {})[parent] = fixed if fixed in names else None
    return out


def orphan_rows(conn) -> list:
    """[(table, rowid, column, nullable)] from PRAGMA foreign_key_check, for
    parents that exist (rows under a missing parent are handled by the rebuild)."""
    names = _tables(conn)
    out = []
    fk_cols, notnull = {}, {}
    for table, rowid, parent, fkid in conn.execute("PRAGMA foreign_key_check").fetchall():
        if parent not in names:
            continue
        if table not in fk_cols:
            fk_cols[table] = {r[0]: r[3] for r in conn.execute(f'PRAGMA foreign_key_list("{table}")')}
            notnull[table] = {r[1]: bool(r[3]) for r in conn.execute(f'PRAGMA table_info("{table}")')}
        col = fk_cols[table].get(fkid)
        out.append((table, rowid, col, not notnull[table].get(col, True)))
    return out


def inspect(path) -> dict:
    """Read-only summary of what repair() would change."""
    conn = sqlite3.connect(f"file:{Path(path).as_posix()}?mode=ro", uri=True)
    try:
        broken = broken_references(conn)
        orphans = orphan_rows(conn)
    finally:
        conn.close()
    summary = {}
    for table, _rowid, col, nullable in orphans:
        key = f"{table}.{col}"
        s = summary.setdefault(key, {"action": "set NULL" if nullable else "delete", "rows": 0})
        s["rows"] += 1
    return {"db": Path(path).name, "rebuild": broken, "orphans": summary,
            "clean": not broken and not orphans}


def backup(path, tag: str = "pre-fk") -> Path:
    """Consistent copy via the SQLite backup API. With WAL on, copying just the
    .db file can miss recent commits that are still in the -wal file."""
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    dest = BACKUP_DIR / f"{Path(path).stem}.{tag}-{time.strftime('%Y%m%d-%H%M%S')}.db"
    src = sqlite3.connect(str(path))
    dst = sqlite3.connect(str(dest))
    try:
        src.backup(dst)   # consistent copy, including anything still in the WAL
    finally:
        dst.close()
        src.close()
    return dest


def _rebuild(conn, table: str, renames: dict) -> None:
    sql = conn.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone()[0]
    fixed = _REF_RX.sub(lambda m: m.group(1) + (renames.get(m.group(3)) or m.group(3))
                        if m.group(3) in renames else m.group(0), sql)
    tmp = f"{table}__fkfix"
    fixed = re.sub(r'^\s*CREATE\s+TABLE\s+(IF\s+NOT\s+EXISTS\s+)?(["`\[]?)' + re.escape(table) + r'(["`\]]?)',
                   f'CREATE TABLE "{tmp}"', fixed, count=1, flags=re.I)
    indexes = [r[0] for r in conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='index' AND tbl_name=? AND sql IS NOT NULL", (table,))]
    triggers = [r[0] for r in conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='trigger' AND tbl_name=?", (table,))]
    cols = ", ".join(f'"{r[1]}"' for r in conn.execute(f'PRAGMA table_info("{table}")'))
    conn.execute(f'DROP TABLE IF EXISTS "{tmp}"')
    conn.execute(fixed)
    conn.execute(f'INSERT INTO "{tmp}" ({cols}) SELECT {cols} FROM "{table}"')
    conn.execute(f'DROP TABLE "{table}"')
    conn.execute(f'ALTER TABLE "{tmp}" RENAME TO "{table}"')
    for stmt in indexes + triggers:
        conn.execute(stmt)


def repair(path, make_backup: bool = True) -> dict:
    """Fix broken references and orphan rows in one database file. Returns the
    inspect() report plus "ok" (the foreign key check is clean afterwards)."""
    report = inspect(path)
    if report["clean"]:
        return {**report, "ok": True, "backup": None}
    unfixable = {t: m for t, m in report["rebuild"].items() if None in m.values()}
    if unfixable:
        return {**report, "ok": False, "backup": None,
                "error": f"references to missing tables with no replacement: {unfixable}"}
    dest = backup(path) if make_backup else None
    conn = sqlite3.connect(str(path), isolation_level=None, timeout=10)
    try:
        conn.execute("PRAGMA foreign_keys=OFF")
        conn.execute("PRAGMA legacy_alter_table=ON")   # a rename must not rewrite other tables' REFERENCES
        conn.execute("BEGIN IMMEDIATE")
        try:
            for table, renames in report["rebuild"].items():
                _rebuild(conn, table, renames)
            for table, rowid, col, nullable in orphan_rows(conn):
                if nullable and col:
                    conn.execute(f'UPDATE "{table}" SET "{col}" = NULL WHERE rowid = ?', (rowid,))
                else:
                    conn.execute(f'DELETE FROM "{table}" WHERE rowid = ?', (rowid,))
            left = conn.execute("PRAGMA foreign_key_check").fetchall()
            if left or broken_references(conn):
                raise RuntimeError(f"{len(left)} foreign key problems left after repair")
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise
    except Exception as e:
        return {**report, "ok": False, "backup": str(dest) if dest else None, "error": str(e)}
    finally:
        conn.close()
    return {**report, "ok": True, "backup": str(dest) if dest else None}


def enable_foreign_keys() -> list:
    """Server start: repair projects.db and auth.db, then switch foreign keys on
    for each database that checks clean. A failure leaves that database as it
    was (foreign keys off) and is logged; the server still starts."""
    from . import auth_db, db as projects
    results = []
    for name, handle, path in (("projects.db", projects._projects_db, projects.PROJECTS_DB_FILE),
                               ("auth.db", auth_db.db(), auth_db.AUTH_DB_FILE)):
        try:
            if not getattr(handle, "path", None) or Path(handle.path) != Path(path):
                continue
            handle.commit()
            res = repair(path)
            if res["ok"]:
                handle.foreign_keys = True
                if not res.get("clean"):
                    print(f"[db] {name}: repaired foreign keys (backup {res['backup']}): "
                          f"rebuilt {list(res['rebuild'])}, orphans {res['orphans']}", file=sys.stderr)
                print(f"[db] {name}: foreign keys ON", file=sys.stderr)
            else:
                print(f"[db] {name}: foreign keys left OFF - {res.get('error')}", file=sys.stderr)
            results.append({"db": name, **res})
        except Exception as e:
            print(f"[db] {name}: foreign key repair failed: {e}", file=sys.stderr)
            results.append({"db": name, "ok": False, "error": str(e)})
    return results

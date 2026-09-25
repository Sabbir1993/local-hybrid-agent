"""core/sqlite_util.py - SQLite connections that are safe to share across threads.

Each app database used to be one sqlite3.Connection opened with
check_same_thread=False and shared by the event loop and the tool worker
threads. SQLite transactions belong to a connection, so one thread's commit
could flush another thread's half-finished writes. ThreadLocalDB gives every
thread its own connection to the same file, behind the surface callers
already use (execute / commit / row_factory ...). WAL lets readers run while a
writer holds the lock, and busy_timeout waits instead of failing with
"database is locked".
"""

import sqlite3
import sys
import threading
from contextlib import contextmanager
from pathlib import Path

BUSY_TIMEOUT_MS = 10000


class ThreadLocalDB:
    def __init__(self, path, row_factory=None):
        self.path = str(path)
        self._row_factory = row_factory
        self._local = threading.local()
        self._all = {}              # thread ident -> connection, so close() can reach them all
        self._lock = threading.Lock()
        self._wal_checked = False
        self.foreign_keys = False   # switched on by core/db_repair once the schema checks clean

    # ---- connection per thread ----
    def _open(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path, timeout=BUSY_TIMEOUT_MS / 1000, check_same_thread=False)
        conn.row_factory = self._row_factory
        conn.execute(f"PRAGMA busy_timeout={BUSY_TIMEOUT_MS}")
        with self._lock:
            if not self._wal_checked:
                self._wal_checked = True
                self._enable_wal(conn)
        conn.execute("PRAGMA synchronous=NORMAL")
        with self._lock:
            alive = {t.ident for t in threading.enumerate()}
            for ident in [i for i in self._all if i not in alive]:
                try:
                    self._all.pop(ident).close()   # left behind by a thread that has exited
                except Exception:
                    pass
            self._all[threading.get_ident()] = conn
        return conn

    def _enable_wal(self, conn: sqlite3.Connection) -> None:
        # Persistent per file. Switching needs the file to itself, so don't wait
        # for another process holding it - just try again on the next start.
        try:
            conn.execute("PRAGMA busy_timeout=0")
            mode = conn.execute("PRAGMA journal_mode=WAL").fetchone()[0]
            if str(mode).lower() != "wal":
                print(f"[db] {Path(self.path).name}: journal_mode stays {mode}", file=sys.stderr)
        except sqlite3.OperationalError as e:
            print(f"[db] {Path(self.path).name}: WAL not enabled yet ({e})", file=sys.stderr)
        finally:
            conn.execute(f"PRAGMA busy_timeout={BUSY_TIMEOUT_MS}")

    def conn(self) -> sqlite3.Connection:
        c = getattr(self._local, "conn", None)
        if c is None:
            c = self._open()
            self._local.conn = c
            self._local.fk = None
        if self._local.fk != self.foreign_keys and not c.in_transaction:
            # PRAGMA foreign_keys is a no-op inside a transaction; retried next call
            c.execute(f"PRAGMA foreign_keys={'ON' if self.foreign_keys else 'OFF'}")
            self._local.fk = self.foreign_keys
        return c

    # ---- sqlite3.Connection surface ----
    def execute(self, *a, **k):
        return self.conn().execute(*a, **k)

    def executemany(self, *a, **k):
        return self.conn().executemany(*a, **k)

    def executescript(self, *a, **k):
        return self.conn().executescript(*a, **k)

    def cursor(self, *a, **k):
        return self.conn().cursor(*a, **k)

    def commit(self):
        return self.conn().commit()

    def rollback(self):
        return self.conn().rollback()

    @property
    def in_transaction(self) -> bool:
        return self.conn().in_transaction

    @property
    def total_changes(self) -> int:
        return self.conn().total_changes

    @property
    def row_factory(self):
        return self._row_factory

    @row_factory.setter
    def row_factory(self, value):
        self._row_factory = value
        with self._lock:
            for c in self._all.values():
                c.row_factory = value

    def __enter__(self):
        return self.conn().__enter__()

    def __exit__(self, *exc):
        return self.conn().__exit__(*exc)

    def backup(self, target, **k):
        return self.conn().backup(target, **k)

    def close(self):
        with self._lock:
            conns = list(self._all.values())
            self._all = {}
        for c in conns:
            try:
                c.close()
            except Exception:
                pass
        self._local = threading.local()

    @contextmanager
    def transaction(self):
        """BEGIN IMMEDIATE ... COMMIT, or ROLLBACK when the block raises, so a
        failed multi-statement write never leaves a transaction open."""
        c = self.conn()
        if c.in_transaction:
            c.commit()
        c.execute("BEGIN IMMEDIATE")
        try:
            yield c
        except BaseException:
            c.rollback()
            raise
        else:
            c.commit()


@contextmanager
def transaction(conn):
    """transaction() for either a ThreadLocalDB or a plain sqlite3.Connection
    (tests patch plain connections in)."""
    if isinstance(conn, ThreadLocalDB):
        with conn.transaction() as c:
            yield c
        return
    if conn.in_transaction:
        conn.commit()
    try:
        yield conn
    except BaseException:
        conn.rollback()
        raise
    else:
        conn.commit()

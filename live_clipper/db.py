"""SQLite access: one connection per thread, WAL mode, namespaced migrations."""

import json
import sqlite3
import threading
import time
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

Migration = str | Callable[[sqlite3.Connection], None]

# namespace -> ordered migrations; version N is migrations[N-1]. Only ever append.
REGISTRY: dict[str, list[Migration]] = {}


def register_migrations(namespace: str, migrations: list[Migration]) -> None:
    """Register a subsystem's ordered migrations. Call at import time of the subsystem module."""
    REGISTRY[namespace] = list(migrations)


register_migrations("core", ["""
CREATE TABLE IF NOT EXISTS settings_kv(
    key TEXT PRIMARY KEY, value_json TEXT NOT NULL, updated_at REAL NOT NULL);
CREATE TABLE IF NOT EXISTS app_state(key TEXT PRIMARY KEY, value_json TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS events(
    id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL NOT NULL, level TEXT NOT NULL,
    source TEXT NOT NULL, message TEXT NOT NULL, data_json TEXT);
CREATE INDEX IF NOT EXISTS events_ts ON events(ts);
"""])


def _statements(script: str) -> Iterator[str]:
    buf = ""
    for part in script.split(";"):
        buf += part + ";"
        if sqlite3.complete_statement(buf):
            if buf.strip(" \t\r\n;"):
                yield buf
            buf = ""
    if buf.strip(" \t\r\n;"):
        yield buf  # let sqlite report the syntax error


class Database:
    def __init__(self, path: Path | str):
        self.path = str(path)
        self._local = threading.local()

    def conn(self) -> sqlite3.Connection:
        """This thread's connection (autocommit; use `with db.tx():` for transactions)."""
        c = getattr(self._local, "conn", None)
        if c is None:
            c = sqlite3.connect(self.path, isolation_level=None, timeout=30, check_same_thread=False)
            c.row_factory = sqlite3.Row
            c.execute("PRAGMA journal_mode=WAL")
            c.execute("PRAGMA foreign_keys=ON")
            c.execute("PRAGMA busy_timeout=30000")
            self._local.conn = c
        return c

    def execute(self, sql: str, params: Any = ()) -> sqlite3.Cursor:
        return self.conn().execute(sql, params)

    def tx(self) -> "_Tx":
        return _Tx(self.conn())

    def migrate(self) -> None:
        """Apply every registered migration not yet recorded. Safe to call from several processes."""
        c = self.conn()
        c.execute("CREATE TABLE IF NOT EXISTS schema_migrations("
                  "namespace TEXT NOT NULL, version INTEGER NOT NULL, applied_at REAL NOT NULL, "
                  "PRIMARY KEY(namespace, version))")
        for namespace, migrations in list(REGISTRY.items()):
            for version, migration in enumerate(migrations, start=1):
                with self.tx():
                    done = c.execute("SELECT 1 FROM schema_migrations WHERE namespace=? AND version=?",
                                     (namespace, version)).fetchone()
                    if done:
                        continue
                    if callable(migration):
                        migration(c)
                    else:
                        for stmt in _statements(migration):
                            c.execute(stmt)
                    c.execute("INSERT INTO schema_migrations VALUES (?, ?, ?)",
                              (namespace, version, time.time()))

    def event(self, level: str, source: str, message: str, data: Any = None) -> None:
        self.execute("INSERT INTO events(ts, level, source, message, data_json) VALUES (?, ?, ?, ?, ?)",
                     (time.time(), level, source, message, None if data is None else json.dumps(data)))


class _Tx:
    def __init__(self, conn: sqlite3.Connection):
        self.conn = conn

    def __enter__(self) -> sqlite3.Connection:
        self.conn.execute("BEGIN IMMEDIATE")
        return self.conn

    def __exit__(self, exc_type, exc, tb) -> None:
        self.conn.execute("ROLLBACK" if exc_type else "COMMIT")


class KV:
    """JSON key/value access to `settings_kv` or `app_state`."""

    def __init__(self, db: Database, table: str):
        assert table in ("settings_kv", "app_state")
        self.db, self.table = db, table

    def get(self, key: str, default: Any = None) -> Any:
        row = self.db.execute(f"SELECT value_json FROM {self.table} WHERE key=?", (key,)).fetchone()
        return default if row is None else json.loads(row[0])

    def set(self, key: str, value: Any) -> None:
        if self.table == "settings_kv":
            self.db.execute("INSERT OR REPLACE INTO settings_kv VALUES (?, ?, ?)",
                            (key, json.dumps(value), time.time()))
        else:
            self.db.execute("INSERT OR REPLACE INTO app_state VALUES (?, ?)", (key, json.dumps(value)))

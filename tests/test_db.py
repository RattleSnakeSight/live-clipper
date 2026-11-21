import sqlite3
import threading

import pytest

from live_clipper import db as dbmod
from live_clipper.paths import get_paths


@pytest.fixture
def registry(monkeypatch):
    reg = {"core": dbmod.REGISTRY["core"]}
    monkeypatch.setattr(dbmod, "REGISTRY", reg)
    return reg


def test_paths_use_env_override_and_create_subdirs(home):
    p = get_paths()
    assert p.root == home
    for sub in ("db", "media", "logs", "secrets", "state"):
        assert (home / sub).is_dir()
        assert getattr(p, sub) == home / sub


def test_core_tables_and_pragmas(registry):
    d = dbmod.Database(get_paths().db / "app.sqlite3")
    d.migrate()
    tables = {r[0] for r in d.conn().execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert {"schema_migrations", "settings_kv", "app_state", "events"} <= tables
    assert d.conn().execute("PRAGMA journal_mode").fetchone()[0] == "wal"
    assert d.conn().execute("PRAGMA foreign_keys").fetchone()[0] == 1


def test_migrations_idempotent_and_namespaced(registry):
    calls = []

    def py_migration(conn):
        calls.append(1)
        conn.execute("INSERT INTO a_items(name) VALUES ('from-callable')")

    dbmod.register_migrations("alpha", [
        "CREATE TABLE a_items(id INTEGER PRIMARY KEY, name TEXT); INSERT INTO a_items(name) VALUES ('x;y')",
        py_migration,
    ])
    dbmod.register_migrations("beta", ["CREATE TABLE b_items(id INTEGER PRIMARY KEY)"])
    d = dbmod.Database(get_paths().db / "app.sqlite3")
    d.migrate()
    d.migrate()
    dbmod.Database(get_paths().db / "app.sqlite3").migrate()

    assert calls == [1]
    rows = d.conn().execute("SELECT namespace, version FROM schema_migrations ORDER BY 1, 2").fetchall()
    assert [tuple(r) for r in rows] == [("alpha", 1), ("alpha", 2), ("beta", 1), ("core", 1)]
    names = [r[0] for r in d.conn().execute("SELECT name FROM a_items ORDER BY id")]
    assert names == ["x;y", "from-callable"]

    # Appending a migration later applies only the new one.
    registry["beta"].append("ALTER TABLE b_items ADD COLUMN label TEXT")
    d.migrate()
    top = d.conn().execute("SELECT max(version) FROM schema_migrations WHERE namespace='beta'").fetchone()[0]
    assert top == 2


def test_failed_migration_rolls_back(registry):
    dbmod.register_migrations("bad", ["CREATE TABLE ok_t(id INTEGER); THIS IS NOT SQL"])
    d = dbmod.Database(get_paths().db / "app.sqlite3")
    with pytest.raises(sqlite3.Error):
        d.migrate()
    tables = {r[0] for r in d.conn().execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert "ok_t" not in tables
    assert d.conn().execute("SELECT count(*) FROM schema_migrations WHERE namespace='bad'").fetchone()[0] == 0


def test_kv_event_and_thread_local_connections(registry):
    d = dbmod.Database(get_paths().db / "app.sqlite3")
    d.migrate()
    settings = dbmod.KV(d, "settings_kv")
    assert settings.get("missing", 5) == 5
    settings.set("theme", {"dark": True})
    assert settings.get("theme") == {"dark": True}
    d.event("info", "test", "hello", {"n": 1})
    row = d.conn().execute("SELECT level, source, message, data_json FROM events").fetchone()
    assert tuple(row) == ("info", "test", "hello", '{"n": 1}')

    seen = []
    t = threading.Thread(target=lambda: seen.append((d.conn(), settings.get("theme"))))
    t.start()
    t.join()
    assert seen[0][0] is not d.conn()
    assert seen[0][1] == {"dark": True}

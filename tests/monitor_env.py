"""Shared monitor test setup: settings, store and source secrets on a throwaway data dir."""

from live_clipper.config import Settings
from live_clipper.db import Database
from live_clipper.secrets import Vault
from live_clipper.sources import SourceSecrets
from live_clipper.store import Store


def env(tmp_path, **settings):
    tmp_path.mkdir(parents=True, exist_ok=True)
    db = Database(tmp_path / "db.sqlite3")
    db.migrate()
    s = Settings(data_dir=tmp_path / "data", **{"min_free_mb": 0, "critical_free_mb": 0, **settings})
    s.ensure_dirs()
    return s, Store(db), SourceSecrets(Vault(tmp_path / "secrets"))

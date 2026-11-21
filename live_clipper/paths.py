"""Data directory layout. Data lives outside the install so upgrades and reinstalls keep it."""

import os
from dataclasses import dataclass
from pathlib import Path

from platformdirs import user_data_dir

SUBDIRS = ("db", "media", "logs", "secrets", "state")


@dataclass(frozen=True)
class Paths:
    root: Path

    @property
    def db(self) -> Path:
        return self.root / "db"

    @property
    def db_file(self) -> Path:
        return self.root / "db" / "live-clipper.sqlite3"

    @property
    def media(self) -> Path:
        return self.root / "media"

    @property
    def logs(self) -> Path:
        return self.root / "logs"

    @property
    def secrets(self) -> Path:
        return self.root / "secrets"

    @property
    def state(self) -> Path:
        return self.root / "state"


def get_paths(create: bool = True) -> Paths:
    """Data dir from env LIVE_CLIPPER_HOME, else the per-user platform data dir."""
    env = os.environ.get("LIVE_CLIPPER_HOME")
    paths = Paths(Path(env) if env else Path(user_data_dir("live-clipper", appauthor=False)))
    if create:
        for sub in SUBDIRS:
            (paths.root / sub).mkdir(parents=True, exist_ok=True)
    return paths

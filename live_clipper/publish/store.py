"""Publishing tables (namespace `publish`) and a small query helper over the app database."""
from __future__ import annotations

import contextlib
import json
import threading
import time
from typing import Any

from ..db import Database, register_migrations

register_migrations("publish", ["""
CREATE TABLE IF NOT EXISTS publish_destinations(
    id INTEGER PRIMARY KEY AUTOINCREMENT, provider TEXT NOT NULL, label TEXT NOT NULL, account TEXT NOT NULL DEFAULT '',
    settings TEXT NOT NULL DEFAULT '{}', status TEXT NOT NULL DEFAULT 'disconnected', status_detail TEXT NOT NULL DEFAULT '',
    enabled INTEGER NOT NULL DEFAULT 0, rights_ack_at REAL, created_at REAL NOT NULL);
CREATE TABLE IF NOT EXISTS publish_clips(
    clip_id NOT NULL, language TEXT NOT NULL, channel_id, payload TEXT NOT NULL, created_at REAL NOT NULL,
    PRIMARY KEY(clip_id, language));
CREATE TABLE IF NOT EXISTS publish_deliveries(
    id INTEGER PRIMARY KEY AUTOINCREMENT, clip_id NOT NULL, destination_id INTEGER NOT NULL, language TEXT NOT NULL,
    idempotency_key TEXT NOT NULL UNIQUE, status TEXT NOT NULL, mode TEXT NOT NULL DEFAULT 'auto',
    attempts INTEGER NOT NULL DEFAULT 0, provider_ref TEXT NOT NULL DEFAULT '{}', provider_post_id TEXT NOT NULL DEFAULT '',
    provider_url TEXT NOT NULL DEFAULT '', outcome TEXT NOT NULL DEFAULT '', last_error TEXT NOT NULL DEFAULT '',
    next_attempt_at REAL NOT NULL DEFAULT 0, history TEXT NOT NULL DEFAULT '[]', created_at REAL NOT NULL,
    updated_at REAL NOT NULL, reconcile INTEGER NOT NULL DEFAULT 0, version INTEGER NOT NULL DEFAULT 1,
    media_version INTEGER NOT NULL DEFAULT 0, blocked_from TEXT NOT NULL DEFAULT '', claimed_at REAL NOT NULL DEFAULT 0,
    public_at REAL, UNIQUE(clip_id, destination_id, language, version));
CREATE INDEX IF NOT EXISTS publish_deliveries_status ON publish_deliveries(status);
CREATE TABLE IF NOT EXISTS publish_quota(
    destination_id INTEGER NOT NULL, day TEXT NOT NULL, used INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY(destination_id, day));
"""])

JSON_COLS = {"settings", "history"}


class Store:
    """`q`/`q1` read, `x` writes, `update` patches one row by id. JSON columns (settings, history) are decoded."""

    def __init__(self, db: Database):
        self.db = db
        self._lock = threading.RLock()

    @staticmethod
    def _row(r) -> dict:
        d = dict(r)
        for k in JSON_COLS & d.keys():
            try:
                d[k] = json.loads(d[k]) if isinstance(d[k], str) else d[k]
            except ValueError:
                d[k] = {} if k == "settings" else []
        return d

    def q(self, sql: str, *params: Any) -> list[dict]:
        return [self._row(r) for r in self.db.execute(sql, params).fetchall()]

    def q1(self, sql: str, *params: Any) -> dict | None:
        r = self.db.execute(sql, params).fetchone()
        return self._row(r) if r else None

    def x(self, sql: str, *params: Any) -> int:
        return self.db.execute(sql, params).lastrowid

    def tx(self):
        return self.db.tx()

    def atomic(self):
        """A transaction for writes that belong together, or the one this thread already has open."""
        return contextlib.nullcontext() if self.db.conn().in_transaction else self.db.tx()

    def update(self, table: str, row_id: int, **fields: Any) -> None:
        if "updated_at" not in fields and table == "deliveries":
            fields["updated_at"] = time.time()
        vals = [json.dumps(v) if k in JSON_COLS else v for k, v in fields.items()]
        sets = ",".join(f"{k}=?" for k in fields)
        self.db.execute(f"UPDATE publish_{table} SET {sets} WHERE id=?", (*vals, row_id))

    # ---- clips -----------------------------------------------------------
    def clip(self, clip_id: Any, language: str = "en") -> dict | None:
        r = self.db.execute("SELECT payload FROM publish_clips WHERE clip_id=? AND language=?",
                            (clip_id, language)).fetchone()
        return json.loads(r[0]) if r else None

    def save_clip(self, payload: dict) -> None:
        self.db.execute("INSERT OR REPLACE INTO publish_clips VALUES(?,?,?,?,?)",
                        (payload["clip_id"], payload.get("language") or "en", payload.get("channel_id"),
                         json.dumps(payload), time.time()))
